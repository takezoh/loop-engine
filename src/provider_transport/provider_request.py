"""The request a caller passes to a provider session for one action on one turn."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ActionRequest:
    """One action (``start``, ``continue``, ``observe``, ``cancel``, ``close``) and, for a turn, what it runs.

    ``operation_id`` names the operation; a turn's ``model``, ``effort`` and
    ``prompt`` reach the agent, checked against the provider's bindings.
    """

    action: str
    operation_id: str
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    cwd: str | None = None
    prompt: str | None = None
