"""The single fake of the protocol driver port that provider lifecycle tests use.

`test_protocol_drivers.py` runs the shared driver contract against it and
against each production driver. A test scripts each turn: whether binding
fails, how the turn ends and whether it waits for the test to release it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import count
from threading import Event
from types import SimpleNamespace

from provider_transport.provider_port import DriverTraits, SessionHandle, TurnEnd
from provider_transport.turn_result import TurnProgress


class FakeHost:
    def __init__(self, number: int, log: list | None = None) -> None:
        self.number = number
        self.dead = False
        self.closed = False
        self.close_error: BaseException | None = None
        self.log = log if log is not None else []

    def alive(self) -> bool:
        return not self.dead and not self.closed

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True
        self.log.append(("closed", self.number))


@dataclass
class TurnScript:
    """How one turn behaves.

    `bind_error` fails before the session binds; `hold` keeps the turn running
    until `release` is set or the turn is cancelled; `end` is how it ends, and
    `error` an error raised after binding instead.
    """

    bind_error: BaseException | None = None
    hold: bool = False
    end: TurnEnd = field(default_factory=lambda: TurnEnd(True, "completed", {"turn_identity": "fake"}))
    error: BaseException | None = None
    never_bind: bool = False
    ignore_cancel: bool = False
    release: Event = field(default_factory=Event)
    cancelled: Event = field(default_factory=Event)


class FakeAdmission:
    """What the lifecycle reads from a spawn admission: its cwd; drivers mark a spawn."""

    def __init__(self, cwd: str = "/work") -> None:
        self.cwd_lease = SimpleNamespace(admitted_path=lambda: cwd)
        self.spawned = 0


class FakeDriver:
    """A protocol driver whose hosts, sessions and turns are scripted by the test."""

    transport = "fake"

    def __init__(
        self, *, shared_host: bool = True, startup_timeout: float = 2.0,
        startup_grace: float = 0.0, unload_timeout: float = 2.0,
    ) -> None:
        self.traits = DriverTraits(shared_host, startup_timeout, startup_grace, unload_timeout)
        self.scripts: list[TurnScript] = []
        self.hosts: list[FakeHost] = []
        self.turns: list[tuple[str, str | None]] = []
        self.released: list[str] = []
        # Releases and host closes in the order they happened.
        self.log: list[tuple[str, object]] = []
        self.release_error: BaseException | None = None
        self.release_gate: Event | None = None
        self._sessions = count(1)
        self._session_hosts: dict[str, FakeHost] = {}

    def script(self, **values) -> TurnScript:
        turn = TurnScript(**values)
        self.scripts.append(turn)
        return turn

    def host_key(self, request):
        return (request.model, request.effort)

    def run_turn(self, turn, slot, started) -> TurnEnd:
        script = self.scripts.pop(0) if self.scripts else TurnScript()
        if slot.host is None:
            assert slot.admission is not None, "a host launch needs an admission"
            host = FakeHost(len(self.hosts) + 1, self.log)
            self.hosts.append(host)
            slot.admission.spawned += 1
            slot.adopt(host)
        host = slot.host
        if script.never_bind:
            script.cancelled.wait(10)
            raise TimeoutError("never-bound")
        if script.bind_error is not None:
            raise script.bind_error
        session_id = turn.durable_id or f"session-{next(self._sessions)}"
        self.turns.append((session_id, turn.durable_id))
        session = SessionHandle(session_id, session_id, {"provider": "fake", "session_id": session_id}, script)
        self._session_hosts[session_id] = host
        started(session)
        if script.hold:
            while not script.release.wait(0.01):
                if script.cancelled.is_set() and not script.ignore_cancel:
                    return TurnEnd(False, "cancelled", {"turn_identity": f"{session_id}:cancelled"})
        if script.error is not None:
            raise script.error
        return script.end

    def progress(self, session) -> TurnProgress:
        return TurnProgress(f"preview:{session.session_id}", 1794587067.0)

    def cancel(self, session) -> None:
        session.state.cancelled.set()

    def release(self, session, timeout: float) -> None:
        if self.release_gate is not None and not self.release_gate.wait(10):
            raise TimeoutError("release-timeout")
        if self.release_error is not None:
            raise self.release_error
        self.released.append(session.session_id)
        self.log.append(("released", session.session_id))
        if not self.traits.shared_host:
            self._session_hosts[session.session_id].close()
