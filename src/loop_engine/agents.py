"""The engine's own port for agent turns. It names no provider and no wire protocol.

A session is the agent's conversation. The port hands the engine an opaque
token for the session a turn ran in; the engine stores it per execution and
passes it back to continue that session. The engine never reads a token.
"""
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class TurnRequest:
    """One turn to run: which unit and role it serves, the agent to use and what to tell it.

    `session` is the token of the session to continue, or None for a new session. `excluded_skills`
    is the contract's `{"plugin", "names"}` of skills the agent must not load, or None.
    """

    unit_id: str
    role: str
    round: int
    agent: str
    model: "str | None"
    effort: "str | None"
    cwd: str
    prompt: str
    session: "str | None" = None
    excluded_skills: "dict | None" = None


@dataclass(frozen=True)
class TurnOutcome:
    """How a turn ended. `actions` is the provider's record of what the agent did, when it gave one.

    `session` is the token of the session the turn ran in, None when no session bound.
    `refused` counts the agent's requests for approval that were refused in this turn.
    `session_lost` says the requested session could not be continued and no turn ran.
    `released` is False when the turn's session was not released, so a process of the agent may
    still be using the working directory; such a turn is never completed.
    """

    completed: bool
    text: "str | None"
    actions: "Mapping[str, object] | None"
    detail: "str | None" = None
    session: "str | None" = None
    refused: int = 0
    session_lost: bool = False
    released: bool = True

    def to_wire(self):
        return {"completed": self.completed, "text": self.text, "actions": self.actions, "detail": self.detail,
                "session": self.session, "refused": self.refused, "session_lost": self.session_lost,
                "released": self.released}


class AgentTurns(Protocol):
    def known(self, agent: str) -> bool:
        """Whether the installed catalog offers this agent."""
        ...

    def run(self, request: TurnRequest, cancelled) -> TurnOutcome:
        """Run the turn to its end and return once its session is released. `cancelled` is an event;
        once set, stop the turn and return.

        With `request.session`, the turn continues that session; when it cannot,
        the outcome says `session_lost` and nothing was sent to the agent.
        """
        ...

    def end_session(self, agent: str, session: str) -> None:
        """Close a session no turn will continue."""
        ...

    def close(self) -> None:
        """Release every session, then stop every agent process this port started."""
        ...
