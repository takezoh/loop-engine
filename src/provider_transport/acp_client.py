"""Provider-neutral ACP v1 session client and fail-closed client authority."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from threading import Lock
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .jsonl_rpc import JsonlRpcClient, NO_TIMEOUT
from .cwd_authority import SpawnAdmission
from .turn_actions import (
    KIND_COMMAND,
    KIND_EDIT,
    KIND_OTHER,
    KIND_READ,
    REFUSED_COMMAND,
    REFUSED_FILE_CHANGE,
    REFUSED_PERMISSIONS,
    ActionLog,
)
from .turn_result import (
    LOSS_IDENTITY_MISSING_OR_INVALID,
    LOSS_MALFORMED_AGENT_MESSAGE,
    LOSS_TRANSPORT_ERROR,
    LOSS_TURN_TIMEOUT,
    TurnCollector,
    TurnProgress,
    TurnResult,
)


class AcpRpc(Protocol):
    def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: object = None,
    ) -> Mapping[str, object]:
        ...

    def notify(self, method: str, params: Mapping[str, object]) -> None:
        ...

    def wait_notification(
        self, method: str, *, predicate, timeout: float | None = None
    ) -> Mapping[str, object]:
        ...


@dataclass(frozen=True)
class AcpBinding:
    session_id: str
    provider: str
    generation: str


@dataclass(frozen=True)
class AcpOutcome:
    binding: AcpBinding
    stop_reason: str
    turn_result: TurnResult | None = None


class AcpPromptError(RuntimeError):
    """A prompt failed after its turn collector opened."""

    def __init__(self, cause: BaseException, turn_result: TurnResult) -> None:
        self.cause = cause
        self.turn_result = turn_result
        super().__init__(f"acp-prompt-failed:{type(cause).__name__}:{cause}")


ACP_TOOL_KINDS = {
    "execute": KIND_COMMAND,
    "edit": KIND_EDIT, "delete": KIND_EDIT, "move": KIND_EDIT,
    "read": KIND_READ, "search": KIND_READ, "think": KIND_READ, "fetch": KIND_READ,
}


def record_tool_update(actions: ActionLog, update: Mapping[str, object]) -> None:
    """Add one ACP `tool_call` or `tool_call_update` to the turn's actions."""
    call_id = update.get("toolCallId")
    if not isinstance(call_id, str) or not call_id:
        return
    raw_kind = update.get("kind")
    kind = ACP_TOOL_KINDS.get(raw_kind, KIND_OTHER) if isinstance(raw_kind, str) else None
    raw_input = update.get("rawInput")
    command = raw_input.get("command") if isinstance(raw_input, Mapping) else None
    locations = update.get("locations")
    paths = tuple(
        location["path"] for location in locations
        if isinstance(location, Mapping) and isinstance(location.get("path"), str)
    ) if isinstance(locations, Sequence) and not isinstance(locations, (str, bytes)) else ()
    title = update.get("title")
    actions.begin(
        call_id, kind, name=title if isinstance(title, str) else None,
        command=command if isinstance(command, str) else None, paths=paths,
    )
    # An agent may report the tool's own output apart from the text it shows the model.
    meta = update.get("_meta")
    if isinstance(meta, Mapping):
        for extension in meta.values():
            response = extension.get("toolResponse") if isinstance(extension, Mapping) else None
            stdout = response.get("stdout") if isinstance(response, Mapping) else None
            if isinstance(stdout, str):
                actions.output(call_id, stdout)
    if update.get("status") in ("completed", "failed"):
        raw_output = update.get("rawOutput")
        actions.end(
            call_id, output=raw_output if isinstance(raw_output, str) else None,
            keep_recorded_output=True,
        )


class AcpMessageCapture:
    """Bounded ACP agent-message mapper demultiplexed by provider session id."""

    def __init__(
        self,
        *,
        max_chars: int = 8192,
        activity_observer: Callable[[str, str], None] | None = None,
    ) -> None:
        self.max_chars = max_chars
        self._lock = Lock()
        self._text = ""
        self._collectors: dict[str, TurnCollector] = {}
        self._actions: dict[str, ActionLog] = {}
        self._last_session_id: str | None = None
        self.activity_observer = activity_observer

    def begin(self, binding: AcpBinding, invocation_sequence: int) -> None:
        with self._lock:
            if binding.session_id in self._collectors:
                raise RuntimeError("acp-session-turn-already-active")
            # A continued session in a new process restarts the invocation
            # sequence, so a random component keeps every turn identity fresh.
            identity = (
                f"{binding.generation}:{binding.session_id}"
                f":prompt-{invocation_sequence}-{uuid4().hex[:12]}"
            )
            self._collectors[binding.session_id] = TurnCollector(identity)
            self._actions[binding.session_id] = ActionLog()
            self._last_session_id = binding.session_id
        self._activity("prompt-starting", "session-prompt-starting")

    def finish(
        self, *, completed: bool, reason: str = LOSS_TRANSPORT_ERROR,
        binding: AcpBinding | None = None,
    ) -> TurnResult:
        with self._lock:
            session_id = binding.session_id if binding is not None else self._last_session_id
            collector = self._collectors.pop(session_id, None) if session_id else None
            if self._last_session_id == session_id:
                self._last_session_id = next(iter(self._collectors), None)
        if collector is None:
            raise RuntimeError("acp-turn-collector-not-active")
        self._activity("terminal-pending" if completed else "degraded", reason)
        return (
            collector.seal_completed()
            if completed
            else collector.seal_incomplete(reason)
        )

    def observe(self, message: Mapping[str, object]) -> None:
        if message.get("method") != "session/update":
            return
        params = message.get("params")
        if not isinstance(params, Mapping):
            self._note_active_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        with self._lock:
            observed_session = params.get("sessionId")
            collector = self._collectors.get(observed_session) if isinstance(observed_session, str) else None
        if not isinstance(observed_session, str) or not observed_session:
            self._note_active_loss(LOSS_IDENTITY_MISSING_OR_INVALID)
            return
        if collector is None:
            return
        update = params.get("update")
        if not isinstance(update, Mapping):
            collector.note_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        if update.get("sessionUpdate") in ("tool_call", "tool_call_update"):
            collector.note_activity()
            with self._lock:
                actions = self._actions.get(observed_session)
            if actions is not None:
                record_tool_update(actions, update)
            return
        if update.get("sessionUpdate") != "agent_message_chunk":
            collector.note_activity()
            return
        content = update.get("content")
        if not isinstance(content, Mapping) or content.get("type") != "text":
            collector.note_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        text = content.get("text")
        if not isinstance(text, str):
            collector.note_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        with self._lock:
            collector = self._collectors.get(observed_session) if isinstance(observed_session, str) else None
            if collector is not None:
                collector.observe_text(text)
                self._activity("running", "agent-message-chunk")

    def take(self) -> str:
        with self._lock:
            result = self._text
            self._text = ""
            return result

    def refused(self, session_id: object, kind: str, description: str) -> None:
        """Record a refused request in the actions of the session's running prompt."""
        with self._lock:
            actions = self._actions.get(session_id) if isinstance(session_id, str) else None
        if actions is not None:
            actions.refuse(kind, description)

    def take_actions(self, binding: AcpBinding) -> dict[str, object] | None:
        """The actions of the session's latest prompt, handed over once."""
        with self._lock:
            actions = self._actions.pop(binding.session_id, None)
        return actions.to_wire() if actions is not None else None

    def progress(self, binding: AcpBinding | None = None) -> TurnProgress:
        """Return the current bounded turn text and latest event time without sealing or consuming it."""
        with self._lock:
            session_id = binding.session_id if binding is not None else self._last_session_id
            collector = self._collectors.get(session_id or "")
            text = self._text
        if collector is not None:
            return collector.progress()
        return TurnProgress(text[-self.max_chars :], None)

    def _note_active_loss(self, reason: str) -> None:
        with self._lock:
            collectors = tuple(self._collectors.values())
        for collector in collectors:
            collector.note_loss(reason)
            self._activity("degraded", reason)

    def note_transport_loss(self, reason: str) -> None:
        self._note_active_loss(reason)

    def _activity(self, phase: str, reason: str) -> None:
        if self.activity_observer is not None:
            self.activity_observer(phase, reason)


@dataclass(frozen=True)
class ParentAuthority:
    roots: tuple[Path, ...]

    def permits_path(self, raw_path: str) -> bool:
        candidate = Path(raw_path).expanduser().resolve()
        return any(candidate == root.resolve() or root.resolve() in candidate.parents for root in self.roots)


# What an ACP tool call asks permission for, as the kind of a refused request.
PERMISSION_KINDS = {
    "execute": REFUSED_COMMAND,
    "edit": REFUSED_FILE_CHANGE, "delete": REFUSED_FILE_CHANGE, "move": REFUSED_FILE_CHANGE,
}


def permission_refusal(params: Mapping[str, object]) -> tuple[Mapping[str, object], tuple[str, str]]:
    """The answer to `session/request_permission`, and what it refused.

    Every request is refused and the turn goes on: the option of kind
    `reject_once` when the agent offers one, otherwise `cancelled`.
    """
    tool = params.get("toolCall")
    tool = tool if isinstance(tool, Mapping) else {}
    kind = tool.get("kind")
    title = tool.get("title")
    refusal = (
        PERMISSION_KINDS.get(kind, REFUSED_PERMISSIONS) if isinstance(kind, str) else REFUSED_PERMISSIONS,
        title if isinstance(title, str) else "",
    )
    options = params.get("options")
    if isinstance(options, Sequence) and not isinstance(options, (str, bytes)):
        for option in options:
            if not isinstance(option, Mapping) or option.get("kind") != "reject_once":
                continue
            option_id = option.get("optionId")
            if isinstance(option_id, str) and option_id:
                return {"outcome": {"outcome": "selected", "optionId": option_id}}, refusal
    return {"outcome": {"outcome": "cancelled"}}, refusal


class AcpClientRequestPolicy:
    """Decide agent-to-client requests without inheriting provider defaults.

    A permission request is refused and recorded through `refused(session_id,
    kind, description)`. Filesystem and terminal requests are refused with an error.
    """

    def __init__(
        self, authority: ParentAuthority, *,
        refused: Callable[[object, str, str], None] | None = None,
    ) -> None:
        self.authority = authority
        self.refused = refused
        self.failures: list[dict[str, str]] = []

    def _fail(self, method: str, code: str) -> None:
        if len(self.failures) < 16:
            self.failures.append({
                "owner": "acp-client-request-policy",
                "method": method,
                "error_code": code,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "retry_decision": "caller_owned",
            })

    def handle(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        if method == "session/request_permission":
            reply, (kind, description) = permission_refusal(params)
            if self.refused is not None:
                self.refused(params.get("sessionId"), kind, description)
            return reply
        if method.startswith("fs/"):
            path = params.get("path")
            if not isinstance(path, str) or not self.authority.permits_path(path):
                self._fail(method, "filesystem_outside_parent_authority")
                raise PermissionError("acp-filesystem-request-outside-parent-authority")
            self._fail(method, "filesystem_request_not_enabled")
            raise NotImplementedError("acp-filesystem-request-not-enabled")
        if method.startswith("terminal/"):
            self._fail(method, "terminal_request_not_authorized")
            raise PermissionError("acp-terminal-request-not-authorized")
        self._fail(method, "unsupported_client_request")
        raise NotImplementedError(f"unsupported-acp-client-request:{method}")


class AcpSessionClient:
    """Exact-direction ACP session operations; raw updates stay behind this API."""

    def __init__(
        self,
        rpc: AcpRpc,
        *,
        provider: str,
        generation: str,
        capabilities: Mapping[str, object],
        message_capture: AcpMessageCapture | None = None,
    ) -> None:
        self.rpc = rpc
        self.provider = provider
        self.generation = generation
        self.capabilities = capabilities
        self.message_capture = message_capture
        self._prompt_sequence = 0

    def new_session(
        self, *, cwd: str, mcp_servers: Sequence[object] = (),
        meta: Mapping[str, object] | None = None,
    ) -> AcpBinding:
        params: dict[str, object] = {"cwd": cwd, "mcpServers": list(mcp_servers)}
        if meta is not None:
            params["_meta"] = dict(meta)
        result = self.rpc.request("session/new", params)
        return AcpBinding(
            self._string(result, "sessionId"), self.provider, self.generation
        )

    def load_session(
        self, session_id: str, *, cwd: str, mcp_servers: Sequence[object] = (),
        meta: Mapping[str, object] | None = None,
    ) -> AcpBinding:
        sessions = self.capabilities.get("sessionCapabilities")
        if self._load_supported():
            method = "session/load"
        elif isinstance(sessions, Mapping) and isinstance(sessions.get("resume"), Mapping):
            method = "session/resume"
        else:
            raise RuntimeError("acp-load-session-not-capability-confirmed")
        params: dict[str, object] = {"sessionId": session_id, "cwd": cwd, "mcpServers": list(mcp_servers)}
        if meta is not None:
            params["_meta"] = dict(meta)
        result = self.rpc.request(method, params)
        returned = result.get("sessionId", session_id)
        if returned != session_id:
            raise ValueError("acp-load-returned-foreign-session")
        return AcpBinding(session_id, self.provider, self.generation)

    def prompt(self, binding: AcpBinding, text: str) -> AcpOutcome:
        self._require_binding(binding)
        self._prompt_sequence += 1
        capture = self.message_capture
        if capture is not None:
            capture.begin(binding, self._prompt_sequence)
        try:
            result = self.rpc.request(
                "session/prompt",
                {
                    "sessionId": binding.session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
                timeout=NO_TIMEOUT,
            )
            stop_reason = self._string(result, "stopReason")
        except Exception as exc:
            if capture is None:
                raise
            raise AcpPromptError(
                exc,
                capture.finish(
                    completed=False,
                    reason=(
                        LOSS_TURN_TIMEOUT
                        if isinstance(exc, TimeoutError)
                        else LOSS_TRANSPORT_ERROR
                    ),
                    binding=binding,
                ),
            ) from exc
        turn_result = (
            capture.finish(completed=True, binding=binding)
            if capture is not None else None
        )
        return AcpOutcome(binding, stop_reason, turn_result)

    def set_config_option(
        self, binding: AcpBinding, *, config_id: str, value: str
    ) -> None:
        self._require_binding(binding)
        wire_id, wire_value = config_id, value
        result = self.rpc.request(
            "session/set_config_option",
            {
                "sessionId": binding.session_id,
                "configId": wire_id,
                "value": wire_value,
            },
        )
        options = result.get("configOptions")
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
            raise RuntimeError("acp-config-readback-missing")
        for option in options:
            if (
                isinstance(option, Mapping)
                and option.get("id") == wire_id
                and option.get("currentValue") == wire_value
            ):
                return
        raise RuntimeError(f"acp-config-readback-mismatch:{config_id}")

    def cancel(self, binding: AcpBinding) -> None:
        self._require_binding(binding)
        self.rpc.notify("session/cancel", {"sessionId": binding.session_id})

    def close(self, binding: AcpBinding, *, timeout: float | None = None) -> None:
        self._require_binding(binding)
        if not self._close_supported():
            return
        params = {"sessionId": binding.session_id}
        if timeout is None:
            self.rpc.request("session/close", params)
        else:
            self.rpc.request("session/close", params, timeout=timeout)

    def _require_binding(self, binding: AcpBinding) -> None:
        if binding.provider != self.provider or binding.generation != self.generation:
            raise ValueError("stale-or-foreign-acp-binding")

    def _load_supported(self) -> bool:
        return bool(self.capabilities.get("loadSession"))

    def _close_supported(self) -> bool:
        if self.capabilities.get("closeSession"):
            return True
        sessions = self.capabilities.get("sessionCapabilities")
        return (
            isinstance(sessions, Mapping)
            and isinstance(sessions.get("close"), Mapping)
        )

    @staticmethod
    def _string(value: Mapping[str, object], key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str) or not item:
            raise ValueError(f"ACP response field {key} must be a non-empty string")
        return item


@dataclass
class AcpStdioConnection:
    """Initialized ACP stdio connection with finite process ownership."""

    rpc: JsonlRpcClient
    capabilities: Mapping[str, object]
    request_policy: AcpClientRequestPolicy | None = None

    @classmethod
    def launch(
        cls,
        command: Sequence[str],
        *,
        authority: ParentAuthority,
        request_timeout: float = 30.0,
        message_capture: AcpMessageCapture | None = None,
        spawn_admission: SpawnAdmission,
        host_cell_id: str,
    ) -> "AcpStdioConnection":
        from .admitted_process import launch_admitted
        process, spawn_receipt = launch_admitted(
            tuple(command), spawn_admission=spawn_admission, host_cell_id=host_cell_id,
            options={
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
                "encoding": "utf-8",
                "bufsize": 1,
            },
        )
        assert process.stdin is not None and process.stdout is not None
        policy = AcpClientRequestPolicy(
            authority, refused=message_capture.refused if message_capture is not None else None,
        )
        rpc = JsonlRpcClient(
            stdin=process.stdin,
            stdout=process.stdout,
            stderr=process.stderr,
            process=process,
            request_timeout=request_timeout,
            server_request_handler=policy.handle,
            notification_allowlist=(
                frozenset({"session/update"})
                if message_capture is not None
                else frozenset()
            ),
            notification_handler=(
                message_capture.observe if message_capture is not None else None
            ),
            active_turn_loss_handler=(
                message_capture.note_transport_loss
                if message_capture is not None
                else None
            ),
            spawn_admission=spawn_admission,
            spawn_receipt=spawn_receipt,
        )
        try:
            initialized = rpc.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {
                            "readTextFile": False,
                            "writeTextFile": False,
                        },
                        "terminal": False,
                    },
                    "clientInfo": {
                        "name": "runtime",
                        "version": "0.1.0",
                    },
                },
            )
        except BaseException:
            rpc.close()
            raise
        capabilities = initialized.get("agentCapabilities")
        if not isinstance(capabilities, Mapping):
            rpc.close()
            raise ValueError("ACP initialize omitted agentCapabilities")
        return cls(rpc, capabilities, policy)
