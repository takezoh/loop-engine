"""Pure decisions and views of the provider lifecycle, shared by every wire protocol.

Nothing here performs I/O, starts threads or reads a clock: each function maps
what `ProviderSessions` observed to the wire view or the next step.
"""
from __future__ import annotations

from typing import Mapping

from .provider_port import SessionHandle, TurnEnd
from .turn_result import TurnCollector


REASON_LIMIT = 512


def failure_reason(error: BaseException) -> str:
    return f"{type(error).__name__}:{error}"[:REASON_LIMIT]


def incomplete_turn_result(identity: str) -> Mapping[str, object]:
    return TurnCollector(identity).seal_incomplete("transport_error").to_wire()


def running_receipt(session: SessionHandle) -> dict[str, object]:
    """The startup receipt of a turn that is still running."""
    return {
        "outcome": "running",
        "session_id": session.session_id,
        "provider_thread_id": session.durable_id,
        "binding_proof": dict(session.binding_proof) if session.binding_proof is not None else None,
        "provider_status": "running",
        "cleanup": "not_required",
    }


def terminal_view(
    *, session: SessionHandle | None, end: TurnEnd | None, error: BaseException | None,
    identity: str,
) -> dict[str, object]:
    """The terminal view of one turn from its end, or from the error that ended it."""
    session_id = session.session_id if session is not None else None
    if end is None:
        view: dict[str, object] = {
            "outcome": "post_start_failed",
            "session_id": session_id,
            "provider_status": "failed",
            "view": {"reason": failure_reason(error) if error is not None else "turn-ended-without-outcome"},
            "turn_result": incomplete_turn_result(identity),
            "cleanup": "not_required",
        }
    else:
        view = {
            **dict(end.detail),
            "outcome": (
                "cleanup_failed" if end.cleanup
                else "completed" if end.completed
                else "post_start_failed"
            ),
            "session_id": session_id,
            "provider_status": end.provider_status,
            "turn_result": dict(end.turn_result) if end.turn_result is not None else None,
            "cleanup": end.cleanup or "not_required",
        }
    if session is not None:
        view["provider_thread_id"] = session.durable_id
        if session.binding_proof is not None:
            view["binding_proof"] = dict(session.binding_proof)
    return view


def turn_evidence(
    view: Mapping[str, object], *, operation_id: str, fallback_identity: str
) -> dict[str, object]:
    """Terminal evidence the application records for the turn of `operation_id`."""
    turn_result = view.get("turn_result")
    identity = None
    if isinstance(turn_result, Mapping):
        candidate = turn_result.get("turn_identity")
        if isinstance(candidate, str) and candidate:
            identity = candidate
    cleanup = view.get("cleanup")
    return {
        "turn_identity": identity or fallback_identity,
        "turn_operation": operation_id,
        "outcome": view.get("outcome", "post_start_failed"),
        "provider_status": view.get("provider_status"),
        "session_id": view.get("session_id"),
        "turn_result": turn_result,
        "view": dict(view),
        "cleanup": cleanup if isinstance(cleanup, str) else "not_required",
    }


def unload_status(*, scheduled: bool, done: bool, error: str | None) -> tuple[str, str | None]:
    """The unload state of a session: completed, pending or failed with its reason."""
    if error is not None:
        return "failed", error
    if scheduled and not done:
        return "pending", None
    return "completed", None


def release_status(
    *, turn_running: bool, scheduled: bool, done: bool, error: str | None
) -> tuple[str, str | None]:
    """Whether the session of a turn is released: its turn ended and its unload completed.

    A turn that still runs holds its session, so its release is pending.
    """
    if turn_running:
        return "pending", "turn-running"
    status, reason = unload_status(scheduled=scheduled, done=done, error=error)
    return status, reason or ("unload-not-finished" if status == "pending" else None)


def continuation_spawns(
    *, known: bool, quarantined: bool, turn_running: bool, unloading: bool, loaded_on_live_host: bool,
) -> bool:
    """Whether a continue needs a spawn admission: never for one it will refuse."""
    if not known or quarantined or turn_running:
        return False
    return unloading or not loaded_on_live_host


def continuation_refusal(
    *, session_id: str | None, quarantined: bool, turn_running: bool,
    unload: str, unload_error: str | None, needs_spawn: bool, admitted: bool,
) -> dict[str, object] | None:
    """Why a continue cannot begin now, or None when it can.

    `unload` is the previous session unload after the bounded wait: "none",
    "pending", "failed" or "completed".
    """
    if session_id is None:
        return {"outcome": "session_lost"}
    if quarantined:
        return {"outcome": "recovery_pending", "session_id": session_id, "cleanup": "termination-unproven"}
    if turn_running:
        return {"outcome": "recovery_pending", "session_id": session_id, "provider_status": "running"}
    if unload == "pending":
        return {"outcome": "recovery_pending", "session_id": session_id, "provider_status": "unloading"}
    if unload == "failed":
        return {"outcome": "recovery_pending", "session_id": session_id, "cleanup": unload_error}
    if needs_spawn and not admitted:
        return {"outcome": "recovery_pending", "session_id": session_id}
    return None


def joined_diagnostics(*parts: str | None) -> str | None:
    """Join cleanup diagnostics, or None when there are none."""
    present = [part for part in parts if part]
    return ";".join(present)[:4096] if present else None
