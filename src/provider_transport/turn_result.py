"""Provider-neutral bounded communication result for one started turn."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
import time
from typing import Final


WIRE_VERSION: Final = 1
COMMUNICATION_BUDGET_BYTES: Final = 16_384
HEAD_BUDGET_BYTES: Final = 8_192
TAIL_BUDGET_BYTES: Final = 8_192
UINT64_MAX: Final = (1 << 64) - 1
MAX_LOSS_REASONS: Final = 8

PRESENCE_VALUES: Final = frozenset({"present", "absent", "unknown"})
COMPLETION_VALUES: Final = frozenset({"completed", "incomplete"})
EPISTEMIC_VALUES: Final = frozenset(
    {"determinate", "unknown", "inconclusive", "conflicting_evidence"}
)

# Provider-neutral loss vocabulary: equivalent ACP/Codex event histories must
# yield identical tokens, so adapters never emit provider-prefixed reasons.
LOSS_IDENTITY_MISSING_OR_INVALID: Final = "identity_missing_or_invalid"
LOSS_MALFORMED_AGENT_MESSAGE: Final = "malformed_agent_message"
LOSS_MALFORMED_FRAME: Final = "malformed_frame"
LOSS_AMBIGUOUS_CORRELATION: Final = "ambiguous_correlation"
LOSS_PENDING_OVERFLOW: Final = "pending_overflow"
LOSS_SOURCE_CONFLICT: Final = "source_conflict"
LOSS_OBSERVATION_UNAVAILABLE: Final = "observation_unavailable"
LOSS_TRANSPORT_EOF: Final = "transport_eof"
LOSS_TURN_TIMEOUT: Final = "turn_timeout"
LOSS_TRANSPORT_ERROR: Final = "transport_error"
LOSS_TURN_NOT_COMPLETED: Final = "turn_not_completed"
LOSS_INTERNAL_CONTRACT_VIOLATION: Final = "internal_contract_violation"


@dataclass(frozen=True)
class Communication:
    text: str
    presence: str
    truncated: bool
    dropped_bytes: int
    dropped_bytes_saturated: bool

    def to_wire(self) -> dict[str, object]:
        return {
            "text": self.text,
            "presence": self.presence,
            "truncated": self.truncated,
            "dropped_bytes": self.dropped_bytes,
            "dropped_bytes_saturated": self.dropped_bytes_saturated,
        }


@dataclass(frozen=True)
class TransportEvidence:
    completion: str
    epistemic_state: str
    loss_reasons: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "completion": self.completion,
            "epistemic_state": self.epistemic_state,
            "loss_reasons": list(self.loss_reasons),
        }


@dataclass(frozen=True)
class TurnResult:
    turn_identity: str
    communication: Communication
    transport_evidence: TransportEvidence
    version: int = WIRE_VERSION

    def __post_init__(self) -> None:
        if self.version != WIRE_VERSION:
            raise ValueError("unsupported-turn-result-version")
        if not self.turn_identity:
            raise ValueError("turn-identity-must-be-nonempty")
        if self.communication.presence not in PRESENCE_VALUES:
            raise ValueError("invalid-communication-presence")
        evidence = self.transport_evidence
        if evidence.completion not in COMPLETION_VALUES:
            raise ValueError("invalid-turn-completion")
        if evidence.epistemic_state not in EPISTEMIC_VALUES:
            raise ValueError("invalid-turn-epistemic-state")
        if len(evidence.loss_reasons) > MAX_LOSS_REASONS:
            raise ValueError("too-many-turn-loss-reasons")
        if (
            not isinstance(self.communication.dropped_bytes, int)
            or isinstance(self.communication.dropped_bytes, bool)
            or not 0 <= self.communication.dropped_bytes <= UINT64_MAX
            or self.communication.truncated != (self.communication.dropped_bytes > 0)
            or (
                self.communication.dropped_bytes_saturated
                and self.communication.dropped_bytes != UINT64_MAX
            )
        ):
            raise ValueError("invalid-turn-truncation-evidence")
        if self.communication.presence == "absent" and (
            evidence.completion != "completed"
            or evidence.epistemic_state != "determinate"
            or evidence.loss_reasons
            or self.communication.text
        ):
            raise ValueError("invalid-completed-absence")
        if evidence.completion == "incomplete" and (
            self.communication.presence == "absent" or not evidence.loss_reasons
        ):
            raise ValueError("invalid-incomplete-turn-result")
        encoded = self.communication.text.encode("utf-8")
        if len(encoded) > COMMUNICATION_BUDGET_BYTES:
            raise ValueError("turn-communication-over-budget")

    def to_wire(self) -> dict[str, object]:
        return {
            "version": self.version,
            "turn_identity": self.turn_identity,
            "communication": self.communication.to_wire(),
            "transport_evidence": self.transport_evidence.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: object) -> "TurnResult":
        if not isinstance(value, dict) or value.get("version") != WIRE_VERSION:
            raise ValueError("unsupported-turn-result-version")
        if set(value) != {
            "version", "turn_identity", "communication", "transport_evidence"
        }:
            raise ValueError("malformed-turn-result")
        communication = value.get("communication")
        evidence = value.get("transport_evidence")
        if not isinstance(communication, dict) or not isinstance(evidence, dict):
            raise ValueError("malformed-turn-result")
        if set(communication) != {
            "text", "presence", "truncated", "dropped_bytes",
            "dropped_bytes_saturated",
        } or set(evidence) != {"completion", "epistemic_state", "loss_reasons"}:
            raise ValueError("malformed-turn-result")
        try:
            identity = value["turn_identity"]
            text = communication["text"]
            presence = communication["presence"]
            truncated = communication["truncated"]
            dropped_bytes = communication["dropped_bytes"]
            saturated = communication["dropped_bytes_saturated"]
            completion = evidence["completion"]
            epistemic_state = evidence["epistemic_state"]
            reasons = evidence["loss_reasons"]
            if not isinstance(identity, str) or not identity:
                raise ValueError("malformed-turn-identity")
            if not isinstance(text, str) or not isinstance(presence, str):
                raise ValueError("malformed-turn-communication")
            if not isinstance(truncated, bool) or not isinstance(saturated, bool):
                raise ValueError("malformed-turn-truncation")
            if (
                not isinstance(dropped_bytes, int)
                or isinstance(dropped_bytes, bool)
                or not 0 <= dropped_bytes <= UINT64_MAX
            ):
                raise ValueError("malformed-dropped-bytes")
            if not isinstance(completion, str) or not isinstance(epistemic_state, str):
                raise ValueError("malformed-turn-evidence")
            if not isinstance(reasons, list) or not all(
                isinstance(reason, str) and reason for reason in reasons
            ):
                raise ValueError("malformed-turn-loss-reasons")
            return cls(
                turn_identity=identity,
                communication=Communication(
                    text=text,
                    presence=presence,
                    truncated=truncated,
                    dropped_bytes=dropped_bytes,
                    dropped_bytes_saturated=saturated,
                ),
                transport_evidence=TransportEvidence(
                    completion=completion,
                    epistemic_state=epistemic_state,
                    loss_reasons=tuple(reasons),
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("malformed-turn-result") from exc


@dataclass(frozen=True)
class TurnProgress:
    """What an unsealed turn has shown: its bounded text and when its provider last reported any event."""

    output: str
    activity_at: float | None


class TurnCollector:
    """Thread-safe, exactly-once, fixed-memory collector for normalized text facts."""

    def __init__(
        self, turn_identity: str, *, initial_total_bytes: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not turn_identity:
            raise ValueError("turn-identity-must-be-nonempty")
        if initial_total_bytes < 0:
            raise ValueError("initial-total-bytes-must-be-nonnegative")
        self.turn_identity = turn_identity
        self._clock = clock
        self._activity_at: float | None = None
        self._total_bytes = initial_total_bytes
        self._head = bytearray()
        self._tail = bytearray()
        self._loss_reasons: list[str] = []
        self._epistemic_state = "determinate"
        self._sealed: TurnResult | None = None
        self._lock = Lock()

    @property
    def sealed(self) -> bool:
        with self._lock:
            return self._sealed is not None

    @property
    def truncated(self) -> bool:
        with self._lock:
            if self._sealed is not None:
                return self._sealed.communication.truncated
            return self._total_bytes > COMMUNICATION_BUDGET_BYTES

    def text_preview(self) -> str:
        """Return the current bounded projection without sealing or exposing raw fragments."""
        with self._lock:
            if self._sealed is not None:
                return self._sealed.communication.text
            return self._project_bytes().decode("utf-8")

    def progress(self) -> TurnProgress:
        """The current bounded text and the time of the latest provider event, without sealing."""
        with self._lock:
            if self._sealed is not None:
                return TurnProgress(self._sealed.communication.text, self._activity_at)
            return TurnProgress(self._project_bytes().decode("utf-8"), self._activity_at)

    def note_activity(self) -> None:
        """Record that the provider reported an event for this turn, whatever its kind."""
        with self._lock:
            if self._sealed is None:
                self._activity_at = self._clock()

    def observe_text(self, text: str) -> bool:
        if not isinstance(text, str):
            raise TypeError("turn communication text must be str")
        if not text:
            return True
        encoded = text.encode("utf-8")
        with self._lock:
            if self._sealed is not None:
                return False
            self._activity_at = self._clock()
            self._total_bytes += len(encoded)
            remaining = HEAD_BUDGET_BYTES - len(self._head)
            if remaining > 0:
                self._head.extend(encoded[:remaining])
                encoded = encoded[remaining:]
            if encoded:
                self._tail.extend(encoded)
                if len(self._tail) > TAIL_BUDGET_BYTES + 3:
                    del self._tail[: len(self._tail) - (TAIL_BUDGET_BYTES + 3)]
            return True

    def restart_text(self) -> None:
        """Drop the text observed so far: it belonged to an earlier message of the same turn.

        An agent can write several messages in one turn, progress notes and then its
        answer. The turn's communication is the last one. Losses already noted are kept.
        """
        with self._lock:
            if self._sealed is None:
                self._head.clear()
                self._tail.clear()
                self._total_bytes = 0

    def note_loss(self, reason: str, *, epistemic_state: str = "inconclusive") -> None:
        if not reason:
            raise ValueError("loss-reason-must-be-nonempty")
        if epistemic_state not in EPISTEMIC_VALUES - {"determinate"}:
            raise ValueError("loss-requires-nondeterminate-epistemic-state")
        with self._lock:
            if self._sealed is not None:
                return
            if reason not in self._loss_reasons:
                if len(self._loss_reasons) < MAX_LOSS_REASONS:
                    self._loss_reasons.append(reason)
                elif "loss_reason_overflow" not in self._loss_reasons:
                    self._loss_reasons[-1] = "loss_reason_overflow"
            precedence = {"unknown": 1, "inconclusive": 2, "conflicting_evidence": 3}
            if precedence[epistemic_state] > precedence.get(self._epistemic_state, 0):
                self._epistemic_state = epistemic_state

    def seal_completed(self) -> TurnResult:
        with self._lock:
            if self._sealed is not None:
                return self._sealed
            if self._loss_reasons:
                return self._seal_locked(completed=False)
            return self._seal_locked(completed=True)

    def seal_incomplete(
        self, reason: str, *, epistemic_state: str = "inconclusive"
    ) -> TurnResult:
        self.note_loss(reason, epistemic_state=epistemic_state)
        with self._lock:
            if self._sealed is not None:
                return self._sealed
            return self._seal_locked(completed=False)

    def _seal_locked(self, *, completed: bool) -> TurnResult:
        text_bytes = self._project_bytes()
        text = text_bytes.decode("utf-8")
        dropped = max(0, self._total_bytes - len(text_bytes))
        saturated = dropped > UINT64_MAX
        if completed:
            presence = "present" if text else "absent"
            evidence = TransportEvidence("completed", "determinate", ())
        else:
            presence = "present" if text else "unknown"
            reasons = tuple(self._loss_reasons or ["internal_contract_violation"])
            state = self._epistemic_state
            if state == "determinate":
                state = "inconclusive"
            evidence = TransportEvidence("incomplete", state, reasons)
        self._sealed = TurnResult(
            self.turn_identity,
            Communication(
                text=text,
                presence=presence,
                truncated=dropped > 0,
                dropped_bytes=min(dropped, UINT64_MAX),
                dropped_bytes_saturated=saturated,
            ),
            evidence,
        )
        self._head.clear()
        self._tail.clear()
        self._loss_reasons.clear()
        return self._sealed

    def project_reference_text(self, text: str) -> str:
        """Project text through the same head/tail retention as this collector's input."""
        reference = TurnCollector(self.turn_identity)
        reference.observe_text(text)
        return reference.text_preview()

    def _project_bytes(self) -> bytes:
        if self._total_bytes <= COMMUNICATION_BUDGET_BYTES:
            return bytes(self._head + self._tail)
        head = bytes(self._head).decode("utf-8", errors="ignore").encode("utf-8")
        raw_tail = bytes(self._tail[-(TAIL_BUDGET_BYTES + 3) :])
        window = raw_tail[-TAIL_BUDGET_BYTES:]
        tail = b""
        for offset in range(min(4, len(window) + 1)):
            candidate = window[offset:]
            try:
                candidate.decode("utf-8")
            except UnicodeDecodeError:
                continue
            tail = candidate
            break
        return head + tail
