"""Ports between the caller of provider sessions, the provider lifecycle and each wire protocol.

`ProviderAdapter` is everything a caller uses from one provider.
`ProviderSessions` is its only implementation: it owns turns, workers, hosts,
unload and quarantine once for every provider. What differs
between wire protocols sits behind `ProtocolDriver`, one per transport kind
that providers.json declares.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Hashable, Mapping, Protocol

from .cwd_authority import SpawnAdmission
from .provider_request import ActionRequest
from .turn_result import TurnProgress


TerminalObserver = Callable[[str, "str | None", Mapping[str, object]], None]


class ExecutionRef(Protocol):
    """The caller's execution an action is about."""

    execution_id: str


class ProviderCleanupFailure(RuntimeError):
    """A provider operation failed and its process cleanup also failed."""

    def __init__(
        self,
        primary: BaseException,
        cleanup_diagnostics: str,
        *,
        cleanup_failed: bool = True,
    ) -> None:
        self.primary = primary
        self.cleanup_diagnostics = cleanup_diagnostics
        self.cleanup_failed = cleanup_failed
        super().__init__(str(primary))


class ProviderStartupTimeout(RuntimeError):
    """No session bound within the driver's startup timeout."""


class ProviderAdapter(Protocol):
    """What a caller uses from one provider."""

    def start(self, request: ActionRequest, admission: SpawnAdmission) -> Mapping[str, object]: ...

    def action(
        self, request: ActionRequest, snapshot: ExecutionRef,
        admission: SpawnAdmission | None = None,
    ) -> Mapping[str, object]: ...

    def interrupt(self, request: ActionRequest, snapshot: ExecutionRef) -> Mapping[str, object]: ...

    def shutdown(self) -> None: ...

    def set_terminal_observer(self, observer: TerminalObserver) -> None: ...

    def bind_pending_execution(self, execution_id: str, operation_id: str) -> None: ...

    def bind_execution(self, execution_id: str, session_id: str | None) -> None: ...

    def recover_binding(self, execution_id: str, session_id: str) -> bool: ...

    def continuation_needs_spawn(self, execution_id: str) -> bool: ...

    def uses_shared_host(self, session_id: object) -> bool: ...

    def observed_turn(self, execution_id: str) -> str | None: ...

    def turn_running(self, execution_id: str) -> bool: ...

    def wait_turn(self, execution_id: str, timeout: float) -> bool: ...

    def wait_release(self, execution_id: str) -> Mapping[str, object]: ...


class ProviderHost(Protocol):
    """One launched provider process."""

    def alive(self) -> bool: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class DriverTraits:
    """How a wire protocol's hosts and sessions behave.

    `shared_host`: one host process serves several sessions and outlives them.
    `startup_timeout`: how long a start or continue waits for its session to bind.
    `startup_grace`: how long it then waits for a turn that ends at once.
    `unload_timeout`: how long unloading a session, or a continuation waiting
    for that unload, may take.
    """

    shared_host: bool
    startup_timeout: float
    startup_grace: float
    unload_timeout: float


@dataclass(frozen=True)
class TurnSpec:
    """One turn to run: the request, its working folder and the session it continues."""

    request: ActionRequest
    cwd: str
    durable_id: str | None


@dataclass
class SessionHandle:
    """A bound provider session. `session_id` keys it; `durable_id` reloads it in a later host.

    `state` is the driver's own per-session state; only the driver reads it.
    """

    session_id: str
    durable_id: str
    binding_proof: Mapping[str, object] | None = None
    state: object = None


@dataclass(frozen=True)
class TurnEnd:
    """How a turn ended, in the provider's terms.

    `completed` is the provider's own verdict; incomplete communication
    evidence stays inside `turn_result`. `cleanup` names a provider cleanup
    failure. `detail` carries protocol fields kept in the terminal view.
    """

    completed: bool
    provider_status: str
    turn_result: Mapping[str, object] | None
    cleanup: str | None = None
    detail: Mapping[str, object] = field(default_factory=dict)


class HostSlot:
    """The host a turn runs on: a live one to reuse, or the admission to launch one.

    A driver that launches a host passes it to `adopt` at once, before binding a
    session, so the lifecycle can stop the exact process if binding never ends.
    """

    def __init__(
        self, host: ProviderHost | None, admission: SpawnAdmission | None,
        cell_id: str, adopt: Callable[[ProviderHost], None],
    ) -> None:
        self.host = host
        self.admission = admission
        self.cell_id = cell_id
        self._adopt = adopt

    def adopt(self, host: ProviderHost) -> None:
        self.host = host
        self._adopt(host)

    def launch_admission(self) -> SpawnAdmission:
        """The admission to launch a host with; a slot without a live host always carries one."""
        if self.admission is None:
            raise RuntimeError("spawn-admission-required")
        return self.admission


class ProtocolDriver(Protocol):
    """One wire protocol's way to launch hosts, bind sessions and run turns."""

    transport: str
    traits: DriverTraits

    def host_key(self, request: ActionRequest) -> Hashable:
        """The host a turn of this request needs; drivers without shared hosts may return any value."""
        ...

    def run_turn(
        self, turn: TurnSpec, slot: HostSlot, started: Callable[[SessionHandle], None]
    ) -> TurnEnd:
        """Bind a new or durable session, call `started`, then run the turn to its end.

        Launches a host through `slot.admission` when `slot.host` is None. An
        error raised before `started` means no session bound; one raised after
        it ends that turn.
        """
        ...

    def progress(self, session: SessionHandle) -> TurnProgress:
        """The running turn's bounded text and the time its provider last reported any event."""
        ...

    def cancel(self, session: SessionHandle) -> None: ...

    def release(self, session: SessionHandle, timeout: float) -> None:
        """Unload the session; a driver without shared hosts also ends the session's host."""
        ...
