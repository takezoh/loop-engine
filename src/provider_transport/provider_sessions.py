"""The provider lifecycle over one wire protocol: the only `ProviderAdapter`.

`ProviderSessions` runs each turn on its own worker, returns a bounded startup
receipt once the turn's session binds, records terminal evidence, unloads the
session after its turn, keeps live shared hosts for later turns and
quarantines turns whose termination it cannot prove. The wire protocol is
reached only through a `ProtocolDriver`; what to answer is decided by the pure
functions of `provider_lifecycle`.
"""
from __future__ import annotations

from datetime import UTC, datetime
from threading import Event, RLock, Thread
from time import monotonic
from typing import Hashable, Mapping

from .bounded_wait import ended_within, set_within
from .cwd_authority import SpawnAdmission
from .provider_request import ActionRequest
from .provider_lifecycle import (
    continuation_refusal,
    continuation_spawns,
    failure_reason,
    joined_diagnostics,
    release_status,
    running_receipt,
    terminal_view,
    turn_evidence,
    unload_status,
)
from .provider_port import (
    ExecutionRef,
    HostSlot,
    ProtocolDriver,
    ProviderCleanupFailure,
    ProviderHost,
    ProviderStartupTimeout,
    SessionHandle,
    TerminalObserver,
    TurnEnd,
    TurnSpec,
)
from .turn_result import TurnProgress


class _Turn:
    """One turn running on a worker owned by `ProviderSessions`."""

    def __init__(
        self, request: ActionRequest, execution_id: str | None, host_key: Hashable
    ) -> None:
        self.request = request
        self.operation_id = request.operation_id
        self.execution_id = execution_id
        self.host_key = host_key
        self.session: SessionHandle | None = None
        self.created_host: ProviderHost | None = None
        self.end: TurnEnd | None = None
        self.error: BaseException | None = None
        self.started = Event()
        self.done = Event()
        self.thread: Thread | None = None
        self.cancel_requested = False

    @property
    def session_id(self) -> str | None:
        return self.session.session_id if self.session is not None else None

    @property
    def identity(self) -> str:
        return f"{self.session_id or self.operation_id}:{self.operation_id}"


class ProviderSessions:
    """Turns, sessions and hosts of one provider, over its `ProtocolDriver`."""

    TERMINATION_JOIN_TIMEOUT = 5.0

    def __init__(self, driver: ProtocolDriver, *, cell_id: str) -> None:
        self.driver = driver
        self.cell_id = cell_id
        self._lock = RLock()
        self._hosts: dict[Hashable, ProviderHost] = {}
        self._session_hosts: dict[str, Hashable] = {}
        self._loaded: dict[str, SessionHandle] = {}
        # Sessions whose latest turn ran on a shared host launched before it.
        self._reused_host: set[str] = set()
        self._durable: dict[str, str] = {}
        self._cwds: dict[str, str] = {}
        self._execution_sessions: dict[str, str] = {}
        self._session_turns: dict[str, _Turn] = {}
        self._execution_turns: dict[str, _Turn] = {}
        self._operation_turns: dict[str, _Turn] = {}
        self._operation_executions: dict[str, str] = {}
        self._unloads: dict[str, Event] = {}
        self._unload_errors: dict[str, str] = {}
        self._quarantined: dict[str, _Turn] = {}
        # Hosts whose close failed: never reused, closed again at shutdown.
        self._orphaned: list[ProviderHost] = []
        self._terminal_observer: TerminalObserver | None = None

    # The application's view of turns and sessions.

    def set_terminal_observer(self, observer: TerminalObserver) -> None:
        self._terminal_observer = observer

    def bind_pending_execution(self, execution_id: str, operation_id: str) -> None:
        with self._lock:
            self._operation_executions[operation_id] = execution_id
            turn = self._operation_turns.get(operation_id)
            if turn is not None:
                turn.execution_id = execution_id
                self._execution_turns[execution_id] = turn

    def bind_execution(self, execution_id: str, session_id: str | None) -> None:
        if session_id is None:
            raise ValueError(f"{self.driver.transport}-session-binding-missing")
        with self._lock:
            self._execution_sessions[execution_id] = session_id
            turn = self._session_turns.get(session_id) or self._execution_turns.get(execution_id)
            if turn is not None:
                turn.execution_id = execution_id
                self._execution_turns[execution_id] = turn
        if turn is not None:
            self._record_terminal(turn)

    def recover_binding(self, execution_id: str, session_id: str) -> bool:
        with self._lock:
            self._execution_sessions[execution_id] = session_id
            self._durable.setdefault(session_id, session_id)
        return True

    def uses_shared_host(self, session_id: object) -> bool:
        """Whether the session's latest turn ran on a shared host it did not launch."""
        with self._lock:
            return isinstance(session_id, str) and session_id in self._reused_host

    def observed_turn(self, execution_id: str) -> str | None:
        """The operation of the turn this host holds for the execution, else None."""
        turn = self._turn(execution_id)
        return turn.operation_id if turn is not None else None

    def turn_running(self, execution_id: str) -> bool:
        turn = self._turn(execution_id)
        return turn is not None and not turn.done.is_set()

    def wait_turn(self, execution_id: str, timeout: float) -> bool:
        """Block until the execution's current turn ends; True when none is running."""
        turn = self._turn(execution_id)
        return turn is None or set_within(turn.done, timeout)

    def wait_release(self, execution_id: str) -> Mapping[str, object]:
        """Block, within the driver's unload bound, until the session of the execution's ended turn is released.

        `unload_status` is `completed` once the driver released the session (or its host is gone),
        `pending` while the turn still runs or the unload has not finished, and `failed` when the
        unload failed; `cleanup` then says why.
        """
        turn = self._turn(execution_id)
        with self._lock:
            session_id = turn.session_id if turn is not None else self._execution_sessions.get(execution_id)
        running = turn is not None and not turn.done.is_set()
        if turn is not None and turn.session is not None and not running:
            self._schedule_unload(turn.session)  # the worker may not have handed the ended turn over yet
            with self._lock:
                event = self._unloads.get(turn.session.session_id)
            if event is not None:
                set_within(event, self.driver.traits.unload_timeout)
        with self._lock:
            event = self._unloads.get(session_id) if session_id is not None else None
            error = self._unload_errors.get(session_id) if session_id is not None else None
        status, reason = release_status(
            turn_running=running, scheduled=event is not None,
            done=event is not None and event.is_set(), error=error,
        )
        return {"session_id": session_id, "unload_status": status, **({"cleanup": reason} if reason else {})}

    def continuation_needs_spawn(self, execution_id: str) -> bool:
        self._reap_dead_hosts()
        running = self.turn_running(execution_id)
        with self._lock:
            session_id = self._execution_sessions.get(execution_id)
            unloading = self._unloads.get(session_id) if session_id is not None else None
            key = self._session_hosts.get(session_id) if session_id is not None else None
            host = self._hosts.get(key) if key is not None else None
            return continuation_spawns(
                known=session_id is not None,
                quarantined=execution_id in self._quarantined,
                turn_running=running,
                unloading=unloading is not None and not unloading.is_set(),
                loaded_on_live_host=session_id in self._loaded and host is not None and host.alive(),
            )

    # Actions.

    def start(self, request: ActionRequest, admission: SpawnAdmission) -> Mapping[str, object]:
        self._reap_dead_hosts()
        with self._lock:
            execution_id = self._operation_executions.get(request.operation_id)
        return self._begin(request, admission, None, execution_id)

    def action(
        self, request: ActionRequest, snapshot: ExecutionRef,
        admission: SpawnAdmission | None = None,
    ) -> Mapping[str, object]:
        self._reap_dead_hosts()
        execution_id = snapshot.execution_id
        if request.action == "continue":
            return self._continue(request, execution_id, admission)
        if request.action == "observe":
            return self._observe(execution_id)
        if request.action == "cancel":
            return self._cancel_action(execution_id)
        if request.action in {"close", "reconcile"}:
            return self._close(execution_id)
        raise ValueError("unsupported-provider-action")

    def interrupt(self, request: ActionRequest, snapshot: ExecutionRef) -> Mapping[str, object]:
        transport = self.driver.transport
        with self._lock:
            session_id = self._execution_sessions.get(snapshot.execution_id)
        turn = self._turn(snapshot.execution_id)
        if turn is None or turn.session is None or turn.done.is_set():
            return {"receipt_ref": f"{transport}:no-active-turn:{session_id}"}
        failure = self._cancel(turn)
        if failure is not None:
            raise RuntimeError(f"{transport}-interrupt-failed") from failure
        return {"receipt_ref": f"{transport}:interrupt:{turn.session_id}:{turn.operation_id}"}

    def shutdown(self) -> None:
        with self._lock:
            turns: list[_Turn] = []
            for turn in (
                *self._session_turns.values(), *self._execution_turns.values(),
                *self._operation_turns.values(),
            ):
                if turn not in turns:
                    turns.append(turn)
            self._session_turns.clear()
            self._execution_turns.clear()
            self._operation_turns.clear()
            self._operation_executions.clear()
            self._execution_sessions.clear()
        for turn in turns:
            if self._terminate(turn) is not None:
                # Keep an unproven turn so the host lease stays fail closed.
                self._quarantine(turn)
        # Release every loaded session before any host is closed: closing a host ends its
        # processes by signal, which is the last resort only.
        with self._lock:
            loaded = tuple(self._loaded.values())
        for session in loaded:
            self._schedule_unload(session)
        with self._lock:
            unloads = tuple(self._unloads.values())
        deadline = monotonic() + self.driver.traits.unload_timeout
        for unloading in unloads:
            set_within(unloading, deadline - monotonic())
        with self._lock:
            hosts = (*self._hosts.values(), *self._orphaned)
            self._hosts.clear()
            self._orphaned.clear()
            self._session_hosts.clear()
            self._loaded.clear()
            self._cwds.clear()
        for host in hosts:
            try:
                host.close()
            except Exception:
                pass

    # Turn lifecycle.

    def _begin(
        self, request: ActionRequest, admission: SpawnAdmission | None,
        session_id: str | None, execution_id: str | None,
    ) -> Mapping[str, object]:
        with self._lock:
            cwd = self._cwds.get(session_id) if session_id is not None else None
            durable_id = self._durable.get(session_id, session_id) if session_id is not None else None
        if cwd is None:
            if admission is None:
                return {"outcome": "recovery_pending", "session_id": session_id}
            cwd = admission.cwd_lease.admitted_path()
        key = self.driver.host_key(request)
        with self._lock:
            host = self._hosts.get(key) if self.driver.traits.shared_host else None
            if host is None and admission is None:
                return {"outcome": "recovery_pending", "session_id": session_id}
            turn = _Turn(request, execution_id, key)
            self._operation_turns[turn.operation_id] = turn
            if execution_id is not None:
                self._execution_turns[execution_id] = turn

        def adopt(launched: ProviderHost) -> None:
            turn.created_host = launched
            if self.driver.traits.shared_host:
                with self._lock:
                    self._hosts[key] = launched

        def on_started(session: SessionHandle) -> None:
            with self._lock:
                turn.session = session
                bound = session.session_id
                self._session_turns[bound] = turn
                self._loaded[bound] = session
                self._session_hosts[bound] = key
                self._cwds[bound] = cwd
                self._durable[bound] = session.durable_id
                self._unloads.pop(bound, None)
                self._unload_errors.pop(bound, None)
                if self.driver.traits.shared_host and turn.created_host is None:
                    self._reused_host.add(bound)
                else:
                    self._reused_host.discard(bound)
                if turn.execution_id is not None:
                    self._execution_sessions[turn.execution_id] = bound
            turn.started.set()

        slot = HostSlot(host, admission if host is None else None, self.cell_id, adopt)
        spec = TurnSpec(request, cwd, durable_id)

        def work() -> None:
            try:
                turn.end = self.driver.run_turn(spec, slot, on_started)
            except BaseException as exc:  # observed as terminal evidence
                turn.error = exc
            finally:
                turn.done.set()
                turn.started.set()
                self._record_terminal(turn)

        turn.thread = Thread(
            target=work, name=f"{self.driver.transport}-turn-{turn.operation_id}", daemon=True
        )
        turn.thread.start()
        set_within(turn.started, self.driver.traits.startup_timeout)
        if turn.session is None:
            self._startup_failed(turn)
        grace = self.driver.traits.startup_grace
        if grace > 0 and set_within(turn.done, grace):
            return self._terminal(turn)
        assert turn.session is not None
        return running_receipt(turn.session)

    def _startup_failed(self, turn: _Turn) -> None:
        """Stop a turn whose session never bound, then raise why."""
        diagnostics = None
        if turn.created_host is not None:
            with self._lock:
                if self._hosts.get(turn.host_key) is turn.created_host:
                    self._hosts.pop(turn.host_key, None)
            try:
                turn.created_host.close()
            except BaseException as exc:
                diagnostics = f"host.close:{failure_reason(exc)}"
                with self._lock:
                    self._orphaned.append(turn.created_host)
        diagnostics = joined_diagnostics(diagnostics, self._terminate(turn))
        primary = turn.error or ProviderStartupTimeout(f"{self.driver.transport}-startup-receipt-timeout")
        if diagnostics is not None:
            self._quarantine(turn)
            raise ProviderCleanupFailure(primary, diagnostics)
        with self._lock:
            self._operation_turns.pop(turn.operation_id, None)
            if turn.execution_id is not None and self._execution_turns.get(turn.execution_id) is turn:
                self._execution_turns.pop(turn.execution_id, None)
        raise primary

    def _continue(
        self, request: ActionRequest, execution_id: str, admission: SpawnAdmission | None
    ) -> Mapping[str, object]:
        with self._lock:
            session_id = self._execution_sessions.get(execution_id)
            unloading = self._unloads.get(session_id) if session_id is not None else None
        unload = "none"
        if unloading is not None:
            unload = "completed" if set_within(unloading, self.driver.traits.unload_timeout) else "pending"
        with self._lock:
            unload_error = self._unload_errors.get(session_id) if session_id is not None else None
            quarantined = execution_id in self._quarantined
        if unload_error is not None:
            unload = "failed"
        refusal = continuation_refusal(
            session_id=session_id,
            quarantined=quarantined,
            turn_running=self.turn_running(execution_id),
            unload=unload,
            unload_error=unload_error,
            needs_spawn=self.continuation_needs_spawn(execution_id),
            admitted=admission is not None,
        )
        if refusal is not None:
            return refusal
        return self._begin(request, admission, session_id, execution_id)

    def _observe(self, execution_id: str) -> Mapping[str, object]:
        turn = self._turn(execution_id)
        if turn is None:
            with self._lock:
                session_id = self._execution_sessions.get(execution_id)
            return {"outcome": "session_lost", "session_id": session_id}
        with self._lock:
            turn.execution_id = execution_id
            self._execution_turns[execution_id] = turn
        if not turn.done.is_set():
            assert turn.session is not None
            try:
                progress = self.driver.progress(turn.session)
            except Exception:
                progress = TurnProgress("", None)
            return {
                "outcome": "running",
                "session_id": turn.session_id,
                "provider_status": "running",
                "output": progress.output,
                "activity_at": (
                    datetime.fromtimestamp(progress.activity_at, UTC).isoformat(timespec="seconds")
                    if progress.activity_at is not None else None
                ),
            }
        return self._terminal(turn)

    def _cancel_action(self, execution_id: str) -> Mapping[str, object]:
        with self._lock:
            session_id = self._execution_sessions.get(execution_id)
        turn = self._turn(execution_id)
        if turn is None or turn.session is None or turn.done.is_set():
            return {"outcome": "cancel_reserved", "session_id": session_id}
        if self._cancel(turn) is not None:
            return {"outcome": "recovery_pending", "session_id": session_id}
        return {"outcome": "cancel_reserved", "session_id": session_id}

    def _close(self, execution_id: str) -> Mapping[str, object]:
        with self._lock:
            session_id = self._execution_sessions.get(execution_id)
        turn = self._turn(execution_id)
        diagnostics = self._terminate(turn) if turn is not None else None
        if diagnostics is not None:
            assert turn is not None
            self._quarantine(turn)
            return {"outcome": "cleanup_failed", "session_id": session_id, "cleanup": diagnostics}
        if turn is not None and turn.done.is_set():
            self._record_terminal(turn)
        if session_id is not None:
            with self._lock:
                session = self._loaded.get(session_id)
            if session is not None:
                self._schedule_unload(session)
        status, error = self._unload_status(session_id)
        with self._lock:
            self._execution_sessions.pop(execution_id, None)
            self._execution_turns.pop(execution_id, None)
            if turn is not None:
                self._operation_turns.pop(turn.operation_id, None)
                self._operation_executions.pop(turn.operation_id, None)
                if turn.session_id is not None and self._session_turns.get(turn.session_id) is turn:
                    self._session_turns.pop(turn.session_id, None)
            if session_id is not None:
                # The unload record stays: a later load of the session still waits for it.
                self._cwds.pop(session_id, None)
                self._durable.pop(session_id, None)
                self._reused_host.discard(session_id)
        return {
            "outcome": "completed", "session_id": session_id,
            "unload_status": status, **({"cleanup": error} if error else {}),
        }

    def _turn(self, execution_id: str) -> _Turn | None:
        with self._lock:
            turn = self._execution_turns.get(execution_id)
            if turn is not None:
                return turn
            session_id = self._execution_sessions.get(execution_id)
            return self._session_turns.get(session_id) if session_id is not None else None

    def _terminal(self, turn: _Turn) -> Mapping[str, object]:
        self._record_terminal(turn)
        return terminal_view(session=turn.session, end=turn.end, error=turn.error, identity=turn.identity)

    def _record_terminal(self, turn: _Turn) -> None:
        """Hand the ended turn's evidence to the application, then unload its session."""
        if not turn.done.is_set():
            return
        observer = self._terminal_observer
        if observer is not None and turn.execution_id is not None:
            view = terminal_view(session=turn.session, end=turn.end, error=turn.error, identity=turn.identity)
            try:
                observer(
                    turn.execution_id, turn.session_id,
                    turn_evidence(view, operation_id=turn.operation_id, fallback_identity=turn.identity),
                )
            except Exception:
                # Evidence is best effort here; a later observe or close records it again.
                pass
        if turn.session is not None:
            self._schedule_unload(turn.session)

    def _cancel(self, turn: _Turn) -> BaseException | None:
        if turn.session is None:
            return None
        with self._lock:
            if turn.cancel_requested:
                return None
            turn.cancel_requested = True
        try:
            self.driver.cancel(turn.session)
        except BaseException as exc:
            with self._lock:
                turn.cancel_requested = False
            return exc
        return None

    def _terminate(self, turn: _Turn) -> str | None:
        """Cancel a running turn and join its worker; diagnostics when its end is unproven."""
        diagnostics: list[str] = []
        if turn.session is not None and not turn.done.is_set():
            failure = self._cancel(turn)
            if failure is not None:
                diagnostics.append(f"turn.cancel:{failure_reason(failure)}")
        if not set_within(turn.done, self.TERMINATION_JOIN_TIMEOUT):
            diagnostics.append("turn-worker-timeout")
        if turn.thread is not None and not ended_within(turn.thread, self.TERMINATION_JOIN_TIMEOUT):
            diagnostics.append("turn-worker-alive")
        return joined_diagnostics(*diagnostics)

    def _quarantine(self, turn: _Turn) -> None:
        with self._lock:
            self._quarantined[turn.execution_id or turn.session_id or turn.operation_id] = turn

    # Sessions and hosts.

    def _schedule_unload(self, session: SessionHandle) -> None:
        session_id = session.session_id
        with self._lock:
            if session_id in self._unloads or session_id not in self._loaded:
                return
            completed = Event()
            self._unloads[session_id] = completed
            key = self._session_hosts.get(session_id)

        def unload() -> None:
            try:
                self.driver.release(session, self.driver.traits.unload_timeout)
            except Exception as exc:
                with self._lock:
                    host = self._hosts.get(key) if key is not None else None
                if self.driver.traits.shared_host and (host is None or not host.alive()):
                    # The host is gone with its sessions; the durable identity still reloads.
                    self._unloaded(session_id)
                else:
                    with self._lock:
                        self._unload_errors[session_id] = failure_reason(exc)
            else:
                self._unloaded(session_id)
            finally:
                completed.set()

        Thread(target=unload, name=f"{self.driver.transport}-unload-{session_id}", daemon=True).start()

    def _unloaded(self, session_id: str) -> None:
        with self._lock:
            self._loaded.pop(session_id, None)
            self._session_hosts.pop(session_id, None)

    def _unload_status(self, session_id: str | None) -> tuple[str, str | None]:
        with self._lock:
            event = self._unloads.get(session_id) if session_id is not None else None
            error = self._unload_errors.get(session_id) if session_id is not None else None
        return unload_status(
            scheduled=event is not None, done=event is not None and event.is_set(), error=error
        )

    def _reap_dead_hosts(self) -> None:
        """Forget hosts that died; their sessions stay reloadable by durable identity."""
        with self._lock:
            dead = [(key, host) for key, host in self._hosts.items() if not host.alive()]
            for key, _host in dead:
                self._hosts.pop(key, None)
                for session_id, session_key in tuple(self._session_hosts.items()):
                    if session_key == key:
                        self._session_hosts.pop(session_id, None)
                        self._loaded.pop(session_id, None)
        for _key, host in dead:
            try:
                host.close()
            except Exception:
                pass
