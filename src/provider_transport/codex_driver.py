"""The Codex App Server protocol driver.

One shared `codex app-server` host serves every thread. A new session starts a
thread; a later session resumes the thread by its durable thread id. Each turn
starts on the bound thread and ends when the App Server reports its terminal.
"""
from __future__ import annotations

from typing import Callable, Hashable, Mapping

from . import codex_app_server
from .codex_app_server import AppServerRpc, CodexAppServerAdapter, CodexTurnError, TransportBinding
from .cwd_authority import SpawnAdmission
from .provider_request import ActionRequest
from .provider_catalog import CATALOG_PATH, load_provider
from .provider_port import DriverTraits, HostSlot, SessionHandle, TurnEnd, TurnSpec
from .turn_result import TurnProgress


GENERATION = "codex-app-server:v2"


def _actions_detail(actions: Mapping[str, object] | None) -> dict[str, object]:
    return {"actions": dict(actions)} if actions is not None else {}


class CodexHost:
    """A launched App Server: one JSON-RPC connection shared by its threads."""

    def __init__(self, rpc: AppServerRpc) -> None:
        self.rpc = rpc

    def alive(self) -> bool:
        process = getattr(self.rpc, "process", None)
        return process is None or process.poll() is None

    def close(self) -> None:
        getattr(self.rpc, "close")()


class _CodexSession:
    def __init__(self, client: CodexAppServerAdapter, binding: TransportBinding) -> None:
        self.client = client
        self.binding = binding
        self.turn_id: str | None = None
        self.cancelled = False


class CodexDriver:
    """Starts and resumes App Server threads and runs one turn on each."""

    transport = "codex-app-server"

    def __init__(
        self, *, provider: str = "codex", generation: str = GENERATION, timeout: float = 300.0,
        startup_grace: float = 1.0,
        launch: Callable[[SpawnAdmission, str], AppServerRpc] | None = None,
        exclude: object = None,
    ) -> None:
        self.provider = provider
        self.provider_spec = load_provider(provider, exclude=exclude)
        self.generation = generation
        self.timeout = timeout
        self._launch = launch
        self.traits = DriverTraits(
            shared_host=True, startup_timeout=timeout, startup_grace=startup_grace, unload_timeout=65.0
        )

    def host_key(self, request: ActionRequest) -> Hashable:
        return self.provider

    def launch_host(self, admission: SpawnAdmission, cell_id: str) -> AppServerRpc:
        """Launch the App Server with the catalog command, through the spawn admission."""
        if self._launch is not None:
            return self._launch(admission, cell_id)
        from .jsonl_rpc import JsonlRpcClient
        return JsonlRpcClient.launch(
            command=self.provider_spec.argv(subsession_dir=CATALOG_PATH.parent),
            request_timeout=self.timeout,
            spawn_admission=admission,
            host_cell_id=cell_id,
        )

    def run_turn(
        self, turn: TurnSpec, slot: HostSlot, started: Callable[[SessionHandle], None]
    ) -> TurnEnd:
        if slot.host is None:
            slot.adopt(CodexHost(self.launch_host(slot.launch_admission(), slot.cell_id)))
        assert isinstance(slot.host, CodexHost)
        client = codex_app_server.CodexAppServerAdapter(
            slot.host.rpc, generation=self.generation,
        )
        request = turn.request
        binding = (
            client.start(cwd=turn.cwd, model=request.model, effort=request.effort)
            if turn.durable_id is None
            else client.resume(turn.durable_id, cwd=turn.cwd, model=request.model)
        )
        session = _CodexSession(client, binding)
        started(SessionHandle(
            binding.session_id, binding.thread_id,
            {
                "provider": self.provider, "generation": binding.generation,
                "model": request.model, "effort": request.effort,
                "session_id": binding.session_id,
            },
            session,
        ))
        try:
            session.turn_id = client.continue_turn(
                binding, text=request.prompt or "", model=request.model, effort=request.effort,
            )
            if session.cancelled:
                client.interrupt(binding, turn_id=session.turn_id)
            outcome = client.wait(binding, turn_id=session.turn_id, timeout=None)
        except CodexTurnError as exc:
            return TurnEnd(False, "incomplete", exc.turn_result.to_wire(),
                           detail={'reason': str(exc)[:512], **_actions_detail(exc.actions)})
        return TurnEnd(
            outcome.status == "completed", outcome.status,
            outcome.turn_result.to_wire() if outcome.turn_result is not None else None,
            detail=_actions_detail(outcome.actions),
        )

    def progress(self, session: SessionHandle) -> TurnProgress:
        state = session.state
        assert isinstance(state, _CodexSession)
        return state.client.progress(state.binding)

    def cancel(self, session: SessionHandle) -> None:
        state = session.state
        assert isinstance(state, _CodexSession)
        state.cancelled = True
        if state.turn_id is not None:
            state.client.interrupt(state.binding, turn_id=state.turn_id)

    def release(self, session: SessionHandle, timeout: float) -> None:
        state = session.state
        assert isinstance(state, _CodexSession)
        state.client.release(state.binding, timeout=timeout)
