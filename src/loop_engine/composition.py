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
from dataclasses import replace
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
    """The protocol texts by name: each role's first message and its follow-up."""
    templates = {}
    for role in ("writer", "reviewer", "reporter"):
        for name in (role, f"{role}-follow-up"):
            with open(os.path.join(HERE, "protocol", f"{name}.md"), encoding="utf-8") as handle:
                templates[name] = handle.read()
    return templates


class TransportAgents:
    """`AgentTurns` over the transport's `ProviderSessions`, one per catalog provider.

    A session token is the provider's durable session identity (the Codex
    thread, the ACP or Pi session). Within this process a token maps to the
    provider-session execution that holds the session; after a restart the
    token alone binds it again. A turn returns once the transport has released
    its session; a later turn of the execution loads it again by the token.
    """

    def __init__(self, *, origin, state_dir):
        from cross_host_transport.subsession.cell_membership import create_cell_membership
        from cross_host_transport.subsession.cwd_authority import ChildLedger, CwdAuthority
        from cross_host_transport.subsession.provider_catalog import provider_names

        os.makedirs(origin, exist_ok=True)
        self.origin, self.state_dir = origin, state_dir
        self.cell_id = uuid.uuid4().hex
        self.membership = create_cell_membership()
        self.ledger = ChildLedger(cell_id=self.cell_id, max_children=16,
                                  release_certainty=self.membership.release_certain)
        self.authority = CwdAuthority.capture(origin)
        self.names = frozenset(provider_names())
        self._sessions = {}
        self._held = {}  # (agent, token) -> the provider-session execution holding it
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
                driver = driver_for(load_provider(agent), origin=Path(self.origin), state_dir=Path(self.state_dir))
                self._sessions[agent] = ProviderSessions(driver, cell_id=self.cell_id)
            return self._sessions[agent]

    def _admission(self, cwd):
        from cross_host_transport.subsession.cwd_authority import SpawnAdmission

        return SpawnAdmission(
            reservation=self.ledger.reserve(), cwd_lease=self.authority.admit(cwd),
            cell_id=self.cell_id, epoch=1, fence_check=lambda _epoch: True,
            cell_membership=self.membership,
        )

    @staticmethod
    def _abort_unused(admission):
        if admission is not None and not admission.spawned_by_adapter:
            try:
                admission.abort()  # the turn joined a host that is already running, or never launched one
            except Exception:
                pass

    def run(self, request, cancelled):
        from cross_host_transport.subsession.mcp_protocol import ActionRequest

        sessions = self._sessions_for(request.agent)
        operation = f"{request.unit_id}-{request.round}-{request.role}-{uuid.uuid4().hex[:12]}"
        turn = dict(operation_id=operation, consumer=CONSUMER, provider=request.agent, model=request.model,
                    effort=request.effort, cwd=request.cwd, prompt=request.prompt)
        admission = None
        if request.session is None:
            execution = operation
            try:
                admission = self._admission(request.cwd)
                sessions.bind_pending_execution(execution, operation)
                sessions.start(ActionRequest(action="start", **turn), admission)
                self._abort_unused(admission)
            except Exception as error:
                self._abort_unused(admission)
                return TurnOutcome(False, None, None, f"agent-start-failed: {type(error).__name__}: {error}"[:1000])
        else:
            with self._lock:
                execution = self._held.get((request.agent, request.session))
            if execution is None:  # this process has not held the session: bind it by its token
                execution = operation
                sessions.recover_binding(execution, request.session)
            try:
                admission = self._admission(request.cwd)
                receipt = sessions.action(ActionRequest(action="continue", **turn),
                                          SimpleNamespace(execution_id=execution), admission)
                self._abort_unused(admission)
            except Exception as error:
                self._abort_unused(admission)
                receipt = {"outcome": "session_lost", "reason": f"{type(error).__name__}: {error}"}
            if receipt.get("outcome") not in ("running", "completed", "post_start_failed", "cleanup_failed"):
                self._forget(request.agent, request.session, execution)
                detail = str(receipt.get("reason") or receipt.get("provider_status") or receipt.get("outcome"))
                return TurnOutcome(False, None, None, f"session-not-continuable: {detail}"[:1000],
                                   session_lost=True)
        snapshot = SimpleNamespace(execution_id=execution)
        interrupted = False
        while not sessions.wait_turn(execution, WAIT_SLICE_SECONDS):
            if cancelled.is_set() and not interrupted:
                interrupted = True
                try:
                    sessions.action(ActionRequest(action="cancel", operation_id=operation + "-cancel",
                                                  consumer=CONSUMER), snapshot)
                except Exception:
                    pass
                if not sessions.wait_turn(execution, CANCEL_GRACE_SECONDS):
                    break
        try:
            view = sessions.action(ActionRequest(action="observe", operation_id=operation + "-observe",
                                                 consumer=CONSUMER), snapshot)
        except Exception as error:
            view = {"outcome": "post_start_failed", "view": {"reason": f"{type(error).__name__}: {error}"}}
        # The turn is over only once its session is released: until then a process of the agent,
        # and its sandbox, may still be in the working directory.
        try:
            release = sessions.wait_release(execution)
        except Exception as error:
            release = {"unload_status": "failed", "cleanup": f"{type(error).__name__}: {error}"}
        token = view.get("provider_thread_id")
        if isinstance(token, str) and token:
            with self._lock:
                self._held[(request.agent, token)] = execution
        else:
            token = None
        return released_outcome(turn_outcome(view, token), release)

    def _forget(self, agent, token, execution):
        from cross_host_transport.subsession.mcp_protocol import ActionRequest

        with self._lock:
            if self._held.get((agent, token)) == execution:
                del self._held[(agent, token)]
            sessions = self._sessions.get(agent)
        if sessions is None:
            return
        try:
            sessions.action(ActionRequest(action="close", operation_id=f"{execution}-close-{uuid.uuid4().hex[:8]}",
                                          consumer=CONSUMER), SimpleNamespace(execution_id=execution))
        except Exception:
            pass

    def end_session(self, agent, session):
        with self._lock:
            execution = self._held.get((agent, session))
        if execution is not None:
            self._forget(agent, session, execution)

    def close(self):
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._held.clear()
        for provider in sessions:
            try:
                provider.shutdown()  # releases its sessions before it closes its hosts
            except Exception:
                pass
        try:
            self.ledger.terminate_owned()  # the last resort for a process that did not end
        except Exception:
            pass


def refused_count(actions):
    """How many of the agent's requests the transport refused in the turn."""
    if not isinstance(actions, dict):
        return 0
    refused, dropped = actions.get("refused"), actions.get("refused_dropped")
    return (len(refused) if isinstance(refused, list) else 0) + (
        dropped if isinstance(dropped, int) and not isinstance(dropped, bool) else 0)


def turn_outcome(view, session=None):
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
                       actions if isinstance(actions, dict) else None, detail, session, refused_count(actions))


def released_outcome(outcome, release):
    """The turn's outcome, or a non-completed one naming why its session was not released."""
    if release.get("unload_status") == "completed":
        return outcome
    reason = release.get("cleanup") or release.get("unload_status")
    return replace(outcome, completed=False, detail=f"agent-session-not-released: {reason}"[:1000], released=False)


def build(environ):
    """The engine of this installation and the path of its process lock."""
    root = home_root(environ)
    os.makedirs(root, mode=0o700, exist_ok=True)
    worktrees = worktrees_root(environ)
    agents = TransportAgents(origin=worktrees, state_dir=os.path.join(root, "agents"))
    installation = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    own_code = (os.path.dirname(os.path.dirname(HERE)),
                os.path.join(installation, "packages", "cross-host-transport"))
    engine = Engine(
        root, git=Git(environ), agents=agents, templates=load_templates(),
        runner=(sys.executable, os.path.join(HERE, "check_runner.py")), worktrees=worktrees,
        own_code=own_code,
    )
    return engine, os.path.join(root, "engine.lock")
