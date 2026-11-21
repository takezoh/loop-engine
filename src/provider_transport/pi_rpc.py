"""The Pi RPC wire client: one `pi --mode rpc` process and its session.

Each command is one JSON record on stdin with an id; stdout carries the one
response that repeats the id, and the session's events. Records end with LF
and are split only on LF, so Unicode line separators inside JSON strings stay
inside their record. When stdout closes, waiting commands fail and listeners
receive one `transport_closed` event.
"""
from __future__ import annotations

from itertools import count
import json
from queue import Empty, Queue
import subprocess
from threading import Lock, Thread
from typing import IO, Any, Callable, Mapping

from .admitted_process import launch_admitted, reap, release
from .bounded_wait import ended_within
from .cwd_authority import SpawnAdmission, SpawnReceipt


TRANSPORT_CLOSED = "transport_closed"


class PiRpcError(RuntimeError):
    """Pi rejected a command."""


class PiRpcClosed(OSError):
    """The Pi process stopped answering."""


EventListener = Callable[[Mapping[str, Any]], None]


class PiRpcClient:
    """Commands and events of one Pi RPC process."""

    def __init__(
        self, *, stdin: IO[bytes], stdout: IO[bytes], process: Any = None,
        request_timeout: float = 30.0, spawn_admission: SpawnAdmission | None = None,
        spawn_receipt: SpawnReceipt | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.process = process
        self.request_timeout = request_timeout
        self._spawn_admission = spawn_admission
        self._spawn_receipt = spawn_receipt
        self._ids = count(1)
        self._lock = Lock()
        self._write_lock = Lock()
        self._pending: dict[str, Queue] = {}
        self._listeners: list[EventListener] = []
        self._closed = False
        self._reader = Thread(target=self._read_loop, name="pi-rpc-reader", daemon=True)
        self._reader.start()

    @classmethod
    def launch(
        cls, command: tuple[str, ...], *, request_timeout: float = 30.0,
        spawn_admission: SpawnAdmission, host_cell_id: str,
    ) -> "PiRpcClient":
        process, receipt = launch_admitted(
            command, spawn_admission=spawn_admission, host_cell_id=host_cell_id,
            options={"stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": None},
        )
        try:
            return cls(
                stdin=process.stdin, stdout=process.stdout, process=process,
                request_timeout=request_timeout, spawn_admission=spawn_admission, spawn_receipt=receipt,
            )
        except BaseException:
            reap(process, term_grace=2.0, kill_reap_timeout=1.0)
            release(spawn_admission, receipt, process)
            raise

    def subscribe(self, listener: EventListener) -> None:
        with self._lock:
            closed = self._closed
            if not closed:
                self._listeners.append(listener)
        if closed:
            listener({"type": TRANSPORT_CLOSED})

    def command(self, kind: str, *, timeout: float | None = None, **fields: Any) -> Mapping[str, Any]:
        """Send one command and return its response data; raise when Pi rejects it."""
        request_id = f"{kind}-{next(self._ids)}"
        answer: Queue = Queue(maxsize=1)
        with self._lock:
            if self._closed:
                raise PiRpcClosed("pi-rpc-closed")
            self._pending[request_id] = answer
        try:
            self._send({"id": request_id, "type": kind, **fields})
            try:
                response = answer.get(timeout=self.request_timeout if timeout is None else timeout)
            except Empty:
                raise TimeoutError(f"pi-rpc-timeout:{kind}") from None
        finally:
            with self._lock:
                self._pending.pop(request_id, None)
        if isinstance(response, BaseException):
            raise response
        if response.get("success") is not True:
            raise PiRpcError(f"pi-rpc-rejected:{kind}:{response.get('error', '')}"[:512])
        data = response.get("data")
        return data if isinstance(data, Mapping) else {}

    def cancel_ui_request(self, request_id: str) -> None:
        """Answer an extension dialog (`extension_ui_request`) as cancelled.

        The answer is an `extension_ui_response` with the request's id; Pi sends
        no command response to it.
        """
        self._send({"type": "extension_ui_response", "id": request_id, "cancelled": True})

    def alive(self) -> bool:
        with self._lock:
            if self._closed:
                return False
        return self.process is None or self.process.poll() is None

    def close(self, *, term_grace: float = 2.0, kill_reap_timeout: float = 1.0) -> None:
        with self._lock:
            already = self._closed
            self._closed = True
        if not already:
            self._fail_pending(PiRpcClosed("pi-rpc-closed"))
        try:
            self.stdin.close()
        except OSError:
            pass
        if self.process is not None:
            reap(self.process, term_grace=term_grace, kill_reap_timeout=kill_reap_timeout)
            release(self._spawn_admission, self._spawn_receipt, self.process)
            self._spawn_receipt = None
        ended_within(self._reader, 1.0)

    def _send(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        try:
            with self._write_lock:  # the reader thread answers dialogs while commands are sent
                self.stdin.write(line)
                self.stdin.flush()
        except (OSError, ValueError) as exc:
            raise PiRpcClosed(f"pi-rpc-write-failed:{exc}") from exc

    def _read_loop(self) -> None:
        try:
            for raw in iter(self.stdout.readline, b""):
                record = self._parse(raw)
                if record is None:
                    continue
                if record.get("type") == "response" and isinstance(record.get("id"), str):
                    with self._lock:
                        waiting = self._pending.get(record["id"])
                    if waiting is not None:
                        waiting.put(record)
                    continue
                for listener in self._listeners_now():
                    listener(record)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._closed = True
                listeners = list(self._listeners)
                self._listeners.clear()
            self._fail_pending(PiRpcClosed("pi-rpc-stdout-closed"))
            for listener in listeners:
                listener({"type": TRANSPORT_CLOSED})

    @staticmethod
    def _parse(raw: bytes) -> Mapping[str, Any] | None:
        line = raw.rstrip(b"\n").rstrip(b"\r")
        if not line:
            return None
        try:
            record = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return record if isinstance(record, Mapping) else None

    def _listeners_now(self) -> list[EventListener]:
        with self._lock:
            return list(self._listeners)

    def _fail_pending(self, error: BaseException) -> None:
        with self._lock:
            waiting = list(self._pending.values())
        for answer in waiting:
            try:
                answer.put_nowait(error)
            except Exception:
                pass
