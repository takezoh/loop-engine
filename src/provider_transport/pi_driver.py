"""The Pi RPC protocol driver.

Pi serves one session per process, so each turn launches `pi --mode rpc` in
the admitted folder with its durable session id and session folder, its
model and thinking level, and the catalog skill exclusion. The session binds once
`get_state` confirms that id, model and thinking level; the turn ends at
`agent_settled`. Releasing the session ends its process; a later turn
reloads the session file by the same id.
"""
from __future__ import annotations

from pathlib import Path
from threading import Event, Lock
from typing import Any, Callable, Hashable, Mapping
from uuid import uuid4

from .cwd_authority import SpawnAdmission
from .provider_request import ActionRequest
from .pi_rpc import TRANSPORT_CLOSED, PiRpcClient
from .provider_catalog import CATALOG_PATH, load_provider
from .provider_port import DriverTraits, HostSlot, SessionHandle, TurnEnd, TurnSpec
from .turn_actions import KIND_COMMAND, KIND_EDIT, KIND_OTHER, KIND_READ, REFUSED_INPUT, ActionLog
from .turn_result import LOSS_TRANSPORT_EOF, LOSS_TURN_NOT_COMPLETED, TurnCollector, TurnProgress


GENERATION = "pi-rpc:v1"
FAILED_STOPS = frozenset({"error", "aborted"})
# Pi's builtin tools by what they do; any other tool is recorded by name only.
TOOL_KINDS = {
    "bash": KIND_COMMAND, "edit": KIND_EDIT, "write": KIND_EDIT,
    "read": KIND_READ, "grep": KIND_READ, "find": KIND_READ, "ls": KIND_READ,
}


# Extension UI methods that wait for an answer; every other UI request is a notice.
DIALOG_METHODS = frozenset({"select", "confirm", "input", "editor"})


def dialog_refusal(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """The id of an extension dialog to answer as cancelled and what it asked; None for anything else."""
    if event.get("type") != "extension_ui_request" or event.get("method") not in DIALOG_METHODS:
        return None
    request_id = event.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    title = event.get("title")
    return request_id, title if isinstance(title, str) else ""


def record_tool_event(actions: ActionLog, event: Mapping[str, Any]) -> None:
    """Add one `tool_execution_start` or `tool_execution_end` event to the turn's actions."""
    call_id = event.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    if event.get("type") == "tool_execution_start":
        name = event.get("toolName")
        name = name if isinstance(name, str) else ""
        kind = TOOL_KINDS.get(name, KIND_OTHER)
        args = event.get("args")
        args = args if isinstance(args, Mapping) else {}
        command = args.get("command") if kind == KIND_COMMAND else None
        path = args.get("path")
        actions.begin(
            call_id, kind, name=name,
            command=command if isinstance(command, str) else None,
            paths=(path,) if isinstance(path, str) else (),
        )
        return
    result = event.get("result")
    result = result if isinstance(result, Mapping) else {}
    structured = result.get("structuredContent")
    structured = structured if isinstance(structured, Mapping) else {}
    output = structured.get("output")
    if not isinstance(output, str):
        content = result.get("content")
        output = "".join(
            str(block.get("text", "")) for block in content
            if isinstance(block, Mapping) and block.get("type") == "text"
        ) if isinstance(content, list) else None
    exit_code = structured.get("exit_code")
    actions.end(
        call_id, output=output,
        exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
    )


def binding_mismatch(
    state: Mapping[str, Any], *, session_id: str, model: str | None, effort: str | None
) -> str | None:
    """Why the session Pi reports is not the one requested, or None when it is."""
    if state.get("sessionId") != session_id:
        return "pi-session-identity-mismatch"
    if model:
        current = state.get("model")
        names = set()
        if isinstance(current, Mapping):
            names = {str(current.get("id")), f"{current.get('provider')}/{current.get('id')}"}
        if model not in names:
            return "pi-model-binding-mismatch"
    if effort and state.get("thinkingLevel") != effort:
        return "pi-thinking-binding-mismatch"
    return None


def assistant_text(message: Mapping[str, Any] | None) -> str:
    """The text blocks of an assistant message, in order."""
    if message is None:
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text", "")) for block in content
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def turn_end(
    *, identity: str, final: Mapping[str, Any] | None, aborted: bool, transport_lost: bool,
    actions: ActionLog | None = None,
) -> TurnEnd:
    """How a Pi run ended, from its final assistant message and how it settled."""
    collector = TurnCollector(identity)
    collector.observe_text(assistant_text(final))
    detail = {"actions": actions.to_wire()} if actions is not None else {}
    if transport_lost:
        return TurnEnd(False, "transport_lost", collector.seal_incomplete(LOSS_TRANSPORT_EOF).to_wire(),
                       detail=detail)
    stop = final.get("stopReason") if final is not None else None
    status = "aborted" if aborted else str(stop or "no_message")
    if aborted or final is None or stop in FAILED_STOPS:
        return TurnEnd(False, status, collector.seal_incomplete(LOSS_TURN_NOT_COMPLETED).to_wire(),
                       detail=detail)
    return TurnEnd(True, status, collector.seal_completed().to_wire(), detail=detail)


class PiHost:
    """One Pi RPC process serving one session."""

    def __init__(self, client: PiRpcClient) -> None:
        self.client = client

    def alive(self) -> bool:
        return self.client.alive()

    def close(self) -> None:
        self.client.close()


class _PiRun:
    """What the events of one run told: live text, the final message, how it settled.

    An extension dialog is answered as cancelled at once and recorded as refused.
    """

    def __init__(self, identity: str, client: PiRpcClient) -> None:
        self.client = client
        self.live = TurnCollector(identity)
        self.actions = ActionLog()
        self.final: Mapping[str, Any] | None = None
        self.aborted = False
        self.transport_lost = False
        self.settled = Event()
        self._lock = Lock()

    def on_event(self, event: Mapping[str, Any]) -> None:
        kind = event.get("type")
        if kind != TRANSPORT_CLOSED:
            self.live.note_activity()
        if kind == "message_update":
            update = event.get("assistantMessageEvent")
            if isinstance(update, Mapping) and update.get("type") == "text_delta":
                self.live.observe_text(str(update.get("delta", "")))
        elif kind == "message_end":
            message = event.get("message")
            if isinstance(message, Mapping) and message.get("role") == "assistant":
                with self._lock:
                    self.final = message
        elif kind in ("tool_execution_start", "tool_execution_end"):
            record_tool_event(self.actions, event)
        elif kind == "extension_ui_request":
            dialog = dialog_refusal(event)
            if dialog is not None:
                request_id, asked = dialog
                self.actions.refuse(REFUSED_INPUT, asked)
                try:
                    self.client.cancel_ui_request(request_id)
                except OSError:
                    pass  # the transport is closing; the run ends with it
        elif kind == "agent_settled":
            self.aborted = event.get("aborted") is True
            self.settled.set()
        elif kind == TRANSPORT_CLOSED:
            self.transport_lost = True
            self.settled.set()


class _PiSession:
    def __init__(self, client: PiRpcClient, run: _PiRun) -> None:
        self.client = client
        self.run = run


class PiDriver:
    """Launches one Pi RPC process per turn and runs the prompt to `agent_settled`."""

    transport = "pi-rpc"

    def __init__(
        self, *, provider: str = "pi", generation: str = GENERATION, session_dir: Path,
        timeout: float = 300.0, startup_timeout: float = 60.0,
        launch: Callable[[tuple[str, ...], SpawnAdmission, str], PiRpcClient] | None = None,
        new_session_id: Callable[[], str] = lambda: uuid4().hex,
        exclude: object = None,
    ) -> None:
        self.provider = provider
        self.provider_spec = load_provider(provider, exclude=exclude)
        self.generation = generation
        self.session_dir = session_dir
        self.timeout = timeout
        self._launch = launch
        self._new_session_id = new_session_id
        self.traits = DriverTraits(
            shared_host=False, startup_timeout=startup_timeout, startup_grace=0.0, unload_timeout=10.0
        )

    def host_key(self, request: ActionRequest) -> Hashable:
        return (self.provider, request.operation_id)

    def command(self, session_id: str, request: ActionRequest) -> tuple[str, ...]:
        """The catalog command with this session's folder, id, model and thinking level."""
        argv = self.provider_spec.argv(
            subsession_dir=CATALOG_PATH.parent, model=request.model, effort=request.effort
        )
        return (
            *argv, "--session-dir", str(self.session_dir), "--session-id", session_id,
            *(("--model", request.model) if request.model else ()),
            *(("--thinking", request.effort) if request.effort else ()),
        )

    def _launch_host(self, command: tuple[str, ...], admission: SpawnAdmission, cell_id: str) -> PiRpcClient:
        if self._launch is not None:
            return self._launch(command, admission, cell_id)
        self.session_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        return PiRpcClient.launch(
            command, request_timeout=self.timeout, spawn_admission=admission, host_cell_id=cell_id
        )

    def run_turn(
        self, turn: TurnSpec, slot: HostSlot, started: Callable[[SessionHandle], None]
    ) -> TurnEnd:
        request = turn.request
        session_id = turn.durable_id or self._new_session_id()
        if slot.host is None:
            slot.adopt(PiHost(self._launch_host(self.command(session_id, request), slot.launch_admission(), slot.cell_id)))
        assert isinstance(slot.host, PiHost)
        client = slot.host.client
        run = _PiRun(f"{session_id}:{request.operation_id}", client)
        client.subscribe(run.on_event)
        mismatch = binding_mismatch(
            client.command("get_state"), session_id=session_id, model=request.model, effort=request.effort
        )
        if mismatch is not None:
            raise RuntimeError(mismatch)
        started(SessionHandle(
            session_id, session_id,
            {
                "provider": self.provider, "generation": self.generation,
                "model": request.model, "effort": request.effort, "session_id": session_id,
            },
            _PiSession(client, run),
        ))
        accepted = client.command("prompt", message=request.prompt or "")
        if accepted.get("disposition") == "handled":
            return TurnEnd(True, "handled", TurnCollector(f"{session_id}:{request.operation_id}").seal_completed().to_wire())
        run.settled.wait()
        return turn_end(
            identity=f"{session_id}:{request.operation_id}", final=run.final,
            aborted=run.aborted, transport_lost=run.transport_lost, actions=run.actions,
        )

    def progress(self, session: SessionHandle) -> TurnProgress:
        state = session.state
        assert isinstance(state, _PiSession)
        return state.run.live.progress()

    def cancel(self, session: SessionHandle) -> None:
        state = session.state
        assert isinstance(state, _PiSession)
        state.client.command("abort")

    def release(self, session: SessionHandle, timeout: float) -> None:
        state = session.state
        assert isinstance(state, _PiSession)
        state.client.close(term_grace=timeout)
