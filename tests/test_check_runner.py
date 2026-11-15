"""The fixed check program: what it runs, where, and the one line the engine reads."""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

from engine_support import RUNNER, write

from loop_engine import facts
from loop_engine.check_runner import tree_digest

TEST = ("import os, sys, unittest\nsys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))\n"
        "from calc import add\n\nclass T(unittest.TestCase):\n    def test_it(self):\n        self.assertEqual({want}, add({args}))\n")
COMMITTED = ["calc.py", "tests"]
STATUS = re.compile(r"^loop-engine-check (\S+) tree=(candidate|base) digest=(\S+) exit=(-?\d+)$")


class CheckRunnerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = os.path.realpath(self._tmp.name)
        self.base, self.candidate, self.config = (os.path.join(root, name) for name in ("base", "wt", "checks.json"))
        write(self.base, "calc.py", "def add(a, b):\n    return a - b\n")
        write(self.base, "tests/test_calc.py", TEST.format(want=0, args="0, 0"))
        write(self.base, ".git", "gitdir: elsewhere\n")
        write(self.candidate, "calc.py", "def add(a, b):\n    return a + b\n")
        write(self.candidate, "tests/test_calc.py", TEST.format(want=3, args="1, 2"))
        with open(self.config, "w") as handle:
            json.dump({"cwd": self.candidate, "base": self.base, "ignore": ["__pycache__"], "committed": COMMITTED,
                       "checks": {
                "unit": {"argv": [sys.executable, "-m", "unittest", "discover", "-s", "tests"], "test_paths": ["tests"]},
                "missing": {"argv": ["/nonexistent/program"], "test_paths": []},
            }}, handle)

    def run_check(self, *arguments):
        done = subprocess.run([sys.executable, "-I", RUNNER, *arguments], cwd=self.candidate, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        lines = done.stdout.strip().splitlines()
        return done.returncode, STATUS.match(lines[-1]) if lines else None

    def record(self, *runs):
        runner = f"{sys.executable} -I {RUNNER} {self.config}"
        entries = []
        for tree in runs:
            command = facts.expected_command(runner, "unit", tree)
            done = subprocess.run(command.split(), cwd=self.candidate, text=True, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            entries.append({"seq": len(entries) + 1, "kind": "command", "command": command, "ended": True,
                            "output_tail": done.stdout[-2048:]})
        fact = {"checks": [{"name": "unit", "expect": "pass"}, {"name": "unit", "expect": "fails_on_base"}]}
        return facts.judge_checks(fact, {"entries": entries}, runner, tree_digest(self.candidate, ["__pycache__"], COMMITTED))

    def test_status_line_is_last_and_carries_the_tree_its_digest_and_the_exit_code(self):
        digest = tree_digest(self.candidate, ["__pycache__"], COMMITTED)
        code, status = self.run_check(self.config, "unit")
        self.assertEqual((0, ("unit", "candidate", digest, "0")), (code, status.groups()))
        code, status = self.run_check(self.config, "unit", "--base")
        self.assertEqual((0, ("unit", "base", digest, "1")), (code, status.groups()))  # the candidate's test fails there
        self.assertEqual(digest, tree_digest(self.candidate, ["__pycache__"], COMMITTED))  # caches the check wrote are ignored
        self.assertFalse(os.path.exists(os.path.join(self.base, "tests", "__pycache__")))  # the base copy is not run in place

    def test_a_fix_with_a_test_that_fails_on_base_is_accepted_and_one_without_is_not(self):
        self.assertEqual({}, self.record("candidate", "base"))
        write(self.candidate, "tests/test_calc.py", TEST.format(want=0, args="0, 0"))
        self.assertEqual({"unit:base": "passes on the base revision: no test fails without the change"},
                         self.record("candidate", "base"))

    def test_a_check_that_ran_before_a_later_change_does_not_count(self):
        runner = f"{sys.executable} -I {RUNNER} {self.config}"
        command = facts.expected_command(runner, "unit", "candidate")
        done = subprocess.run(command.split(), cwd=self.candidate, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        write(self.candidate, "calc.py", "def add(a, b):\n    return a + b  # changed through the shell\n")
        fact = {"checks": [{"name": "unit", "expect": "pass"}]}
        entries = [{"seq": 1, "kind": "command", "command": command, "ended": True, "output_tail": done.stdout}]
        self.assertEqual({"unit:candidate": "not run on the present tree"}, facts.judge_checks(
            fact, {"entries": entries}, runner, tree_digest(self.candidate, ["__pycache__"], COMMITTED)))

    def test_unknown_check_missing_program_and_extra_arguments_do_not_pass(self):
        code, status = self.run_check(self.config, "nope")
        self.assertEqual((127, "127"), (code, status.group(4)))
        code, status = self.run_check(self.config, "missing")
        self.assertEqual((1, "127"), (code, status.group(4)))
        self.assertEqual((2, None), self.run_check(self.config, "unit", "--base", "extra"))
        self.assertEqual((2, None), self.run_check(self.config, "unit", "; touch x"))
        self.assertEqual(127, self.run_check(os.path.join(self.candidate, "absent.json"), "unit")[0])


if __name__ == "__main__":
    unittest.main()
