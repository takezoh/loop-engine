"""Small initialized JSONL RPC client for an app-server provider command."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import json
from queue import Empty, Queue
import subprocess
from threading import Condition, Lock, Thread
import time
from typing import IO, Any

from .admitted_process import launch_admitted, reap, release
from .bounded_wait import ended_within
from .cwd_authority import SpawnAdmission, SpawnReceipt

from .turn_result import (
    LOSS_INTERNAL_CONTRACT_VIOLATION,
    LOSS_MALFORMED_FRAME,
    LOSS_TRANSPORT_EOF,
)


# Distinguishes "no caller preference, use the client default" from an explicit
# request to wait without a deadline. A model turn has no known duration, so its
# request must not inherit the bounded control-operation timeout.
_UNSET = object()
NO_TIMEOUT = object()


class JsonlRpcClient:
    def __init__(
        self,
        *,
        stdin: IO[str],
        stdout: IO[str],
        stderr: IO[str] | None = None,
        process: subprocess.Popen[str] | None = None,
        request_timeout: float = 30.0,
        server_request_handler: Callable[
            [str, Mapping[str, object]], Mapping[str, object]
        ]
        | None = None,
        notification_allowlist: frozenset[str] | None = None,
        notification_handler: Callable[[Mapping[str, object]], None] | None = None,
        active_turn_loss_handler: Callable[[str], None] | None = None,
        notification_sink: Callable[[Mapping[str, object]], bool] | None = None,
        spawn_admission: SpawnAdmission | None = None,
        spawn_receipt: SpawnReceipt | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.process = process
        self.request_timeout = request_timeout
        self.server_request_handler = server_request_handler
        self.notification_allowlist = notification_allowlist
        self.notification_handler = notification_handler
        self.active_turn_loss_handler = active_turn_loss_handler
        self.notification_sink = notification_sink
        self._spawn_admission = spawn_admission
        self._spawn_receipt = spawn_receipt
        self.initialize_result: Mapping[str, object] | None = None
        self._write_lock = Lock()
        self._next_id = 0
        self._pending: dict[
            int, Queue[Mapping[str, object] | BaseException]
        ] = {}
        self._notifications: list[Mapping[str, object]] = []
        self._condition = Condition()
        self._terminal_error: OSError | None = None
        self._closed = False
        self._reader = Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        self._stderr_chunks: list[str] = []
        self._stderr_bytes = 0
        self._stderr_lock = Lock()
        self._stderr_reader = (
            Thread(target=self._read_stderr_loop, daemon=True)
            if stderr is not None else None
        )
        if self._stderr_reader is not None:
            self._stderr_reader.start()

    @classmethod
    def launch(
        cls,
        command: tuple[str, ...],
        *,
        request_timeout: float = 30.0,
        spawn_admission: SpawnAdmission,
        host_cell_id: str,
    ) -> "JsonlRpcClient":
        process, spawn_receipt = launch_admitted(
            command,
            spawn_admission=spawn_admission,
            host_cell_id=host_cell_id,
            options={
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": None,
                "text": True,
                "encoding": "utf-8",
                "bufsize": 1,
            },
        )
        client: JsonlRpcClient | None = None
        try:
            assert process.stdin is not None and process.stdout is not None
            client = cls(
                stdin=process.stdin,
                stdout=process.stdout,
                process=process,
                request_timeout=request_timeout,
                spawn_admission=spawn_admission,
                spawn_receipt=spawn_receipt,
            )
            client.initialize()
            return client
        except BaseException:
            try:
                if client is not None:
                    client.close()
                else:
                    if process.stdin is not None:
                        process.stdin.close()
                    reap(process, term_grace=2.0, kill_reap_timeout=1.0)
            finally:
                raise

    def initialize(self) -> Mapping[str, object]:
        result = self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "loop_engine",
                    "title": "Loop Engine",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [
                        "item/reasoning/textDelta",
                        "item/commandExecution/outputDelta",
                    ]
                },
            },
        )
        self.initialize_result = result
        self.notify("initialized", {})
        return result

    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: object = _UNSET,
    ) -> Mapping[str, object]:
        with self._write_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue: Queue[Mapping[str, object] | BaseException] = Queue(
                maxsize=1
            )
            self._pending[request_id] = response_queue
            with self._condition:
                if self._terminal_error is not None:
                    self._pending.pop(request_id, None)
                    raise self._terminal_error
            try:
                self._send(
                    {"method": method, "id": request_id, "params": dict(params)}
                )
            except OSError as exc:
                self._transition_terminal(exc)
                raise
        effective_timeout = self._effective_timeout(timeout)
        try:
            response = response_queue.get(timeout=effective_timeout)
        except Empty as exc:
            with self._condition:
                self._pending.pop(request_id, None)
                terminal_error = self._terminal_error
            if terminal_error is not None:
                raise terminal_error
            raise TimeoutError(f"app-server request timed out: {method}") from exc
        if isinstance(response, BaseException):
            raise response
        if "error" in response:
            error = response["error"]
            raise RuntimeError(f"app-server {method} failed: {error}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise ValueError(f"app-server {method} returned a non-object result")
        return result

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        with self._write_lock:
            with self._condition:
                if self._terminal_error is not None:
                    raise self._terminal_error
            try:
                self._send({"method": method, "params": dict(params)})
            except OSError as exc:
                self._transition_terminal(exc)
                raise

    def wait_notification(
        self,
        method: str,
        *,
        predicate: Callable[[Mapping[str, object]], bool] = lambda _: True,
        timeout: object = _UNSET,
    ) -> Mapping[str, object]:
        effective = self._effective_timeout(timeout)
        deadline = (
            None if effective is None else time.monotonic() + effective
        )
        with self._condition:
            while True:
                for index, message in enumerate(self._notifications):
                    if message.get("method") != method:
                        continue
                    params = message.get("params")
                    if isinstance(params, Mapping) and predicate(params):
                        self._notifications.pop(index)
                        return params
                if self._terminal_error is not None:
                    raise self._terminal_error
                if deadline is None:
                    self._condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"app-server notification timed out: {method}")
                self._condition.wait(remaining)

    def _effective_timeout(self, timeout: object) -> float | None:
        if timeout is _UNSET:
            return self.request_timeout
        if timeout is NO_TIMEOUT:
            return None
        return timeout  # type: ignore[return-value]

    def drain_notifications(
        self,
        method: str,
        *,
        predicate: Callable[[Mapping[str, object]], bool] = lambda _: True,
    ) -> list[Mapping[str, object]]:
        """Remove and return currently queued matching notification params in read order."""
        drained: list[Mapping[str, object]] = []
        with self._condition:
            retained: list[Mapping[str, object]] = []
            for message in self._notifications:
                params = message.get("params")
                if (
                    message.get("method") == method
                    and isinstance(params, Mapping)
                    and predicate(params)
                ):
                    drained.append(params)
                else:
                    retained.append(message)
            self._notifications = retained
        return drained

    def close(
        self, *, term_grace: float = 2.0, kill_reap_timeout: float = 1.0
    ) -> None:
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._transition_terminal_locked(OSError("app-server client closed"))
        try:
            self.stdin.close()
        finally:
            if self.process is not None:
                reap(self.process, term_grace=term_grace, kill_reap_timeout=kill_reap_timeout)
                release(self._spawn_admission, self._spawn_receipt, self.process)
            close_stdout = getattr(self.stdout, "close", None)
            if callable(close_stdout):
                close_stdout()
            if self.stderr is not None:
                close_stderr = getattr(self.stderr, "close", None)
                if callable(close_stderr):
                    close_stderr()
            ended_within(self._reader, 1.0)
            if self._stderr_reader is not None:
                ended_within(self._stderr_reader, 1.0)

    def stderr_text(self) -> str:
        with self._stderr_lock:
            return "".join(self._stderr_chunks)

    def _read_stderr_loop(self) -> None:
        assert self.stderr is not None
        for line in self.stderr:
            encoded = line.encode("utf-8", errors="replace")
            with self._stderr_lock:
                remaining = 65_536 - self._stderr_bytes
                if remaining <= 0:
                    continue
                kept = encoded[:remaining].decode("utf-8", errors="replace")
                self._stderr_chunks.append(kept)
                self._stderr_bytes += len(kept.encode("utf-8"))

    def _send(self, message: Mapping[str, Any]) -> None:
        envelope = {"jsonrpc": "2.0", **message}
        self.stdin.write(json.dumps(envelope, separators=(",", ":")) + "\n")
        self.stdin.flush()

    def _read_loop(self) -> None:
        try:
            for line in self.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    if self.active_turn_loss_handler is not None:
                        self.active_turn_loss_handler(LOSS_MALFORMED_FRAME)
                    continue
                if not isinstance(message, Mapping):
                    if self.active_turn_loss_handler is not None:
                        self.active_turn_loss_handler(LOSS_MALFORMED_FRAME)
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int) and (
                    "result" in message or "error" in message
                ):
                    with self._condition:
                        pending = self._pending.pop(request_id, None)
                    if pending is not None:
                        pending.put(message)
                    continue
                if isinstance(request_id, int) and "method" in message:
                    self._handle_server_request(
                        request_id, str(message["method"]), message.get("params")
                    )
                    continue
                sink = self.notification_sink
                if sink is not None:
                    try:
                        consumed = sink(message)
                    except Exception:
                        if self.active_turn_loss_handler is not None:
                            self.active_turn_loss_handler(
                                LOSS_INTERNAL_CONTRACT_VIOLATION
                            )
                        continue
                    if consumed:
                        continue
                if (
                    self.notification_allowlist is not None
                    and message.get("method") not in self.notification_allowlist
                ):
                    continue
                if self.notification_handler is not None:
                    self.notification_handler(message)
                    continue
                with self._condition:
                    self._notifications.append(message)
                    self._condition.notify_all()
        finally:
            try:
                if self.active_turn_loss_handler is not None:
                    self.active_turn_loss_handler(LOSS_TRANSPORT_EOF)
            finally:
                self._transition_terminal(OSError("app-server stdout reached EOF"))

    def _transition_terminal(self, error: OSError) -> None:
        with self._condition:
            self._transition_terminal_locked(error)

    def _transition_terminal_locked(self, error: OSError) -> None:
        if self._terminal_error is not None:
            return
        self._terminal_error = error
        pending = list(self._pending.values())
        self._pending.clear()
        for response_queue in pending:
            response_queue.put(error)
        self._condition.notify_all()

    def _handle_server_request(
        self, request_id: int, method: str, raw_params: object
    ) -> None:
        if self.server_request_handler is not None and isinstance(raw_params, Mapping):
            try:
                result = self.server_request_handler(method, raw_params)
            except PermissionError as exc:
                self._send_server_error(request_id, -32001, str(exc))
                return
            except NotImplementedError as exc:
                self._send_server_error(request_id, -32601, str(exc))
                return
            except (OSError, ValueError) as exc:
                self._send_server_error(
                    request_id, -32602, f"Invalid server request: {exc}"
                )
                return
            with self._write_lock:
                self._send({"id": request_id, "result": dict(result)})
            return
        self._send_server_error(
            request_id, -32601, f"Unsupported server request: {method}"
        )

    def _send_server_error(self, request_id: int, code: int, message: str) -> None:
        with self._write_lock:
            self._send(
                {
                    "id": request_id,
                    "error": {
                        "code": code,
                        "message": message,
                    },
                }
            )
