"""Codex App Server adapter with normalized, generation-bound evidence."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import StrEnum
import shlex
from threading import Lock, RLock
from typing import Callable, Mapping, Protocol, Sequence
from uuid import uuid4

from .turn_actions import (
    KIND_COMMAND,
    KIND_EDIT,
    KIND_OTHER,
    REFUSED_COMMAND,
    REFUSED_FILE_CHANGE,
    REFUSED_INPUT,
    REFUSED_PERMISSIONS,
    ActionLog,
)
from .turn_result import (
    COMMUNICATION_BUDGET_BYTES,
    LOSS_AMBIGUOUS_CORRELATION,
    LOSS_IDENTITY_MISSING_OR_INVALID,
    LOSS_MALFORMED_AGENT_MESSAGE,
    LOSS_OBSERVATION_UNAVAILABLE,
    LOSS_PENDING_OVERFLOW,
    LOSS_SOURCE_CONFLICT,
    LOSS_TRANSPORT_ERROR,
    LOSS_TURN_NOT_COMPLETED,
    LOSS_TURN_TIMEOUT,
    TurnCollector,
    TurnProgress,
    TurnResult,
)


class AppServerRpc(Protocol):
    def request(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """Send one initialized app-server request or raise on protocol failure."""

    def wait_notification(
        self,
        method: str,
        *,
        predicate,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        """Wait for one normalized notification."""


def shell_script(command: object) -> str | None:
    """The script the agent asked its shell to run, without the `<shell> -lc` wrapper."""
    if isinstance(command, str):
        try:
            argv = shlex.split(command)
        except ValueError:
            return command
    elif isinstance(command, Sequence) and all(isinstance(part, str) for part in command):
        argv = list(command)
    else:
        return None
    if len(argv) == 3 and argv[1] in ("-lc", "-c"):
        return argv[2]
    return command if isinstance(command, str) else shlex.join(argv)


# Thread items that are the conversation itself, not something the agent did.
CONVERSATION_ITEMS = frozenset({"userMessage", "agentMessage", "reasoning", "plan"})


def record_item(actions: ActionLog, method: str, params: Mapping[str, object]) -> None:
    """Add one `item/started` or `item/completed` notification to the turn's actions."""
    item = params.get("item")
    if not isinstance(item, Mapping):
        return
    item_type, call_id = item.get("type"), item.get("id")
    if not isinstance(item_type, str) or item_type in CONVERSATION_ITEMS:
        return
    if not isinstance(call_id, str) or not call_id:
        return
    if item_type == "commandExecution":
        actions.begin(call_id, KIND_COMMAND, name=item_type, command=shell_script(item.get("command")))
    elif item_type == "fileChange":
        changes = item.get("changes")
        paths = tuple(
            change["path"] for change in changes
            if isinstance(change, Mapping) and isinstance(change.get("path"), str)
        ) if isinstance(changes, Sequence) and not isinstance(changes, (str, bytes)) else ()
        actions.begin(call_id, KIND_EDIT, name=item_type, paths=paths)
    else:
        actions.begin(call_id, KIND_OTHER, name=item_type)
    if method == "item/completed":
        output, exit_code = item.get("aggregatedOutput"), item.get("exitCode")
        actions.end(
            call_id, output=output if isinstance(output, str) else None,
            exit_code=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
        )


Refusal = tuple[str, str]


def _text(params: Mapping[str, object], *keys: str) -> str:
    for key in keys:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def approval_reply(
    method: str, params: Mapping[str, object]
) -> tuple[Mapping[str, object], Refusal] | None:
    """The answer to an App Server request for approval, and what it refused; None for any other request.

    Every approval is refused and the turn goes on: a command or a file change
    is declined, a permission request is granted nothing for the turn, and an
    MCP server's request for input is declined.
    """
    if method == "item/commandExecution/requestApproval":
        return {"decision": "decline"}, (REFUSED_COMMAND, _text(params, "command", "reason"))
    if method == "item/fileChange/requestApproval":
        return {"decision": "decline"}, (REFUSED_FILE_CHANGE, _text(params, "reason"))
    if method == "item/permissions/requestApproval":
        # The scope defaults to the turn; no permission is granted.
        return {"permissions": {}}, (REFUSED_PERMISSIONS, _text(params, "reason"))
    if method == "mcpServer/elicitation/request":
        server = _text(params, "serverName")
        asked = _text(params, "message", "title")
        return (
            {"action": "decline", "content": None, "_meta": None},
            (REFUSED_INPUT, f"{server}: {asked}" if server else asked),
        )
    return None


def _request_thread(params: Mapping[str, object]) -> object:
    # `item/fileChange/requestApproval` names its fields in snake case.
    return params.get("threadId", params.get("thread_id"))


class _CodexNotificationDispatcher:
    """Route every notification and request of a thread to the adapter owning that thread.

    Agent-message deltas are consumed there; every other notification stays
    queued for the waiters of the shared RPC connection. Requests to the client
    are answered by `approval_reply`; a refusal is recorded by the thread's adapter.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._sinks: dict[str, Callable[[Mapping[str, object]], bool]] = {}
        self._refusal_sinks: dict[str, Callable[[str, str], None]] = {}

    def register(
        self, thread_id: str, sink: Callable[[Mapping[str, object]], bool],
        refused: Callable[[str, str], None] | None = None,
    ) -> None:
        with self._lock:
            self._sinks[thread_id] = sink
            if refused is not None:
                self._refusal_sinks[thread_id] = refused

    def unregister(self, thread_id: str) -> None:
        with self._lock:
            self._sinks.pop(thread_id, None)
            self._refusal_sinks.pop(thread_id, None)

    def answer(self, method: str, params: Mapping[str, object]) -> Mapping[str, object]:
        """The reply to one request of the App Server; an unsupported request raises."""
        decided = approval_reply(method, params)
        if decided is None:
            raise NotImplementedError(f"unsupported-app-server-request:{method}")
        reply, (kind, description) = decided
        thread_id = _request_thread(params)
        with self._lock:
            refused = self._refusal_sinks.get(thread_id) if isinstance(thread_id, str) else None
        if refused is not None:
            refused(kind, description)
        return reply

    def __call__(self, message: Mapping[str, object]) -> bool:
        delta = message.get("method") == "item/agentMessage/delta"
        params = message.get("params")
        thread_id = params.get("threadId") if isinstance(params, Mapping) else None
        if not isinstance(thread_id, str):
            return delta
        with self._lock:
            sink = self._sinks.get(thread_id)
        if sink is not None:
            sink(message)
        return delta


class LookupDisposition(StrEnum):
    CURRENT = "confirmed-current"
    UNAVAILABLE = "confirmed-unavailable"
    UNKNOWN = "unknown"
    INCONCLUSIVE = "inconclusive"
    CONFLICTING = "conflicting"


@dataclass(frozen=True)
class TransportBinding:
    thread_id: str
    session_id: str
    generation: str


@dataclass(frozen=True)
class AdapterObservation:
    binding: TransportBinding
    disposition: LookupDisposition
    status: str
    active_flags: tuple[str, ...] = ()

    @property
    def outcome(self) -> str:
        return {
            LookupDisposition.CURRENT: "current",
            LookupDisposition.UNAVAILABLE: "absent",
            LookupDisposition.UNKNOWN: "unknown",
            LookupDisposition.INCONCLUSIVE: "inconclusive",
            LookupDisposition.CONFLICTING: "conflicting",
        }[self.disposition]


@dataclass(frozen=True)
class TurnOutcome:
    binding: TransportBinding
    turn_id: str
    status: str
    turn_result: TurnResult | None = None
    # What the agent did during the turn (`turn_actions` wire form), when observed.
    actions: Mapping[str, object] | None = None


class CodexTurnError(RuntimeError):
    """A Codex turn failed after the bounded collector was opened."""

    def __init__(
        self, cause: BaseException, turn_result: TurnResult,
        actions: Mapping[str, object] | None = None,
    ) -> None:
        self.cause = cause
        self.turn_result = turn_result
        self.actions = actions
        super().__init__(f"codex-turn-failed:{type(cause).__name__}:{cause}")


class CodexAppServerAdapter:
    """Formal adapter; raw app-server messages do not cross this boundary."""

    def __init__(
        self,
        rpc: AppServerRpc,
        *,
        generation: str,
        activity_observer: Callable[[str, str], None] | None = None,
    ) -> None:
        self.rpc = rpc
        self.generation = generation
        self.activity_observer = activity_observer
        self._turn_lock = Lock()
        self._pending_start: tuple[TransportBinding, int, TurnCollector] | None = None
        self._active_turn: tuple[TransportBinding, str, TurnCollector] | None = None
        self._turn_sequence = 0
        self._turn_actions: ActionLog | None = None
        self._sealed_turn_ids: deque[str] = deque(maxlen=64)
        self._pending_deltas: list[tuple[str, object, str]] = []
        self._message_item: str | None = None
        self._pending_delta_bytes = 0
        self._pending_overflowed = False
        loss_setter = getattr(rpc, "active_turn_loss_handler", None)
        if loss_setter is None and hasattr(rpc, "__dict__"):
            try:
                setattr(rpc, "active_turn_loss_handler", self._note_active_loss)
            except (AttributeError, TypeError):
                pass
        # One dispatcher belongs to the shared RPC connection. Per-thread
        # collectors receive only their own deltas, keeping streaming bounded.
        dispatcher = getattr(rpc, "codex_notification_dispatcher", None)
        if dispatcher is None and hasattr(rpc, "__dict__"):
            dispatcher = _CodexNotificationDispatcher()
            try:
                setattr(rpc, "codex_notification_dispatcher", dispatcher)
                setattr(rpc, "notification_sink", dispatcher)
                setattr(rpc, "server_request_handler", dispatcher.answer)
            except (AttributeError, TypeError):
                dispatcher = None
        self._dispatcher = dispatcher
        self._sink_wired = dispatcher is not None

    def start(
        self,
        *,
        cwd: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> TransportBinding:
        """Start a thread under the user's own approval and sandbox settings."""
        params: dict[str, object] = {
            "cwd": cwd,
            "serviceName": "runtime",
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        result = self.rpc.request("thread/start", params)
        thread = self._mapping(result, "thread")
        thread_id = self._string(thread, "id")
        session_id = thread.get("sessionId")
        if session_id is None:
            session_id = thread_id
        if not isinstance(session_id, str):
            raise ValueError("thread.sessionId must be a string")
        binding = TransportBinding(thread_id, session_id, self.generation)
        self._register_thread(binding)
        return binding

    def resume(
        self,
        thread_id: str,
        *,
        cwd: str,
        model: str | None = None,
    ) -> TransportBinding:
        """Resume a thread under the user's own approval and sandbox settings."""
        params: dict[str, object] = {
            "threadId": thread_id,
            "cwd": cwd,
        }
        if model:
            params["model"] = model
        result = self.rpc.request("thread/resume", params)
        thread = self._mapping(result, "thread")
        returned_id = self._string(thread, "id")
        if returned_id != thread_id:
            raise ValueError("thread-resume-returned-foreign-thread")
        session_id = thread.get("sessionId", returned_id)
        if not isinstance(session_id, str):
            raise ValueError("thread.sessionId must be a string")
        binding = TransportBinding(returned_id, session_id, self.generation)
        self._register_thread(binding)
        return binding

    def continue_turn(
        self,
        binding: TransportBinding,
        *,
        text: str,
        model: str | None = None,
        effort: str | None = None,
    ) -> str:
        self._require_generation(binding)
        self._activity("prompt-starting", "turn-starting")
        with self._turn_lock:
            if self._pending_start is not None or self._active_turn is not None:
                raise RuntimeError("codex-turn-collector-already-active")
            self._turn_sequence += 1
            sequence = self._turn_sequence
            # A resumed thread in a new process restarts the sequence, so a
            # random component keeps every turn identity fresh.
            collector = TurnCollector(
                f"{binding.generation}:{binding.thread_id}"
                f":turn-{sequence}-{uuid4().hex[:12]}"
            )
            self._pending_start = (binding, sequence, collector)
            self._turn_actions = ActionLog()
            self._message_item = None
            self._pending_deltas = []
            self._pending_delta_bytes = 0
            self._pending_overflowed = False
        params: dict[str, object] = {
            "threadId": binding.thread_id,
            "input": [{"type": "text", "text": text}],
        }
        if model:
            params["model"] = model
        if effort:
            params["effort"] = effort
        try:
            result = self.rpc.request("turn/start", params)
            turn = self._mapping(result, "turn")
            turn_id = self._string(turn, "id")
        except Exception as exc:
            self._finalize_pending_failure(binding, collector)
            with self._turn_lock:
                self._pending_start = None
            raise CodexTurnError(
                exc,
                collector.seal_incomplete(
                    LOSS_TURN_TIMEOUT
                    if isinstance(exc, TimeoutError)
                    else LOSS_TRANSPORT_ERROR
                ),
                self.take_actions(),
            ) from exc
        self._promote_pending(binding, turn_id, collector)
        with self._turn_lock:
            self._pending_start = None
            self._active_turn = (
                binding,
                turn_id,
                collector,
            )
        return turn_id

    def steer(
        self,
        binding: TransportBinding,
        *,
        text: str,
        expected_turn_id: str,
    ) -> str:
        self._require_generation(binding)
        result = self.rpc.request(
            "turn/steer",
            {
                "threadId": binding.thread_id,
                "expectedTurnId": expected_turn_id,
                "input": [{"type": "text", "text": text}],
            },
        )
        return self._string(result, "turnId")

    def wait(
        self,
        binding: TransportBinding,
        *,
        turn_id: str,
        timeout: float | None = None,
    ) -> TurnOutcome:
        self._require_generation(binding)
        collector = self._require_active_collector(binding, turn_id)
        try:
            params = self.rpc.wait_notification(
                "turn/completed",
                predicate=lambda value: self._notification_matches(
                    value, binding.thread_id, turn_id
                ),
                timeout=timeout,
            )
        except Exception as exc:
            self._collect_agent_deltas(binding, turn_id, collector)
            failure_reason = (
                LOSS_TURN_TIMEOUT if isinstance(exc, TimeoutError) else LOSS_TRANSPORT_ERROR
            )
            self._activity("degraded", failure_reason)
            result = self._seal_active(
                binding,
                turn_id,
                completed=False,
                reason=failure_reason,
            )
            raise CodexTurnError(exc, result, self.take_actions()) from exc
        self._collect_agent_deltas(binding, turn_id, collector)
        turn = self._mapping(params, "turn")
        status = self._string(turn, "status")
        terminal_text = self._terminal_agent_text(turn)
        delta_text = collector.text_preview()
        if terminal_text:
            if delta_text:
                # The collector holds a bounded head/tail projection; compare
                # the terminal full text through the same projection so local
                # truncation never reads as a source conflict.
                reference = (
                    collector.project_reference_text(terminal_text)
                    if collector.truncated
                    else terminal_text
                )
                if reference != delta_text:
                    collector.note_loss(
                        LOSS_SOURCE_CONFLICT,
                        epistemic_state="conflicting_evidence",
                    )
            else:
                collector.observe_text(terminal_text)
        if status == "completed":
            result = self._seal_active(binding, turn_id, completed=True)
        else:
            self._activity("degraded", LOSS_TURN_NOT_COMPLETED)
            result = self._seal_active(
                binding,
                turn_id,
                completed=False,
                reason=LOSS_TURN_NOT_COMPLETED,
            )
        return TurnOutcome(
            binding=binding,
            turn_id=self._string(turn, "id"),
            status=status,
            turn_result=result,
            actions=self.take_actions(),
        )

    def take_actions(self) -> dict[str, object] | None:
        """The actions of the latest turn this adapter started, handed over once."""
        with self._turn_lock:
            actions, self._turn_actions = self._turn_actions, None
        return actions.to_wire() if actions is not None else None

    def progress(self, binding: TransportBinding) -> TurnProgress:
        """The active turn's bounded streamed text and latest event time, or nothing."""
        self._require_generation(binding)
        with self._turn_lock:
            active = self._active_turn
        if active is None:
            return TurnProgress("", None)
        return active[2].progress()

    def lookup(self, binding: TransportBinding) -> AdapterObservation:
        self._require_generation(binding)
        try:
            result = self.rpc.request(
                "thread/read",
                {"threadId": binding.thread_id, "includeTurns": False},
            )
        except TimeoutError:
            return AdapterObservation(
                binding, LookupDisposition.INCONCLUSIVE, "lookup-timeout"
            )
        except (OSError, RuntimeError):
            return AdapterObservation(
                binding, LookupDisposition.INCONCLUSIVE, "lookup-error"
            )

        thread = self._mapping(result, "thread")
        returned_id = thread.get("id")
        if not isinstance(returned_id, str):
            return AdapterObservation(
                binding, LookupDisposition.UNKNOWN, "identity-missing"
            )
        if returned_id != binding.thread_id:
            return AdapterObservation(
                binding, LookupDisposition.CONFLICTING, "identity-mismatch"
            )
        status = thread.get("status")
        if not isinstance(status, Mapping):
            return AdapterObservation(
                binding, LookupDisposition.UNKNOWN, "status-missing"
            )
        kind = status.get("type")
        if kind in {"idle", "active"}:
            flags = status.get("activeFlags", ())
            if not isinstance(flags, Sequence) or isinstance(flags, (str, bytes)):
                flags = ()
            return AdapterObservation(
                binding,
                LookupDisposition.CURRENT,
                str(kind),
                tuple(str(flag) for flag in flags),
            )
        if kind in {"notLoaded", "systemError"}:
            return AdapterObservation(
                binding, LookupDisposition.UNAVAILABLE, str(kind)
            )
        return AdapterObservation(
            binding, LookupDisposition.UNKNOWN, "status-unknown"
        )

    def interrupt(self, binding: TransportBinding, *, turn_id: str) -> None:
        self._require_generation(binding)
        self.rpc.request(
            "turn/interrupt",
            {"threadId": binding.thread_id, "turnId": turn_id},
        )

    def release(self, binding: TransportBinding, *, timeout: float = 65.0) -> None:
        self._require_generation(binding)
        self.rpc.request("thread/unsubscribe", {"threadId": binding.thread_id})
        self.rpc.wait_notification(
            "thread/closed",
            predicate=lambda params: params.get("threadId") == binding.thread_id,
            timeout=timeout,
        )
        if self._dispatcher is not None:
            self._dispatcher.unregister(binding.thread_id)

    def _register_thread(self, binding: TransportBinding) -> None:
        if self._dispatcher is not None:
            self._dispatcher.register(binding.thread_id, self._sink_notification, self._note_refusal)

    def _note_refusal(self, kind: str, description: str) -> None:
        """Record a refused request in the actions of the turn this adapter runs."""
        with self._turn_lock:
            actions = self._turn_actions
        if actions is not None:
            actions.refuse(kind, description)

    def _require_active_collector(
        self, binding: TransportBinding, turn_id: str
    ) -> TurnCollector:
        with self._turn_lock:
            active = self._active_turn
        if active is None or active[0] != binding or active[1] != turn_id:
            raise ValueError("codex-turn-collector-not-active")
        return active[2]

    def _seal_active(
        self,
        binding: TransportBinding,
        turn_id: str,
        *,
        completed: bool,
        reason: str = LOSS_TRANSPORT_ERROR,
    ) -> TurnResult:
        collector = self._require_active_collector(binding, turn_id)
        # Retire the turn before sealing so the reader-thread sink treats any
        # further delta for this turn as a post-seal late event, and never
        # interleaves collector and turn-lock acquisition with sealing.
        with self._turn_lock:
            if self._active_turn is not None and self._active_turn[2] is collector:
                self._active_turn = None
            self._sealed_turn_ids.append(turn_id)
        return (
            collector.seal_completed()
            if completed
            else collector.seal_incomplete(reason)
        )

    def _note_active_loss(self, reason: str) -> None:
        with self._turn_lock:
            active = self._active_turn
            pending = self._pending_start
        collector = (
            active[2]
            if active is not None
            else pending[2]
            if pending is not None
            else None
        )
        if collector is not None:
            collector.note_loss(reason)
            self._activity("degraded", reason)

    def _sink_notification(self, message: Mapping[str, object]) -> bool:
        """Note every event of this thread's turn; consume agent-message deltas on arrival.

        Raw frames are never queued.
        """
        params = message.get("params")
        if not isinstance(params, Mapping):
            return False
        method = message.get("method")
        if method != "item/agentMessage/delta":
            with self._turn_lock:
                current = self._active_turn or self._pending_start
                actions = self._turn_actions
                sealed = params.get("turnId") in self._sealed_turn_ids
            if current is not None and params.get("threadId") == current[0].thread_id:
                current[2].note_activity()
                # A late item of an earlier turn on this thread is not this turn's action.
                if actions is not None and not sealed and method in ("item/started", "item/completed"):
                    record_item(actions, method, params)
            return False
        with self._turn_lock:
            active = self._active_turn
            pending = self._pending_start
            if active is not None:
                binding, turn_id, collector = active
                if params.get("threadId") == binding.thread_id:
                    self._route_delta(params, turn_id, collector)
                    self._activity("running", "agent-message-delta")
            elif pending is not None:
                binding, _sequence, collector = pending
                if params.get("threadId") == binding.thread_id:
                    self._buffer_pending_delta(params, collector)
        # Deltas for sealed, foreign, or idle turns carry no admissible
        # evidence; drop them instead of retaining raw frames.
        return True

    def _activity(self, phase: str, reason: str) -> None:
        if self.activity_observer is not None:
            self.activity_observer(phase, reason)

    def _buffer_pending_delta(
        self, params: Mapping[str, object], collector: TurnCollector
    ) -> None:
        observed_turn = params.get("turnId")
        if not isinstance(observed_turn, str) or not observed_turn:
            collector.note_loss(LOSS_IDENTITY_MISSING_OR_INVALID)
            return
        if observed_turn in self._sealed_turn_ids:
            return
        delta = params.get("delta")
        if not isinstance(delta, str):
            collector.note_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        size = max(1, len(delta.encode("utf-8")))
        if self._pending_delta_bytes + size > COMMUNICATION_BUDGET_BYTES:
            self._pending_overflowed = True
            return
        self._pending_deltas.append((observed_turn, params.get("itemId"), delta))
        self._pending_delta_bytes += size

    def _promote_pending(
        self, binding: TransportBinding, turn_id: str, collector: TurnCollector
    ) -> None:
        with self._turn_lock:
            entries = self._pending_deltas
            overflowed = self._pending_overflowed
            self._pending_deltas = []
            self._pending_delta_bytes = 0
            self._pending_overflowed = False
            for observed_turn, item_id, delta in entries:
                if observed_turn != turn_id:
                    continue
                self._observe_message_delta(collector, item_id, delta)
            if overflowed:
                collector.note_loss(LOSS_PENDING_OVERFLOW)
        self._collect_agent_deltas(binding, turn_id, collector)

    def _finalize_pending_failure(
        self, binding: TransportBinding, collector: TurnCollector
    ) -> None:
        """The turn never returned an id: buffered text cannot be attributed."""
        with self._turn_lock:
            entries = self._pending_deltas
            overflowed = self._pending_overflowed
            self._pending_deltas = []
            self._pending_delta_bytes = 0
            self._pending_overflowed = False
        if entries:
            collector.note_loss(LOSS_AMBIGUOUS_CORRELATION)
        if overflowed:
            collector.note_loss(LOSS_PENDING_OVERFLOW)
        self._collect_agent_deltas(binding, None, collector)

    def _collect_agent_deltas(
        self,
        binding: TransportBinding,
        turn_id: str | None,
        collector: TurnCollector,
    ) -> None:
        drain = getattr(self.rpc, "drain_notifications", None)
        if not callable(drain):
            if not self._sink_wired:
                # Neither streaming nor queued observation exists: absence of
                # deltas is unobservable, never determinate.
                collector.note_loss(
                    LOSS_OBSERVATION_UNAVAILABLE, epistemic_state="unknown"
                )
            return
        matching = drain(
            "item/agentMessage/delta",
            predicate=lambda params: params.get("threadId") == binding.thread_id,
        )
        for params in matching:
            self._route_delta(params, turn_id, collector)

    def _route_delta(
        self,
        params: Mapping[str, object],
        turn_id: str | None,
        collector: TurnCollector,
    ) -> None:
        observed_turn = params.get("turnId")
        if not isinstance(observed_turn, str) or not observed_turn:
            collector.note_loss(LOSS_IDENTITY_MISSING_OR_INVALID)
            return
        if observed_turn in self._sealed_turn_ids:
            return
        if turn_id is None:
            # The turn never learned its id, so this text cannot be proven
            # ours; exclude it rather than promote ambiguous evidence.
            collector.note_loss(LOSS_AMBIGUOUS_CORRELATION)
            return
        if observed_turn != turn_id:
            return
        delta = params.get("delta")
        if not isinstance(delta, str):
            collector.note_loss(LOSS_MALFORMED_AGENT_MESSAGE)
            return
        self._observe_message_delta(collector, params.get("itemId"), delta)

    def _observe_message_delta(self, collector: TurnCollector, item_id: object, delta: str) -> None:
        """Collect the delta into the message it belongs to; a new message replaces the earlier one.

        Codex writes progress notes (`phase: commentary`) and then its answer
        (`phase: final_answer`) as separate agent messages of one turn. The turn's
        communication is the last message, which is what `turn/completed` carries.
        """
        if isinstance(item_id, str) and item_id:
            if self._message_item is not None and item_id != self._message_item:
                collector.restart_text()
            self._message_item = item_id
        collector.observe_text(delta)

    @staticmethod
    def _terminal_agent_text(turn: Mapping[str, object]) -> str:
        output_text = turn.get("outputText")
        if isinstance(output_text, str):
            return output_text
        message = turn.get("message")
        if isinstance(message, Mapping) and isinstance(message.get("text"), str):
            return str(message["text"])
        items = turn.get("items")
        if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
            messages = [
                item for item in items
                if isinstance(item, Mapping) and item.get("type") in {"agentMessage", "agent_message"}
                and isinstance(item.get("text"), str)
            ]
            return str(messages[-1]["text"]) if messages else ""
        return ""

    def _require_generation(self, binding: TransportBinding) -> None:
        if binding.generation != self.generation:
            raise ValueError("stale-transport-generation")

    @staticmethod
    def _notification_matches(
        params: Mapping[str, object], thread_id: str, turn_id: str
    ) -> bool:
        if params.get("threadId") != thread_id:
            return False
        turn = params.get("turn")
        return isinstance(turn, Mapping) and turn.get("id") == turn_id

    @staticmethod
    def _mapping(container: Mapping[str, object], key: str) -> Mapping[str, object]:
        value = container.get(key)
        if not isinstance(value, Mapping):
            raise ValueError(f"{key} must be an object")
        return value

    @staticmethod
    def _string(container: Mapping[str, object], key: str) -> str:
        value = container.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} must be a non-empty string")
        return value
