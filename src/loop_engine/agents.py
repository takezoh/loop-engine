"""The engine's own port for agent turns. It names no provider and no wire protocol."""
from dataclasses import dataclass
from typing import Mapping, Protocol


@dataclass(frozen=True)
class TurnRequest:
    """One turn to run: which unit and role it serves, the agent to use and what to tell it."""

    unit_id: str
    role: str
    round: int
    agent: str
    model: "str | None"
    effort: "str | None"
    cwd: str
    prompt: str


@dataclass(frozen=True)
class TurnOutcome:
    """How a turn ended. `actions` is the provider's record of what the agent did, when it gave one."""

    completed: bool
    text: "str | None"
    actions: "Mapping[str, object] | None"
    detail: "str | None" = None

    def to_wire(self):
        return {"completed": self.completed, "text": self.text, "actions": self.actions, "detail": self.detail}


class AgentTurns(Protocol):
    def known(self, agent: str) -> bool:
        """Whether the installed catalog offers this agent."""
        ...

    def run(self, request: TurnRequest, cancelled) -> TurnOutcome:
        """Run the turn to its end. `cancelled` is an event; once set, stop the turn and return."""
        ...

    def close(self) -> None:
        """Stop every agent process this port started."""
        ...
