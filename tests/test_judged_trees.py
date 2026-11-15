"""What the engine judges a worktree against, and that what it cannot read makes a judgment fail."""
import json
import os
import shlex
import shutil
import stat
import sys
import tempfile
import unittest

from engine_support import (
    EngineCase, RUNNER, actions, approve, produced, published_runner, recorded, run_command, write,
)

from loop_engine import facts, store
from loop_engine.agents import TurnOutcome
from loop_engine.worktree import Unreadable, agent_config_digest, changed_paths, tree_digest

TEST = ("import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual({want}, add({args}))\n")


def unit_checks(root, unit_id):
    with open(store.UnitStore(root, unit_id).path("checks.json"), encoding="utf-8") as handle:
        return json.load(handle)


def restore_modes(tree):
    for folder, directories, _files in os.walk(tree):
        for name in directories:
            os.chmod(os.path.join(folder, name), 0o755)


class BaseRecordTest(EngineCase):
    """The base revision is git's record, or a copy only the engine writes; never a tree an agent writes."""

    def with_scope_fact(self, contract):
        contract["facts"].append({"id": "in-scope", "required": True, "by": "git", "subject": "candidate"})
        contract["executions"][1]["reads"].append("in-scope")
        return contract

    def tampering(self, request, commands, cancelled):
        """A producer that writes outside the scope and the same file into the base copy it was told about."""
        if request.role == "reviewer":
            return approve(request)
        base = unit_checks(self.root, request.unit_id)["base"]
        for tree in (request.cwd, base):
            write(tree, "other.txt", "outside the scope\n")
        return produced(request, commands)

    def test_writing_the_base_copy_does_not_hide_a_change_from_the_scope(self):
        contract = self.with_scope_fact(self.contract(rounds=1))
        _engine, status = self.start(self.tampering, unit=contract["unit"], facts=contract["facts"],
                                     executions=contract["executions"])
        self.assertEqual("rounds_exhausted", status["stop_reason"])
        stop = json.loads(self.result("stop.json"))
        self.assertEqual(["other.txt"], [finding["path"] for finding in stop["stop"]["findings"]])
        self.assertEqual(["producer"], [request.role for request in self.agents.requests])

    def test_the_reviewer_is_told_of_a_change_the_base_copy_was_made_to_hide(self):
        _engine, status = self.start(self.tampering)
        self.assertEqual("done", status["stop_reason"])
        reviewer = next(request for request in self.agents.requests if request.role == "reviewer")
        self.assertIn("- other.txt", reviewer.prompt)

    def test_the_base_copy_is_in_the_units_record_not_beside_the_worktree(self):
        _engine, status = self.start(self.tampering)
        base = unit_checks(self.root, "u1")["base"]
        self.assertEqual(store.unit_dir(self.root, "u1"), os.path.dirname(base))
        self.assertFalse(base.startswith(os.path.join(self.home, "worktrees")))
        self.assertFalse(os.path.exists(base))  # removed with the worktree once the unit ended
        self.assertFalse(os.path.exists(status["worktree"] + ".base"))


class FailsOnBaseTest(EngineCase):
    checks = {"unit": {"argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "test_paths": ["tests"]}}

    def seed(self):
        write(self.repo, "calc.py", "def add(a, b):\n    return a - b\n")
        write(self.repo, "tests/test_calc.py", TEST.format(want=0, args="0, 0"))

    def fixing(self, contract):
        contract["unit"]["scope"] = ["calc.py", "tests"]
        next(fact for fact in contract["facts"] if fact["by"] == "checks")["checks"].append(
            {"name": "unit", "expect": "fails_on_base"})
        return contract

    def ran_both(self, request):
        runner = shlex.join([sys.executable, "-I", published_runner(self.root),
                             store.UnitStore(self.root, request.unit_id).path("checks.json")])
        return actions(("edit", None, None), *[
            ("command", command, run_command(command, request.cwd))
            for command in (facts.expected_command(runner, "unit", "candidate"),
                            facts.expected_command(runner, "unit", "base"))])

    def test_a_test_that_fails_without_the_change_holds_the_fact(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            write(request.cwd, "calc.py", "def add(a, b):\n    return a + b\n")
            write(request.cwd, "tests/test_calc.py", TEST.format(want=3, args="1, 2"))
            return TurnOutcome(True, "fixed", self.ran_both(request))
        _engine, status = self.start(script, **self.fixing(self.contract()))
        self.assertEqual("done", status["stop_reason"])

    def test_a_base_copy_made_to_fail_does_not_hold_the_fact(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            write(request.cwd, "calc.py", "def add(a, b):\n    return a - b  # unchanged behaviour\n")
            write(unit_checks(self.root, request.unit_id)["base"], "calc.py", "raise SystemExit(3)\n")
            return TurnOutcome(True, "fixed", self.ran_both(request))
        _engine, status = self.start(script, **self.fixing(self.contract(rounds=1)))
        self.assertEqual("rounds_exhausted", status["stop_reason"])
        stop = json.loads(self.result("stop.json"))
        self.assertEqual([("unit:base", "not run on the base revision the engine placed")],
                         [(finding["check"], finding["description"]) for finding in stop["stop"]["findings"]])


class UnreadableTreeTest(unittest.TestCase):
    """The engine's and the check program's reading of a tree: what cannot be read is not left out."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        self.base, self.tree = os.path.join(root, "base"), os.path.join(root, "tree")
        for folder in (self.base, self.tree):
            write(folder, "kept.txt", "kept\n")
            write(folder, "pkg/mod.py", "x = 1\n")
        self.recorded = recorded(self.base)
        self.addCleanup(restore_modes, self.tree)
        self.config = os.path.join(root, "checks.json")
        with open(self.config, "w") as handle:
            json.dump({"cwd": self.tree, "base": self.base, "ignore": [], "committed": ["kept.txt", "pkg"],
                       "checks": {"ok": {"argv": ["true"], "test_paths": ["pkg"]}}}, handle)

    def test_a_directory_or_file_the_process_cannot_read_raises_instead_of_shrinking_the_tree(self):
        write(self.tree, "secret/x.txt", "out of sight\n")
        cases = {"a directory that cannot be listed": (os.path.join(self.tree, "secret"), 0o300),
                 "a directory whose entries cannot be reached": (os.path.join(self.tree, "secret"), 0o600),
                 "a file that cannot be read": (os.path.join(self.tree, "secret", "x.txt"), 0o000)}
        for case, (path, mode) in cases.items():
            with self.subTest(case):
                os.chmod(path, mode)
                self.addCleanup(os.chmod, path, 0o755)
                with self.assertRaises(Unreadable):
                    tree_digest(self.tree, [], ["kept.txt", "pkg"])
                with self.assertRaises(Unreadable):
                    changed_paths(self.recorded, self.tree, [], ["kept.txt", "pkg"])
                line = run_command(f"{sys.executable} -I {RUNNER} {self.config} ok", self.tree).strip().splitlines()[-1]
                self.assertEqual("loop-engine-check ok tree=candidate digest=none exit=127", line)
                os.chmod(path, 0o755 if stat.S_ISDIR(os.stat(path).st_mode) else 0o644)

    def test_a_base_revision_the_program_cannot_copy_shows_no_failure_on_it(self):
        os.chmod(os.path.join(self.base, "kept.txt"), 0o000)
        self.addCleanup(os.chmod, os.path.join(self.base, "kept.txt"), 0o644)
        line = run_command(f"{sys.executable} -I {RUNNER} {self.config} ok --base", self.tree).strip().splitlines()[-1]
        self.assertEqual("loop-engine-check ok tree=base digest=none exit=127", line)


class UnreadableFlowTest(EngineCase):
    def hiding(self, role, directory, path="x.txt"):
        def script(request, commands, cancelled):
            if request.role == role:
                write(request.cwd, os.path.join(directory, path), "written where the engine cannot look\n")
            outcome = produced(request, commands) if request.role == "producer" else approve(request)
            if request.role == role:  # after the turn's own record was made
                hidden = os.path.join(request.cwd, directory)
                os.chmod(hidden, 0o300)
                self.addCleanup(lambda: os.path.isdir(hidden) and os.chmod(hidden, 0o755))
            return outcome
        return script

    def finished(self):
        return [(event["execution"], event["detail"]) for event in self.events() if event["type"] == "turn_finished"]

    def test_a_writer_that_makes_a_directory_unreadable_does_not_complete_its_turn(self):
        _engine, status = self.start(self.hiding("producer", "secret"))
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual([("producer", "worktree-not-readable: secret")], self.finished())
        self.assertEqual("worktree-not-readable: secret", json.loads(self.result("stop.json"))["stop"]["detail"])

    def test_a_reviewer_that_makes_a_directory_unreadable_is_not_taken_as_not_having_written(self):
        _engine, status = self.start(self.hiding("reviewer", "secret"))
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual(("reviewer", "worktree-not-readable: secret"), self.finished()[-1])

    def test_agent_configuration_made_unreadable_stops_the_unit(self):
        _engine, status = self.start(self.hiding("producer", ".claude", "settings.json"))
        self.assertEqual("unsafe_agent_config", status["stop_reason"])
        self.assertEqual(["producer"], [request.role for request in self.agents.requests])


class AgentConfigFileTest(EngineCase):
    """An agent configuration name is watched whatever kind of path it is."""

    def test_a_configuration_name_that_is_a_file_with_content_stops_the_unit(self):
        for index, name in enumerate((".pi", ".claude", ".codex", ".agents")):
            def script(request, commands, cancelled, name=name):
                if request.role == "reviewer":
                    return approve(request)
                write(request.cwd, name, '{"extensions": ["./run-me.ts"]}\n')
                return produced(request, commands)
            with self.subTest(name):
                _engine, status = self.start(script, unit_id=f"u{index}")
                self.assertEqual("unsafe_agent_config", status["stop_reason"])
                self.assertEqual(["producer"], [r.role for r in self.agents.requests])

    def test_the_digest_follows_every_file_type_and_leaves_out_only_the_environment_entries(self):
        with tempfile.TemporaryDirectory() as work:
            root = os.path.realpath(work)
            clean = agent_config_digest(root, [])
            pi = os.path.join(root, ".pi")
            open(pi, "x").close()
            os.chmod(pi, 0o444)
            self.assertEqual(clean, agent_config_digest(root, []))  # an environment entry
            self.assertNotEqual(clean, agent_config_digest(root, [".pi"]))  # a committed name is no entry
            os.chmod(pi, 0o644)
            self.assertNotEqual(clean, agent_config_digest(root, []))  # writable: not an entry
            with open(pi, "w") as handle:
                handle.write("x\n")
            content = agent_config_digest(root, [])
            self.assertNotIn(content, (clean, None))
            os.unlink(pi)
            os.mkfifo(pi)
            self.assertNotIn(agent_config_digest(root, []), (clean, content, None))
            os.unlink(pi)
            os.mkdir(pi)
            self.assertEqual(clean, agent_config_digest(root, []))  # an empty directory defines nothing
            shutil.rmtree(pi)


if __name__ == "__main__":
    unittest.main()
