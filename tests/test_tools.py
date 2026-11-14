"""The tools kept for improving the engine: the unit report and the scenario runner's deterministic parts."""
import json
import os
import sys
import tempfile
import unittest

from engine_support import SRC, EngineCase, approve, changes, produced

TOOLS = os.path.join(os.path.dirname(SRC), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import scenarios  # noqa: E402
import unit_report  # noqa: E402

from loop_engine import facts  # noqa: E402


class UnitReportTest(EngineCase):
    def test_report_shows_how_each_unit_stopped_and_what_its_turns_did(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands, text=f"attempt number {self.agents.nth['producer']}\n")
            if request.unit_id == "u1":
                return changes(request, "still wrong") if self.agents.nth["reviewer"] == 1 else approve(request)
            return changes(request, f"problem {self.agents.nth['reviewer']}")
        engine, _status = self.start(script)
        engine.handle({"request": "start", "contract": self.contract(unit_id="u2", rounds=1)})
        self.run_to_stop(engine, "u2")
        units = [unit_report.load_unit(self.root, name) for name in ("u1", "u2")]
        self.assertIsNone(unit_report.load_unit(self.root, "absent"))
        done, exhausted = units
        self.assertEqual(("done", 1, {"producer": 2, "reviewer": 2}),
                         (done["stop_reason"], done["rounds"], done["turns_by_execution"]))
        self.assertEqual({"candidate": True, "checks": True, "quality": True}, done["facts"])
        self.assertEqual({"edit": 1, "command": 1}, done["turns"][0]["actions"])
        self.assertEqual({"agent": "beta", "model": None, "effort": "high"}, done["agents"]["reviewer"])
        self.assertEqual("rounds_exhausted", exhausted["stop_reason"])
        self.assertEqual(["c1"], [finding["criterion"] for finding in exhausted["findings"]])
        summary = unit_report.summarize(units)
        self.assertEqual(({"done": 1, "rounds_exhausted": 1}, 6, 2),
                         (summary["stops"], summary["turns_total"], summary["turns_repeated"]))
        self.assertEqual(0, summary["results_not_counted"])
        self.assertEqual(["new", "new", "continued", "continued"], [turn["session"] for turn in done["turns"]])
        self.assertEqual(2, summary["turns_continued"])  # u1's second producer and reviewer turns

        def vague(request, commands, cancelled):
            from loop_engine.agents import TurnOutcome
            if request.role == "producer":
                return produced(request, commands)
            return TurnOutcome(True, "LGTM", {"version": 1, "dropped": 0, "entries": []})
        engine = self.engine(vague)
        engine.handle({"request": "start", "contract": self.contract(unit_id="u3")})
        self.run_to_stop(engine, "u3")
        unclear = unit_report.load_unit(self.root, "u3")
        self.assertEqual("reply must end with the criteria/findings object", unclear["turns"][1]["not_counted"])
        self.assertEqual(2, unit_report.summarize([unclear])["results_not_counted"])
        self.assertIn("not counted (reply must end", unit_report.render([unclear], unit_report.summarize([unclear])))
        def refusing(request, commands, cancelled):
            from loop_engine.agents import TurnOutcome
            made = produced(request, commands) if request.role == "producer" else approve(request)
            return TurnOutcome(made.completed, made.text, made.actions, refused=3 if request.role == "producer" else 0)
        engine = self.engine(refusing)
        engine.handle({"request": "start", "contract": self.contract(unit_id="u4")})
        self.run_to_stop(engine, "u4")
        asked = unit_report.load_unit(self.root, "u4")
        self.assertEqual({"producer": 3}, asked["refused_by_execution"])
        self.assertEqual(3, unit_report.summarize([asked])["refused_requests"])
        self.assertIn("producer: requests refused 3 times", unit_report.render([asked], unit_report.summarize([asked])))
        text = unit_report.render(units, summary)
        self.assertIn("u1: done | rounds 1 | 4 turns", text)
        self.assertIn("finding [major] c1 out.txt", text)


class ScenarioTest(unittest.TestCase):
    def test_every_shipped_scenario_builds_a_repository_and_a_contract_the_engine_admits(self):
        root = os.path.join(TOOLS, "scenarios")
        for name in sorted(os.listdir(root)):
            with self.subTest(name), tempfile.TemporaryDirectory() as work:
                contract, expect = scenarios.prepare(os.path.join(root, name), work, unit_id="fixed-id")
                facts.validate(contract)
                self.assertEqual(("fixed-id", os.path.join(work, "repo")), (contract["unit_id"], contract["repository"]))
                self.assertTrue(os.path.isdir(os.path.join(work, "repo", ".git")))
                self.assertIn("stop_reason", expect)
                self.assertNotIn("{", json.dumps(contract["unit"].get("base", "")))

    def test_the_existing_change_is_committed_on_top_of_the_named_base(self):
        with tempfile.TemporaryDirectory() as work:
            contract, _expect = scenarios.prepare(os.path.join(TOOLS, "scenarios", "median-missing-empty-check"), work)
            repository = contract["repository"]
            self.assertEqual(contract["unit"]["base"], scenarios._git(repository, "rev-parse", "HEAD~1"))
            self.assertIn("return 0", scenarios._git(repository, "show", "HEAD:stats.py"))

    def test_judge_names_every_difference_from_the_expectation(self):
        unit = {"stop_reason": "rejected", "turns": [1, 2, 3], "findings": [{"criterion": "c2"}]}
        self.assertEqual([], scenarios.judge({"stop_reason": "rejected", "unmet": ["c2"], "max_turns": 3}, unit))
        self.assertEqual(["stopped as rejected, expected done", "no finding names criterion c1",
                          "3 turns, expected at most 2"],
                         scenarios.judge({"stop_reason": "done", "unmet": ["c1"], "max_turns": 2}, unit))
        self.assertEqual(["the unit was not recorded"], scenarios.judge({"stop_reason": "done"}, None))


if __name__ == "__main__":
    unittest.main()
