"""Wire the engine to this installation: state home, git, protocol templates and agent turns.

This is the only module that touches the provider transport. The engine core
sees agents through `agents.AgentTurns`; here that port runs each turn through
the transport's provider sessions and protocol drivers, with the catalog's
commands and the user's own agent settings.
"""
import os
import sys
import threading
import uuid
from types import SimpleNamespace

from .agents import TurnOutcome
from .git import Git
from .service import Engine
from .store import home_root, worktrees_root

HERE = os.path.dirname(os.path.abspath(__file__))
CANCEL_GRACE_SECONDS = 30.0
WAIT_SLICE_SECONDS = 0.5
CONSUMER = "task-execution"


def load_templates():
    templates = {}
    for role in ("writer", "reviewer", "reporter"):
        with open(os.path.join(HERE, "protocol", f"{role}.md"), encoding="utf-8") as handle:
            templates[role] = handle.read()
    return templates


class TransportAgents:
    """`AgentTurns` over the transport's `ProviderSessions`, one per catalog provider."""

    def __init__(self, *, origin, state_dir, grants):
        from cross_host_transport.subsession.cell_membership import create_cell_membership
        from cross_host_transport.subsession.cwd_authority import ChildLedger, CwdAuthority
        from cross_host_transport.subsession.provider_catalog import provider_names

        os.makedirs(origin, exist_ok=True)
        self.origin, self.state_dir, self.grants = origin, state_dir, dict(grants)
        self.cell_id = uuid.uuid4().hex
        self.membership = create_cell_membership()
        self.ledger = ChildLedger(cell_id=self.cell_id, max_children=16,
                                  release_certainty=self.membership.release_certain)
        self.authority = CwdAuthority.capture(origin)
        self.names = frozenset(provider_names())
        self._sessions = {}
        self._lock = threading.Lock()

    def known(self, agent):
        return agent in self.names

    def _sessions_for(self, agent):
        from pathlib import Path
        from cross_host_transport.subsession.provider_catalog import load_provider
        from cross_host_transport.subsession.provider_drivers import driver_for
        from cross_host_transport.subsession.provider_sessions import ProviderSessions

        with self._lock:
            if agent not in self._sessions:
                driver = driver_for(load_provider(agent), origin=Path(self.origin), grants=self.grants,
                                    state_dir=Path(self.state_dir))
                self._sessions[agent] = ProviderSessions(driver, cell_id=self.cell_id)
            return self._sessions[agent]

    def run(self, request, cancelled):
        from cross_host_transport.subsession.cwd_authority import SpawnAdmission
        from cross_host_transport.subsession.mcp_protocol import ActionRequest

        sessions = self._sessions_for(request.agent)
        operation = f"{request.unit_id}-{request.round}-{request.role}-{uuid.uuid4().hex[:12]}"
        snapshot = SimpleNamespace(execution_id=operation)
        admission = None
        try:
            admission = SpawnAdmission(
                reservation=self.ledger.reserve(), cwd_lease=self.authority.admit(request.cwd),
                cell_id=self.cell_id, epoch=1, fence_check=lambda _epoch: True,
                cell_membership=self.membership,
            )
            sessions.bind_pending_execution(operation, operation)
            sessions.start(ActionRequest(
                action="start", operation_id=operation, consumer=CONSUMER, provider=request.agent,
                model=request.model, effort=request.effort, cwd=request.cwd, prompt=request.prompt,
            ), admission)
            if not admission.spawned_by_adapter:
                admission.abort()  # the turn joined a host that is already running
        except Exception as error:
            if admission is not None and not admission.spawned_by_adapter:
                try:
                    admission.abort()
                except Exception:
                    pass
            return TurnOutcome(False, None, None, f"agent-start-failed: {type(error).__name__}: {error}"[:1000])
        interrupted = False
        while not sessions.wait_turn(operation, WAIT_SLICE_SECONDS):
            if cancelled.is_set() and not interrupted:
                interrupted = True
                try:
                    sessions.action(ActionRequest(action="cancel", operation_id=operation + "-cancel",
                                                  consumer=CONSUMER), snapshot)
                except Exception:
                    pass
                if not sessions.wait_turn(operation, CANCEL_GRACE_SECONDS):
                    break
        try:
            view = sessions.action(ActionRequest(action="observe", operation_id=operation + "-observe",
                                                 consumer=CONSUMER), snapshot)
        except Exception as error:
            view = {"outcome": "post_start_failed", "view": {"reason": f"{type(error).__name__}: {error}"}}
        try:
            sessions.action(ActionRequest(action="close", operation_id=operation + "-close",
                                          consumer=CONSUMER), snapshot)
        except Exception:
            pass
        return turn_outcome(view)

    def close(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for provider in sessions:
            try:
                provider.shutdown()
            except Exception:
                pass
        try:
            self.ledger.terminate_owned()
        except Exception:
            pass


def turn_outcome(view):
    """The engine's view of a transport terminal view: completed only with a complete message."""
    result = view.get("turn_result")
    communication = result.get("communication") if isinstance(result, dict) else None
    evidence = result.get("transport_evidence") if isinstance(result, dict) else None
    text = communication.get("text") if isinstance(communication, dict) else None
    complete = (
        view.get("outcome") == "completed"
        and isinstance(evidence, dict) and evidence.get("completion") == "completed"
        and isinstance(communication, dict) and communication.get("presence") == "present"
    )
    actions = view.get("actions")
    detail = None
    if not complete:
        reason = view.get("view", {}).get("reason") if isinstance(view.get("view"), dict) else None
        detail = str(reason or view.get("reason") or view.get("provider_status") or view.get("outcome"))[:1000]
    return TurnOutcome(complete, text if isinstance(text, str) else None,
                       actions if isinstance(actions, dict) else None, detail)


def build(environ):
    """The engine of this installation and the path of its process lock."""
    root = home_root(environ)
    os.makedirs(root, mode=0o700, exist_ok=True)
    grants = {
        "filesystem_read": environ.get("DEV_SKILLS_MCP_FS_READ") == "1",
        "filesystem_write": environ.get("DEV_SKILLS_MCP_FS_WRITE") == "1",
        "terminal": environ.get("DEV_SKILLS_MCP_TERMINAL") == "1",
    }
    worktrees = worktrees_root(environ)
    agents = TransportAgents(origin=worktrees, state_dir=os.path.join(root, "agents"), grants=grants)
    installation = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    own_code = (os.path.dirname(os.path.dirname(HERE)),
                os.path.join(installation, "packages", "cross-host-transport"))
    engine = Engine(
        root, git=Git(environ), agents=agents, templates=load_templates(),
        runner=(sys.executable, os.path.join(HERE, "check_runner.py")), worktrees=worktrees,
        own_code=own_code,
    )
    return engine, os.path.join(root, "engine.lock")
