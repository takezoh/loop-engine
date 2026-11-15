"""Entries the environment places in the working directory: empty, read-only files no commit has."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from engine_support import EngineCase, RUNNER, actions, approve, git, git_out, produced, recorded, run_command, write

from loop_engine import check_runner, store
from loop_engine.agents import TurnOutcome
from loop_engine.git import Git
from loop_engine.worktree import agent_config_digest, changed_paths, environment_entries, tree_digest

ENVIRONMENT_DIRECTORIES = (".agents", ".aws", ".codex")
ENVIRONMENT_FILES = (".pi", "AGENTS.md", "AGENTS.override.md")
MUST_NOT_WRITE = "a-turn-that-must-not-write-changed-the-worktree"


def place_environment(cwd):
    """What an agent's host leaves in its working directory when it starts a session there."""
    for name in ENVIRONMENT_DIRECTORIES:
        os.makedirs(os.path.join(cwd, name), exist_ok=True)
    for name in ENVIRONMENT_FILES:
        path = os.path.join(cwd, name)
        if not os.path.lexists(path):
            open(path, "x").close()
            os.chmod(path, 0o444)


def read_only_empty(cwd, name):
    path = os.path.join(cwd, name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w"):
        pass
    os.chmod(path, 0o444)


def patched_paths(patch):
    return sorted(line.split(" b/", 1)[1] for line in patch.decode().splitlines() if line.startswith("diff --git "))


class TreeTest(unittest.TestCase):
    """The predicate's inputs, and the digests the engine and the check program compute from them."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        self.base, self.tree = os.path.join(root, "wt.base"), os.path.join(root, "wt")
        self.outside = os.path.join(root, "outside")
        for folder in (self.base, self.tree):
            write(folder, "calc.py", "def add(a, b):\n    return a + b\n")
            write(folder, "pkg/mod.py", "x = 1\n")
        self.committed = ["calc.py", "pkg"]
        self.recorded = recorded(self.base)
        self.config = os.path.join(root, "checks.json")
        with open(self.config, "w") as handle:
            json.dump({"cwd": self.tree, "base": self.base, "ignore": [], "committed": self.committed,
                       "checks": {"ok": {"argv": ["true"], "test_paths": []}}}, handle)
        self.clean = tree_digest(self.tree, [], self.committed)

    def observed(self):
        return tree_digest(self.tree, [], self.committed), changed_paths(self.recorded, self.tree, [], self.committed)

    def program_digest(self):
        line = run_command(f"{sys.executable} -I {RUNNER} {self.config} ok", self.tree).strip().splitlines()[-1]
        return line.split(" digest=")[1].split(" ")[0]

    def test_empty_read_only_files_and_empty_directories_change_no_digest_on_either_side(self):
        config = agent_config_digest(self.tree, self.committed)
        place_environment(self.tree)
        self.assertEqual((self.clean, []), self.observed())
        self.assertEqual(self.clean, self.program_digest())
        self.assertEqual(sorted(ENVIRONMENT_FILES), environment_entries(self.tree, self.committed))
        self.assertEqual(config, agent_config_digest(self.tree, self.committed))  # an empty .pi, empty .codex and .agents

    def test_the_mode_an_entry_is_shown_with_does_not_matter_while_it_is_not_writable(self):
        place_environment(self.tree)
        pi = os.path.join(self.tree, ".pi")
        os.chmod(pi, 0o000)  # a view in which the file cannot even be read
        self.assertEqual(self.clean, tree_digest(self.tree, [], self.committed))
        self.assertEqual(self.clean, self.program_digest())
        os.chmod(pi, 0o600)  # as a sandbox shows it, on a mount that refuses writing
        real = os.access
        with mock.patch.object(check_runner.os, "access",
                               lambda path, mode: False if path == pi else real(path, mode)):
            self.assertEqual(self.clean, tree_digest(self.tree, [], self.committed))
        self.assertNotEqual(self.clean, tree_digest(self.tree, [], self.committed))  # writable: counted

    def test_everything_else_is_counted(self):
        cases = {
            "a writable empty file": lambda: write(self.tree, "empty.txt", ""),
            "a non-empty read-only file": lambda: (write(self.tree, "AGENTS.md", "x\n"),
                                                   os.chmod(os.path.join(self.tree, "AGENTS.md"), 0o444)),
            "a committed file truncated and made read-only": lambda: read_only_empty(self.tree, "calc.py"),
            "an empty read-only file below the top directory": lambda: read_only_empty(self.tree, "pkg/__init__.py"),
            "a link to an empty read-only file": lambda: (read_only_empty(self.outside, "target"),
                                                          os.symlink(os.path.join(self.outside, "target"),
                                                                     os.path.join(self.tree, ".pi"))),
        }
        for case, make in cases.items():
            with self.subTest(case):
                make()
                digest, changed = self.observed()
                self.assertNotEqual(self.clean, digest)
                self.assertNotEqual(self.clean, self.program_digest())
                self.assertTrue(changed)
                self.assertEqual([], environment_entries(self.tree, self.committed))
                shutil.rmtree(self.tree)
                shutil.copytree(self.base, self.tree, ignore=shutil.ignore_patterns(".git"))

    def test_an_entry_that_later_holds_content_or_becomes_writable_is_counted(self):
        place_environment(self.tree)
        path = os.path.join(self.tree, ".pi")
        os.chmod(path, 0o644)
        self.assertEqual([".pi"], self.observed()[1])
        with open(path, "w") as handle:
            handle.write("now it says something\n")
        os.chmod(path, 0o444)
        self.assertEqual([".pi"], self.observed()[1])
        self.assertNotEqual(self.clean, self.program_digest())


class PatchTest(EngineCase):
    """What git records: the entries are left out of the index, whatever was staged before."""

    def test_the_patch_leaves_out_only_the_entries_and_follows_their_present_state(self):
        engine_git, head = Git(os.environ), git_out(self.repo, "rev-parse", "HEAD")
        committed = engine_git.top_names(self.repo, head)
        self.assertEqual([".dev-skills", "README.md"], committed)

        def patch():
            entries = environment_entries(self.repo, committed)
            return patched_paths(engine_git.patch(self.repo, head, os.path.join(self.repo, ".git"), entries))
        place_environment(self.repo)
        write(self.repo, "empty.txt", "")
        self.assertEqual(["empty.txt"], patch())
        os.chmod(os.path.join(self.repo, "empty.txt"), 0o444)  # staged above, an entry now
        self.assertEqual([], patch())
        os.chmod(os.path.join(self.repo, ".pi"), 0o644)
        self.assertEqual([".pi"], patch())
        read_only_empty(self.repo, "README.md")
        self.assertEqual([".pi", "README.md"], patch())


class FlowTest(EngineCase):
    def contract_in_scope(self, **changes):
        contract = self.contract(**changes)
        contract["facts"].append({"id": "in-scope", "required": True, "by": "git", "subject": "candidate"})
        contract["executions"][1]["reads"].append("in-scope")
        return contract

    def finished(self, execution, unit_id="u1"):
        return [event["detail"] for event in self.events(unit_id)
                if event["type"] == "turn_finished" and event["execution"] == execution]

    def test_the_environment_placing_entries_in_every_session_does_not_keep_a_unit_from_done(self):
        seen, outputs = [], []

        def script(request, commands, cancelled):
            place_environment(request.cwd)  # as the agent's host does when the session starts
            if request.role == "reviewer":
                seen.append(sorted(os.listdir(request.cwd)))
                return approve(request)
            write(request.cwd, "out.txt", "made by the producer\n")
            outputs.append(run_command(commands["unit"], request.cwd))
            return TurnOutcome(True, "done", actions(("edit", None, None), ("command", commands["unit"], outputs[-1])))
        contract = self.contract_in_scope()
        _engine, status = self.start(script, facts=contract["facts"], executions=contract["executions"])
        self.assertEqual("done", status["stop_reason"])
        self.assertTrue(set(ENVIRONMENT_DIRECTORIES + ENVIRONMENT_FILES) <= set(seen[0]))
        self.assertTrue(outputs[0].rstrip().endswith("exit=0"))
        self.assertEqual(["out.txt"], patched_paths(self.result("patch.diff")))
        stop = json.loads(self.result("stop.json"))
        self.assertTrue(all(fact["holds"] for fact in stop["facts"].values()), stop["facts"])
        self.assertEqual([None], self.finished("reviewer"))

    def test_a_reviewer_placing_an_empty_read_only_file_has_not_written_and_a_writable_one_has(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands)
            if request.unit_id == "u1":
                read_only_empty(request.cwd, "AGENTS.md")
            else:
                write(request.cwd, "AGENTS.md", "")
            return approve(request)
        _engine, status = self.start(script)
        self.assertEqual(("done", [None]), (status["stop_reason"], self.finished("reviewer")))
        _engine, status = self.start(script, unit_id="u2")
        self.assertNotEqual("done", status["stop_reason"])
        self.assertEqual(MUST_NOT_WRITE, self.finished("reviewer", "u2")[0])
        self.assertEqual((MUST_NOT_WRITE,) * 2, self.stop_details("u2"))
        self.assertIn(b"b/AGENTS.md", self.result("patch.diff", "u2"))

    def test_a_writer_keeps_its_own_empty_file_in_the_patch(self):
        def script(request, commands, cancelled):
            place_environment(request.cwd)
            if request.role == "reviewer":
                return approve(request)
            write(request.cwd, "pkg/__init__.py", "")
            return produced(request, commands)
        _engine, status = self.start(script, unit={"goal": "make out.txt", "scope": ["out.txt", "pkg"],
                                                    "criteria": [{"id": "c1", "text": "out.txt is made"}],
                                                    "limits": {"rounds": 3}})
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual(["out.txt", "pkg/__init__.py"], patched_paths(self.result("patch.diff")))

    def test_truncating_a_committed_file_and_making_it_read_only_is_a_change(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands)
            read_only_empty(request.cwd, "README.md")
            return approve(request)
        _engine, status = self.start(script)
        self.assertEqual(MUST_NOT_WRITE, self.finished("reviewer")[0])
        self.assertIn(b"-base", self.result("patch.diff"))

    def test_an_entry_that_comes_to_hold_content_or_be_writable_is_a_change(self):
        def script(request, commands, cancelled):
            place_environment(request.cwd)
            if request.role == "producer":
                return produced(request, commands)
            path = os.path.join(request.cwd, "AGENTS.md")
            os.chmod(path, 0o644)
            if request.unit_id == "u1":
                with open(path, "w") as handle:
                    handle.write("written by the reviewer\n")
                os.chmod(path, 0o444)
            return approve(request)
        for unit_id in ("u1", "u2"):
            with self.subTest(unit_id):
                self.start(script, unit_id=unit_id)
                self.assertEqual(MUST_NOT_WRITE, self.finished("reviewer", unit_id)[0])
                self.assertIn("AGENTS.md", patched_paths(self.result("patch.diff", unit_id)))

    def listed(self, unit_id="u1"):
        return json.loads(self.result("stop.json", unit_id))["environment_entries"]

    def turn_lists(self, unit_id="u1"):
        return [(event["execution"], event["environment_entries"]) for event in self.events(unit_id)
                if event["type"] == "turn_finished"]

    def test_the_entries_left_out_are_named_in_the_record_of_each_turn_and_of_the_stop(self):
        def script(request, commands, cancelled):
            if request.unit_id == "u1":
                place_environment(request.cwd)
            return produced(request, commands) if request.role == "producer" else approve(request)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        entries = {"count": 3, "names": sorted(ENVIRONMENT_FILES)}
        self.assertEqual(entries, self.listed())
        self.assertEqual([("producer", entries), ("reviewer", entries)], self.turn_lists())
        _engine, status = self.start(script, unit_id="u2")
        self.assertEqual({"count": 0, "names": []}, self.listed("u2"))
        self.assertEqual([("producer", {"count": 0, "names": []}), ("reviewer", {"count": 0, "names": []})],
                         self.turn_lists("u2"))

    def test_an_empty_read_only_file_a_role_places_at_the_top_is_named_although_the_patch_leaves_it_out(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            read_only_empty(request.cwd, "conftest.py")  # changes what a test run collects, holds nothing
            return produced(request, commands)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual(["out.txt"], patched_paths(self.result("patch.diff")))
        self.assertEqual({"count": 1, "names": ["conftest.py"]}, self.listed())
        self.assertEqual(("producer", {"count": 1, "names": ["conftest.py"]}), self.turn_lists()[0])

    def test_the_names_are_read_from_the_worktree_at_the_stop_also_after_the_engine_restarts(self):
        def script(request, commands, cancelled):
            if request.role == "producer" and self.agents.nth["producer"] == 1 and request.session is None:
                place_environment(request.cwd)
                return TurnOutcome(True, '{"question": "which?", "id": "q-1"}', None)
            return produced(request, commands) if request.role == "producer" else approve(request)
        engine, status = self.start(script)
        self.assertEqual("waiting_for_answer", status["stop_reason"])
        self.assertEqual({"count": 3, "names": sorted(ENVIRONMENT_FILES)}, self.listed())
        engine.halt()
        read_only_empty(status["worktree"], "CLAUDE.md")  # placed while no engine ran
        later = self.engine(script, durable=self.agents.durable)
        self.assertTrue(later.handle({"request": "answer", "unit_id": "u1", "question_id": "q-1",
                                      "answer": "the first"})["ok"])
        self.assertEqual("done", self.run_to_stop(later)["stop_reason"])
        self.assertEqual({"count": 4, "names": sorted([*ENVIRONMENT_FILES, "CLAUDE.md"])}, self.listed())

    def test_the_record_names_at_most_its_cap_of_entries_and_counts_them_all(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            for index in range(40):
                read_only_empty(request.cwd, f"entry-{index:02d}")
            return produced(request, commands)
        self.start(script)
        listed = self.listed()
        self.assertEqual((40, [f"entry-{index:02d}" for index in range(32)]), (listed["count"], listed["names"]))

    def test_a_name_the_worktree_commit_has_is_not_an_entry_when_the_base_lacks_it(self):
        base = git_out(self.repo, "rev-parse", "HEAD")
        write(self.repo, "out.txt", "made before the unit started\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "the change under review")
        contract = self.contract(facts=[
            {"id": "subject", "required": False, "by": "given"},
            {"id": "quality", "required": True, "by": "verdict", "subject": "subject", "block_at": "major"},
        ], executions=[{"id": "first", "agent": "alpha", "model": None, "effort": None, "instructions": "Review.",
                        "reads": [], "establishes": ["quality"], "writes": False, "independent_of": []}])
        contract["unit"]["base"] = base

        def script(request, commands, cancelled):
            reply = approve(request)
            read_only_empty(request.cwd, "out.txt")
            return reply
        engine = self.engine(script)
        self.assertTrue(engine.handle({"request": "start", "contract": contract})["ok"])
        self.run_to_stop(engine)
        self.assertEqual(MUST_NOT_WRITE, self.finished("first")[0])
        with open(os.path.join(store.unit_dir(self.root, "u1"), "checks.json"), encoding="utf-8") as handle:
            self.assertEqual([".dev-skills", "README.md", "out.txt"], json.load(handle)["committed"])


if __name__ == "__main__":
    unittest.main()
