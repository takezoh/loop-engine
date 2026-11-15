"""Shared fixtures: a real git repository, a scripted agent port and an engine over them."""
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from dataclasses import replace

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from loop_engine import composition, facts, store  # noqa: E402
from loop_engine.agents import TurnOutcome  # noqa: E402
from loop_engine.check_runner import tree_digest  # noqa: E402
from loop_engine.git import Git  # noqa: E402
from loop_engine.platform_facts import CURRENT  # noqa: E402
from loop_engine.service import Engine  # noqa: E402

RUNNER = os.path.join(SRC, "loop_engine", "check_runner.py")


def published_runner(root):
    """Where an engine over the state home `root` publishes the check program agents run."""
    with open(RUNNER, "rb") as handle:
        return store.check_program_path(root, hashlib.sha256(handle.read()).hexdigest())


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args], cwd=cwd,
                   check=True, capture_output=True, stdin=subprocess.DEVNULL)


IGNORE = ["__pycache__"]


def recorded(directory):
    """Git's record of a directory's files, as `changed_paths` compares a worktree with it."""
    git(directory, "init", "-q")
    git(directory, "add", "-A")
    git(directory, "commit", "-qm", "base", "--allow-empty")
    return Git(os.environ).tree_files(directory, os.path.join(directory, ".git"), "HEAD")


def git_out(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL).stdout.strip()


def actions(*entries):
    """An action record from (kind, command, output[, paths]) entries, in order."""
    return {"version": 1, "dropped": 0, "entries": [
        {"seq": index + 1, "kind": entry[0], "name": None, "command": entry[1], "command_truncated": False,
         "paths": list(entry[3]) if len(entry) > 3 else [], "ended": True, "output_tail": entry[2],
         "output_truncated": False, "exit_code": None}
        for index, entry in enumerate(entries)]}


CHECKS_FILES = {}  # worktree -> the checks file the engine wrote for the check program


def status_line(cwd, name, code, tree="candidate"):
    """The line the check program prints, computed as it computes it from the unit's checks file."""
    with open(CHECKS_FILES[cwd], encoding="utf-8") as handle:
        committed = json.load(handle)["committed"]
    return f"loop-engine-check {name} tree={tree} digest={tree_digest(cwd, IGNORE, committed)} exit={code}"


def passing(commands, cwd, *, before=()):
    """The record of a turn that edited, then ran every check command on the present tree with exit 0."""
    return actions(*before, *[("command", command, "ok\n" + status_line(cwd, name, 0) + "\n")
                              for name, command in commands.items()])


def files_in(cwd):
    found = []
    for folder, directories, files in os.walk(cwd):
        directories[:] = [name for name in directories if name != ".git"]
        found += [os.path.relpath(os.path.join(folder, name), cwd) for name in files if name != ".git"]
    return sorted(found)


def cite(cwd):
    """A quote of a line that is in a file the turn produced."""
    for name in ("out.txt", "notes.md", *files_in(cwd)):
        path = os.path.join(cwd, name)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                lines = handle.read().splitlines()
            for number, line in enumerate(lines, 1):
                if len(line.strip()) >= 8:
                    return {"path": name, "line": number, "quote": line.strip()}
    raise AssertionError("nothing to cite")


def examined(cwd):
    return actions(("read", None, None, files_in(cwd)))


def approve(request, text="looks right"):
    reply = {"criteria": [{"id": "c1", "met": True, "evidence": [cite(request.cwd)]}], "findings": []}
    return TurnOutcome(True, text + "\n" + json.dumps(reply), examined(request.cwd))


def changes(request, *findings):
    reply = {"criteria": [{"id": "c1", "met": False, "evidence": []}], "findings": [
        {"severity": "major", "criterion": "c1", **cite(request.cwd), "description": text} for text in findings]}
    return TurnOutcome(True, json.dumps(reply), examined(request.cwd))


def produced(request, commands, name="out.txt", text="made by the producer\n"):
    """A producer turn that writes a file and shows every check passing afterwards."""
    write(request.cwd, name, text)
    return TurnOutcome(True, "wrote " + name, passing(commands, request.cwd, before=(("edit", None, None),)))


class ScriptedAgents:
    """An `AgentTurns` whose turns are functions of (request, check commands, cancelled event).

    It keeps sessions as a provider does: a new turn opens one, a continued turn
    runs in it, and a session the provider no longer has (`durable`, shared
    with the next engine's agents to model a restart, minus `lost`) cannot be
    continued, so no turn runs.
    """

    def __init__(self, root, script, durable=None):
        self.root, self.script, self.requests, self.closed = root, script, [], False
        self.nth = {}  # how many turns each execution has had, the present one included
        self.durable = durable if durable is not None else set()
        self.lost, self.ended, self.opened = set(), [], []

    def known(self, agent):
        return agent in ("alpha", "beta")

    def run(self, request, cancelled):
        self.requests.append(request)
        if request.session is not None and (request.session not in self.durable or request.session in self.lost):
            return TurnOutcome(False, None, None, "session-not-continuable: scripted", session_lost=True)
        self.nth[request.role] = self.nth.get(request.role, 0) + 1
        unit = store.UnitStore(self.root, request.unit_id)
        runner = CURRENT.quote([sys.executable, "-I", published_runner(self.root), unit.path("checks.json")])
        CHECKS_FILES[request.cwd] = unit.path("checks.json")
        commands = {}
        for name in facts.check_names(unit.contract())[0]:
            commands[name] = facts.expected_command(runner, name, "candidate")
        outcome = self.script(request, commands, cancelled)
        if request.session is None and (outcome.detail or "").startswith("agent-start-failed"):
            return outcome  # no session bound
        session = request.session
        if session is None:
            session = f"{request.agent}-session-{os.urandom(4).hex()}"
            self.durable.add(session)
            self.opened.append((request.role, session))
        return replace(outcome, session=session)

    def end_session(self, agent, session):
        self.ended.append((agent, session))
        self.durable.discard(session)

    def close(self):
        self.closed = True


def write(cwd, name, text):
    path = os.path.join(cwd, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class EngineCase(unittest.TestCase):
    """A repository with one check, a state home and an engine with scripted agents."""

    checks = {"unit": {"argv": [sys.executable, "-c", "import sys; sys.exit(0)"], "test_paths": ["tests"]}}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = os.path.realpath(self._tmp.name)
        self.repo = os.path.join(base, "repo")
        self.home = os.path.join(base, "home")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        write(self.repo, "README.md", "base\n")
        write(self.repo, ".dev-skills/checks.json", json.dumps({"checks": self.checks, "ignore": IGNORE}))
        self.seed()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.root = store.home_root({"DEV_SKILLS_HOME": self.home})

    def seed(self):
        """Hook for tests that need more files in the base commit."""

    def contract(self, unit_id="u1", rounds=3, checks=True, **changes_):
        """A producer that writes and a reviewer that judges; `checks=False` leaves the check fact out."""
        def execution(eid, agent, model, effort, instructions, reads, establishes, writes):
            return {"id": eid, "agent": agent, "model": model, "effort": effort, "instructions": instructions,
                    "reads": list(reads), "establishes": list(establishes), "writes": writes,
                    "independent_of": [] if writes else ["producer"]}
        made = ["candidate", "checks"] if checks else ["candidate"]
        value = {
            "unit_id": unit_id, "repository": self.repo,
            "unit": {"goal": "make out.txt", "criteria": [{"id": "c1", "text": "out.txt is made"}],
                     "scope": ["out.txt"], "limits": {"rounds": rounds}},
            "facts": [
                {"id": "candidate", "required": True, "by": "report"},
                *([{"id": "checks", "required": True, "by": "checks", "subject": "candidate",
                    "checks": [{"name": "unit", "expect": "pass"}]}] if checks else []),
                {"id": "quality", "required": True, "by": "verdict", "subject": "candidate", "block_at": "major"},
            ],
            "executions": [
                execution("producer", "alpha", "m-1", None, "You produce.", [], made, True),
                execution("reviewer", "beta", None, "high", "You review.", made, ["quality"], False),
            ],
        }
        value.update(changes_)
        return value

    def with_agent(self, contract, **agents):
        """The contract with the named executions' agents replaced."""
        for execution in contract["executions"]:
            execution["agent"] = agents.get(execution["id"], execution["agent"])
        return contract

    def with_check(self, contract, name):
        next(fact for fact in contract["facts"] if fact["by"] == "checks")["checks"][0]["name"] = name
        return contract

    def engine(self, script, durable=None, **options):
        self.agents = ScriptedAgents(self.root, script, durable)
        engine = Engine(self.root, git=Git(os.environ), agents=self.agents,
                        templates=composition.load_templates(), runner=(sys.executable, RUNNER),
                        worktrees=os.path.join(self.home, "worktrees"), platform=CURRENT,
                        clock=lambda: "2026-01-01T00:00:00+00:00", **options)
        self.addCleanup(engine.halt)
        return engine

    def run_to_stop(self, engine, unit_id="u1", timeout=30):
        thread = engine._threads[unit_id]
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "unit did not stop")
        return store.UnitStore(self.root, unit_id).status()

    def start(self, script, **contract_changes):
        engine = self.engine(script)
        contract = self.contract(**contract_changes)
        response = engine.handle({"request": "start", "contract": contract})
        self.assertTrue(response["ok"], response)
        return engine, self.run_to_stop(engine, contract["unit_id"])

    def events(self, unit_id="u1"):
        return store.UnitStore(self.root, unit_id).events()

    def result(self, name, unit_id="u1"):
        with open(os.path.join(store.unit_dir(self.root, unit_id), "result", name), "rb") as handle:
            return handle.read()


def run_command(command, cwd):
    """Run a check command the way an agent's shell would, returning its output."""
    done = subprocess.run(shlex.split(command), cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return done.stdout
