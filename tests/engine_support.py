"""Shared fixtures: a real git repository, a scripted agent port and an engine over them."""
import hashlib
import json
import os
import shlex
import shutil
import stat
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
from loop_engine.check_runner import is_link, tree_digest  # noqa: E402
from loop_engine.git import Git  # noqa: E402
from loop_engine.platform_facts import CURRENT  # noqa: E402
from loop_engine.service import Engine  # noqa: E402

RUNNER = os.path.join(SRC, "loop_engine", "check_runner.py")


def published_runner(published):
    """Where an engine publishing into `published` places the check program agents run."""
    with open(RUNNER, "rb") as handle:
        return store.check_program_path(published, hashlib.sha256(handle.read()).hexdigest())


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
    """The files of the tree under cwd, as the engine counts them: a link to a directory is one of them."""
    found = []
    for folder, directories, files in os.walk(cwd):
        links = [name for name in directories if is_link(os.path.join(folder, name))]
        directories[:] = [name for name in directories if name != ".git" and name not in links]
        found += [os.path.relpath(os.path.join(folder, name), cwd) for name in [*files, *links] if name != ".git"]
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

    def __init__(self, root, published, script, durable=None):
        self.root, self.published, self.script, self.requests, self.closed = root, published, script, [], False
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
        unit = store.UnitStore(self.root, self.published, request.unit_id)
        runner = CURRENT.quote([sys.executable, "-I", published_runner(self.published), unit.published("checks.json")])
        CHECKS_FILES[request.cwd] = unit.published("checks.json")
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
    """The file `name` holding exactly `text`, with the same bytes on every platform."""
    path = os.path.join(cwd, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def file_link(target, path):
    """A symbolic link at `path` to the file `target`, or False where this process may not make one (Windows
    without the privilege). No agent this process starts can make one there either."""
    try:
        os.symlink(target, path)
    except OSError as error:
        if getattr(error, "winerror", None) == 1314:  # ERROR_PRIVILEGE_NOT_HELD
            return False
        raise
    return True


def unreadable(path, mode):
    """Deny this process what `mode` takes away from `path`, the way the platform allows, and return how to
    undo it; None where the platform cannot deny it, so no agent can make the case arise there.

    `mode` without the read bit: a directory cannot be listed, a file cannot be read; a directory without
    the search bit: its entries cannot be reached. POSIX denies by the mode. Windows ignores those bits and
    denies by an access control entry for this user, and lets every user traverse a directory."""
    restored = 0o755 if os.path.isdir(path) else 0o644
    os.chmod(path, mode)
    if _denied(path, mode):
        return lambda: os.chmod(path, restored)
    os.chmod(path, restored)
    if os.path.isdir(path) and mode & stat.S_IRUSR:
        return None  # only the search bit is taken away, and Windows bypasses traverse checking
    user = os.environ.get("USERNAME")
    right = "RD" if os.path.isdir(path) else "R"  # list a directory, read a file
    if not user or subprocess.run(["icacls", path, "/deny", f"{user}:({right})"], capture_output=True).returncode:
        return None

    def undo():
        subprocess.run(["icacls", path, "/remove:d", user], capture_output=True)
    if _denied(path, mode):
        return undo
    undo()
    return None


def _denied(path, mode):
    try:
        if not os.path.isdir(path):
            open(path, "rb").close()
        elif not mode & stat.S_IRUSR:
            os.listdir(path)
        else:
            for name in os.listdir(path):
                os.stat(os.path.join(path, name))
    except PermissionError:
        return True
    return False


def _file_links_can_be_made():
    with tempfile.TemporaryDirectory() as directory:
        target = os.path.join(directory, "target")
        open(target, "w").close()
        return file_link(target, os.path.join(directory, "link"))


FILE_LINKS = _file_links_can_be_made()


def needs_file_links(test_class):
    """Keep a test class whose case is a symbolic link to a file where this process can make one. Where it
    cannot (Windows without the privilege), no agent it starts can make one either, so the class is not a
    test there: its name is bound to None, which test discovery passes over."""
    return test_class if FILE_LINKS else None


def remove_tree(path):
    """Remove a tree with read-only files in it, which Windows refuses to delete until they are writable."""
    def writable(function, name, _error):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)
    shutil.rmtree(path, onexc=writable)


def directory_link(target, path):
    """A link at `path` to the directory `target`, as an agent can make one: a symbolic link, or where the
    process may not make one, a junction (Windows lets any user make those). `target` may be relative
    to the link's directory."""
    try:
        os.symlink(target, path, target_is_directory=True)
    except OSError:
        import _winapi  # only reached where a symbolic link needs a privilege: Windows
        _winapi.CreateJunction(os.path.normpath(os.path.join(os.path.dirname(path), target)), path)


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
        write(self.repo, ".loop-engine/checks.json", json.dumps({"checks": self.checks, "ignore": IGNORE}))
        self.seed()
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "base")
        self.root = store.home_root({"LOOP_ENGINE_HOME": self.home})
        self.published = store.published_root({"LOOP_ENGINE_HOME": self.home})

    def seed(self):
        """Hook for tests that need more files in the base commit."""

    def contract(self, unit_id="u1", checks=True, **changes_):
        """A producer that writes and a reviewer that judges; `checks=False` leaves the check fact out."""
        def execution(eid, agent, model, effort, instructions, reads, establishes, writes):
            return {"id": eid, "agent": agent, "model": model, "effort": effort, "instructions": instructions,
                    "reads": list(reads), "establishes": list(establishes), "writes": writes,
                    "independent_of": [] if writes else ["producer"]}
        made = ["candidate", "checks"] if checks else ["candidate"]
        value = {
            "unit_id": unit_id, "repository": self.repo,
            "unit": {"goal": "make out.txt", "criteria": [{"id": "c1", "text": "out.txt is made"}],
                     "scope": ["out.txt"]},
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
        self.agents = ScriptedAgents(self.root, self.published, script, durable)
        engine = Engine(self.root, published=self.published, git=Git(os.environ), agents=self.agents,
                        templates=composition.load_templates(), runner=(sys.executable, RUNNER),
                        worktrees=os.path.join(self.home, "worktrees"), platform=CURRENT,
                        clock=lambda: "2026-01-01T00:00:00+00:00", **options)
        self.addCleanup(engine.halt)
        return engine

    def run_to_stop(self, engine, unit_id="u1", timeout=30):
        thread = engine._threads[unit_id]
        thread.join(timeout)
        self.assertFalse(thread.is_alive(), "unit did not stop")
        return self.unit(unit_id).status()

    def start(self, script, **contract_changes):
        engine = self.engine(script)
        contract = self.contract(**contract_changes)
        response = engine.handle({"request": "start", "contract": contract})
        self.assertTrue(response["ok"], response)
        return engine, self.run_to_stop(engine, contract["unit_id"])

    def unit(self, unit_id="u1"):
        return store.UnitStore(self.root, self.published, unit_id)

    def events(self, unit_id="u1"):
        return self.unit(unit_id).events()

    def result(self, name, unit_id="u1"):
        """A result file of the unit's latest stop, read as a caller reads it, or a turn's record."""
        if name in ("stop.json", "patch.diff"):
            return store.stop_result(self.root, unit_id, name)
        with open(os.path.join(store.unit_dir(self.root, unit_id), "result", name), "rb") as handle:
            return handle.read()

    def stop_details(self, unit_id="u1"):
        """Why the unit stopped, as stop.json's stop and the status each name it."""
        return (json.loads(self.result("stop.json", unit_id))["stop"].get("detail"),
                self.unit(unit_id).status().get("detail"))


def run_command(command, cwd):
    """Run a check command the way an agent's shell would, returning its output.

    The line was made by the platform's `quote`, and is undone by its counterpart: a POSIX shell splits
    what `shlex.join` wrote; on Windows the line reaches the program as it is, and the program splits it
    by the C runtime's rules that `windows_command_line` writes for."""
    argv = shlex.split(command) if CURRENT.quote is shlex.join else command
    done = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    return done.stdout
