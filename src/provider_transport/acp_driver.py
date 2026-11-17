"""The Agent Client Protocol driver.

A host is one ACP agent process launched with the catalog argv; it serves
every session of the same argv and parent authority. A new session is
`session/new`, a later one `session/load` of the same session id. The ACP
router binds the session, applies its model and effort and runs the prompt;
this driver keeps the host and session it reports for preview, cancel and
unload.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Callable, Hashable, Mapping, cast

from .acp_client import AcpBinding, AcpMessageCapture, AcpSessionClient, ParentAuthority
from . import acp_router
from .acp_router import AcpRouterResult, AcpStartedTurn
from .provider_request import ActionRequest
from .provider_catalog import CATALOG_PATH, load_provider
from .provider_port import DriverTraits, HostSlot, SessionHandle, TurnEnd, TurnSpec
from .turn_result import TurnProgress


class AcpHost:
    """A launched ACP agent and the message capture its sessions share."""

    def __init__(self, connection: object, capture: AcpMessageCapture) -> None:
        self.connection = connection
        self.capture = capture

    def alive(self) -> bool:
        process = getattr(getattr(self.connection, "rpc", None), "process", None)
        return process is None or process.poll() is None

    def close(self) -> None:
        getattr(self.connection, "rpc").close()


class _AcpSession:
    """The router's client, binding and capture of one bound session."""

    def __init__(self, bound: AcpStartedTurn) -> None:
        self.client = cast(AcpSessionClient, bound.client)
        self.binding = cast(AcpBinding, bound.binding)
        self.capture = cast("AcpMessageCapture | None", bound.message_capture)


def _proof_wire(proof: object | None) -> Mapping[str, object] | None:
    return asdict(proof) if proof is not None and is_dataclass(proof) and not isinstance(proof, type) else None


def turn_end(result: AcpRouterResult, actions: Mapping[str, object] | None = None) -> TurnEnd:
    """How an ACP prompt ended: completed whenever the agent returned a stop reason."""
    failures = tuple(dict(failure) for failure in result.tool_failures)
    status = result.stop_reason or ("cleanup_failed" if result.cleanup_diagnostics else "post_start_failed")
    detail: dict[str, object] = {"tool_failures": [dict(failure) for failure in failures]} if failures else {}
    if actions is not None:
        detail["actions"] = dict(actions)
    return TurnEnd(
        result.stop_reason is not None,
        status,
        getattr(result.turn_result, "to_wire")() if result.turn_result is not None else None,
        cleanup=result.cleanup_diagnostics,
        detail=detail,
    )


class AcpDriver:
    """Binds ACP sessions on shared agent hosts and runs one prompt per turn."""

    transport = "acp-v1"

    def __init__(
        self, *, provider: str, generation: str, authority_root: Path,
        allow_tmp: bool = False, timeout: float = 300.0, startup_timeout: float = 30.0,
        router: Callable[..., AcpRouterResult] | None = None,
        exclude: object = None,
    ) -> None:
        self.provider = provider
        self.exclude = exclude
        self.generation = generation
        self.authority_root = authority_root
        self.allow_tmp = allow_tmp
        self.timeout = timeout
        self.router = router
        self.traits = DriverTraits(
            shared_host=True, startup_timeout=startup_timeout, startup_grace=0.0, unload_timeout=5.0
        )

    def host_key(self, request: ActionRequest) -> Hashable:
        argv = load_provider(self.provider, exclude=self.exclude).argv(
            subsession_dir=CATALOG_PATH.parent, model=request.model, effort=request.effort
        )
        return (
            self.provider, tuple(argv), self.generation,
            str(self.authority_root.resolve()), self.allow_tmp,
        )

    def _authority(self) -> ParentAuthority:
        roots = (self.authority_root, Path("/tmp")) if self.allow_tmp else (self.authority_root,)
        return ParentAuthority(roots)

    def _payload(self, turn: TurnSpec) -> dict[str, object]:
        request = turn.request
        payload: dict[str, object] = {
            "wire_version": 3,
            "provider": self.provider,
            "cwd": turn.cwd,
            "prompt": request.prompt or "",
            "generation": self.generation,
            "model": request.model,
            "effort": request.effort,
            "boundary_kind": "host_approved",
        }
        if turn.durable_id is not None:
            payload["session_id"] = turn.durable_id
        return payload

    def run_turn(
        self, turn: TurnSpec, slot: HostSlot, started: Callable[[SessionHandle], None]
    ) -> TurnEnd:
        host = slot.host
        assert host is None or isinstance(host, AcpHost)
        capture = host.capture if host is not None else AcpMessageCapture()

        def on_spawned(connection: object) -> None:
            # The router reports a reused connection too; only a launched one is a new host.
            if host is None:
                slot.adopt(AcpHost(connection, capture))

        bindings: list[AcpBinding] = []

        def on_started(bound: AcpStartedTurn) -> None:
            session = _AcpSession(bound)
            session_id = session.binding.session_id
            bindings.append(session.binding)
            started(SessionHandle(session_id, session_id, _proof_wire(bound.binding_proof), session))

        router = self.router or acp_router.run_acp_request
        result = router(
            self._payload(turn),
            authority=self._authority(),
            request_timeout=self.timeout,
            spawn_admission=slot.launch_admission() if host is None else None,
            host_cell_id=slot.cell_id if host is None else None,
            started_observer=on_started,
            spawn_observer=on_spawned,
            message_capture=capture,
            existing_connection=host.connection if host is not None else None,
            retain_connection=True,
            exclude=self.exclude,
        )
        return turn_end(result, capture.take_actions(bindings[-1]) if bindings else None)

    def progress(self, session: SessionHandle) -> TurnProgress:
        state = session.state
        assert isinstance(state, _AcpSession)
        return state.capture.progress(state.binding) if state.capture is not None else TurnProgress("", None)

    def cancel(self, session: SessionHandle) -> None:
        state = session.state
        assert isinstance(state, _AcpSession)
        state.client.cancel(state.binding)

    def release(self, session: SessionHandle, timeout: float) -> None:
        state = session.state
        assert isinstance(state, _AcpSession)
        state.client.close(state.binding, timeout=timeout)
