"""The facts engine with scripted agents: the order of turns comes from the contract's data alone."""
import copy
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from loop_engine import facts  # noqa: E402
from loop_engine.facts import (  # noqa: E402
    InvalidContract, apply_turn, decide, expected_command, held, new_state, sha, validate,
)

BASE = {"src/calc.py": "def add(a, b):\n    return a - b\n", "tests/test_calc.py": "# tests\n", "docs/x.md": "x\n"}
FIXED = "def add(a, b):\n    return a + b\n"


def ex(eid, agent, model, reads, establishes, writes=False, independent_of=()):
    return {"id": eid, "agent": agent, "model": model, "effort": "low", "instructions": eid, "reads": list(reads),
            "establishes": list(establishes), "writes": writes, "independent_of": list(independent_of)}


def unit(**extra):
    return {"goal": "make add return the sum", "criteria": [{"id": "c1", "text": "add returns the sum"}],
            "scope": ["src", "tests"], "limits": {"rounds": 3}, **extra}


def contract(unit_value, facts, executions):
    return validate({"unit_id": "u1", "repository": "/work/repo", "unit": unit_value, "facts": facts,
                     "executions": executions})


def implementation(reviewers=("review-a", "review-b"), **extra):
    review_reads = ["candidate", "checks", "in-scope", "authority"]
    models = {"review-a": ("claude", "m2"), "review-b": ("codex", "m3")}
    return contract(unit(**extra), [
        {"id": "authority", "required": True, "by": "report"},
        {"id": "candidate", "required": True, "by": "report"},
        {"id": "in-scope", "required": True, "by": "git", "subject": "candidate"},
        {"id": "checks", "required": True, "by": "checks", "subject": "candidate", "checks": [
            {"name": "unit", "expect": "pass"}, {"name": "unit", "expect": "fails_on_base"}]},
        {"id": "quality", "required": True, "by": "verdict", "subject": "candidate", "block_at": "major"},
    ], [
        ex("survey", "codex", "m1", [], ["authority"]),
        ex("implement", "codex", "m1", ["authority"], ["candidate", "checks"], writes=True),
        *[ex(r, *models[r], review_reads, ["quality"], independent_of=["implement"]) for r in reviewers],
    ])


def review_only():
    return contract(unit(), [
        {"id": "subject", "required": False, "by": "given"},
        {"id": "quality", "required": True, "by": "verdict", "subject": "subject", "block_at": "major"},
    ], [ex("code", "claude", "m2", [], ["quality"]), ex("tests", "codex", "m3", [], ["quality"])])


def investigate():
    return contract(unit(), [
        {"id": "account", "required": True, "by": "report"},
        {"id": "assessed", "required": True, "by": "verdict", "subject": "account", "block_at": "major"},
    ], [ex("pursue", "codex", "m1", [], ["account"]),
        ex("assess", "claude", "m2", ["account"], ["assessed"], independent_of=["pursue"])])


class Turn:
    def __init__(self, sim, feedback):
        self.sim, self.feedback, self.entries, self.completed, self.config = sim, feedback, [], True, False

    def _add(self, kind, **fields):
        self.entries.append({"seq": len(self.entries) + 1, "kind": kind, "command": None, "paths": [],
                             "ended": True, "output_tail": None, **fields})

    def edit(self, path, content):
        self.sim.files[path] = content
        self._add("edit", paths=[path])

    def read(self, *paths):
        self._add("read", paths=list(paths))

    def cmd(self, command, output=""):
        self._add("command", command=command, output_tail=output)

    def check(self, name, code, tree="candidate"):
        self.cmd(expected_command("RUNNER", name, tree),
                 f"...\nloop-engine-check {name} tree={tree} digest={sha(self.sim.files)} exit={code}")

    def shell_write(self, path, content):  # a change the record does not mark as an edit
        self.sim.files[path] = content
        self.cmd(f"printf x > {path}")


class Sim:
    def __init__(self, contract, scripts, files=BASE):
        self.contract, self.scripts = contract, {k: list(v) for k, v in scripts.items()}
        self.base, self.files = dict(files), dict(files)
        self.state, self.feedback_seen = new_state(sha(self.files)), {}

    def changed(self):
        return sorted(p for p in set(self.base) | set(self.files) if self.base.get(p) != self.files.get(p))

    def run(self, max_turns=30, reload=False):
        for _ in range(max_turns):
            if reload:  # the state survives a process restart as plain JSON
                self.state = json.loads(json.dumps(self.state))
            self.state, action = decide(self.contract, self.state)
            if action["do"] == "stop":
                return action
            name = action["execution"]
            script = self.scripts[name]
            fn = script.pop(0) if len(script) > 1 else script[0]
            turn = Turn(self, action["feedback"])
            self.feedback_seen.setdefault(name, []).append(copy.deepcopy(action["feedback"]))
            text = fn(turn)
            self.state = apply_turn(self.contract, self.state, name, {
                "completed": turn.completed, "text": text, "record": {"entries": turn.entries},
                "tree": sha(self.files), "changed": self.changed(),
            }, read=self.files.get)
        raise AssertionError("did not stop")


def survey(turn):
    turn.read("docs/x.md")
    return "authorities: none conflict"


def implement(content=FIXED, path="src/calc.py", unit_exit=0, base_exit=1, checks_first=False):
    def run(turn):
        if checks_first:
            turn.check("unit", unit_exit)
            turn.check("unit", base_exit, "base")
        turn.edit(path, content)
        turn.edit("tests/test_calc.py", "# tests\nassert add(1, 2) == 3\n")
        if not checks_first:
            turn.check("unit", unit_exit)
            turn.check("unit", base_exit, "base")
        return "changed add"
    return run


def cite(sim, path="src/calc.py", line=2):
    return {"path": path, "line": line, "quote": sim.files[path].splitlines()[line - 1].strip()}


def review(findings=(), met=True, examine=True, evidence=True, criteria=("c1",), raw=None):
    def run(turn):
        if examine:
            turn.read(*turn.sim.changed())
        if raw is not None:
            return raw
        found = [{**cite(turn.sim), **f} for f in findings]
        return "reviewed\n" + json.dumps({"criteria": [
            {"id": c, "met": met, "evidence": [cite(turn.sim)] if evidence else []} for c in criteria],
            "findings": found})
    return run


OK = review()
MAJOR = {"severity": "major", "description": "overflow is not handled", "criterion": "c1"}
MINOR = {"severity": "minor", "description": "name could be clearer"}


class DecisionTest(unittest.TestCase):
    def run_sim(self, contract, scripts, **kw):
        sim = Sim(contract, scripts)
        return sim, sim.run(**kw)

    def test_order_is_derived_from_the_facts(self):
        sim, stop = self.run_sim(implementation(), {"survey": [survey], "implement": [implement()],
                                                    "review-a": [OK], "review-b": [OK]})
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["survey", "implement", "review-a", "review-b"], sim.state["log"])

    def test_every_reviewer_is_heard_before_the_findings_go_back_and_the_survey_is_kept(self):
        other = {"severity": "blocker", "description": "wrong sign for negatives", "criterion": "c1"}
        sim, stop = self.run_sim(implementation(), {
            "survey": [survey], "implement": [implement("def add(a, b):\n    return b + a\n"), implement()],
            "review-a": [review([MAJOR], met=False), OK], "review-b": [review([other], met=False), OK]})
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["survey", "implement", "review-a", "review-b", "implement", "review-a", "review-b"],
                         sim.state["log"])
        sent = sim.feedback_seen["implement"][1]
        self.assertEqual({"overflow is not handled", "wrong sign for negatives"}, {f["description"] for f in sent})

    def test_failed_or_stale_checks_never_reach_a_reviewer(self):
        def shell_after(turn):
            implement()(turn)
            turn.shell_write("src/calc.py", FIXED + "# tweak\n")
            return "done"
        for first in (implement(unit_exit=1), implement(checks_first=True), implement(base_exit=0), shell_after):
            sim, stop = self.run_sim(implementation(), {"survey": [survey], "implement": [first, implement()],
                                                        "review-a": [OK], "review-b": [OK]})
            self.assertEqual(["survey", "implement", "implement", "review-a", "review-b"], sim.state["log"])
            self.assertEqual("done", stop["reason"])
            self.assertTrue(all(f["fact"] == "checks" for f in sim.feedback_seen["implement"][1]))

    def test_a_change_outside_the_scope_goes_back_without_review(self):
        sim, stop = self.run_sim(implementation(), {
            "survey": [survey], "implement": [implement("y\n", path="docs/x.md"), lambda t: (
                t.edit("docs/x.md", "x\n"), implement()(t))[1]], "review-a": [OK], "review-b": [OK]})
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["survey", "implement", "implement", "review-a", "review-b"], sim.state["log"])
        self.assertEqual("in-scope", sim.feedback_seen["implement"][1][0]["fact"])

    def test_a_minor_finding_is_noted_and_does_not_block(self):
        sim, stop = self.run_sim(implementation(), {"survey": [survey], "implement": [implement()],
                                                    "review-a": [review([MINOR])], "review-b": [OK]})
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["name could be clearer"], [n["description"] for n in sim.state["notes"]])

    def test_an_unmet_criterion_blocks_whatever_weight_its_finding_has(self):
        sim, stop = self.run_sim(implementation(), {
            "survey": [survey], "implement": [implement("def add(a, b):\n    return b + a\n"), implement()],
            "review-a": [review([{**MINOR, "criterion": "c1"}], met=False), OK], "review-b": [OK]})
        self.assertEqual(2, sim.state["log"].count("implement"))

    def test_results_that_cannot_be_checked_do_not_count(self):
        invalid = {
            "approval stated": review(raw='{"verdict": "approve", "findings": []}'),
            "no evidence": review(evidence=False),
            "not examined": review(examine=False),
            "criterion missing": review(criteria=()),
            "quote not in file": review([{**MAJOR, "quote": "return a * b"}], met=False),
            "unmet without finding": review(met=False),
        }
        for label, bad in invalid.items():
            with self.subTest(label):
                sim, stop = self.run_sim(implementation(("review-a",)), {
                    "survey": [survey], "implement": [implement()], "review-a": [bad, OK]})
                self.assertEqual(["survey", "implement", "review-a", "review-a"], sim.state["log"])
                self.assertEqual("done", stop["reason"])
                sim, stop = self.run_sim(implementation(("review-a",)), {
                    "survey": [survey], "implement": [implement()], "review-a": [bad]})
                self.assertEqual("invalid_result", stop["reason"])

    def test_stops_are_distinct(self):
        base = {"survey": [survey], "review-a": [review([MAJOR], met=False)], "review-b": [OK]}
        sim, stop = self.run_sim(implementation(), {**base, "implement": [implement()]})
        self.assertEqual(("stagnation", "candidate unchanged"), (stop["reason"], stop["detail"]))
        variants = [implement(f"def add(a, b):\n    return a + b + {i}\n") for i in range(6)]
        sim, stop = self.run_sim(implementation(), {**base, "implement": variants})
        self.assertEqual("stagnation", stop["reason"])  # the same finding came back
        changing = [review([{**MAJOR, "description": f"problem {i}"}], met=False) for i in range(6)]
        sim, stop = self.run_sim(implementation(), {**base, "implement": variants, "review-a": changing})
        self.assertEqual("rounds_exhausted", stop["reason"])
        sim, stop = self.run_sim(implementation(), {**base, "implement": [
            lambda t: 'x\n{"question": "which rounding?", "id": "q-round"}']})
        self.assertEqual("waiting_for_answer", stop["reason"])

        def lost(turn):
            turn.completed = False
        sim, stop = self.run_sim(implementation(), {**base, "implement": [lost]})
        self.assertEqual("unknown_result", stop["reason"])

    def test_review_without_a_producer_ends_with_the_findings(self):
        sim, stop = self.run_sim(review_only(), {"code": [OK], "tests": [review([MAJOR], met=False)]})
        self.assertEqual(("rejected", ["overflow is not handled"]),
                         (stop["reason"], [f["description"] for f in stop["findings"]]))
        self.assertEqual(["code", "tests"], sim.state["log"])
        sim, stop = self.run_sim(review_only(), {"code": [OK], "tests": [OK]})
        self.assertEqual("done", stop["reason"])

    def test_a_report_is_sent_back_to_its_author(self):
        sim, stop = self.run_sim(investigate(), {
            "pursue": [lambda t: "cause: A", lambda t: "cause: B, shown by the log"],
            "assess": [review([MAJOR], met=False), OK]})
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["pursue", "assess", "pursue", "assess"], sim.state["log"])
        sim, stop = self.run_sim(investigate(), {"pursue": [lambda t: "cause: A"],
                                                 "assess": [review([MAJOR], met=False)]})
        self.assertEqual(("stagnation", "report unchanged"), (stop["reason"], stop["detail"]))

    def test_state_survives_restart_and_a_tree_changed_outside_a_turn_voids_what_judged_it(self):
        scripts = {"survey": [survey], "implement": [implement()], "review-a": [OK], "review-b": [OK]}
        sim, stop = self.run_sim(implementation(), scripts, reload=True)
        self.assertEqual("done", stop["reason"])
        state = json.loads(json.dumps(sim.state))
        state["stop"], state["tree"] = None, "edited-by-hand"
        self.assertFalse(held(sim.contract, state, "quality"))
        self.assertTrue(held(sim.contract, state, "authority"))
        _, action = decide(sim.contract, state)
        self.assertEqual("implement", action["execution"])

    def test_a_reviewer_is_given_the_files_to_judge_not_the_authors_account_of_them(self):
        from loop_engine import prompts
        sim, _stop = self.run_sim(implementation(), {"survey": [survey], "implement": [implement()],
                                                     "review-a": [OK], "review-b": [OK]})
        by_id = {e["id"]: e for e in sim.contract["executions"]}
        self.assertEqual({"authority": "authorities: none conflict"},
                         prompts.inputs(sim.contract, sim.state, by_id["implement"]))
        self.assertEqual({"authority": "authorities: none conflict"},
                         prompts.inputs(sim.contract, sim.state, by_id["review-a"]))
        self.assertEqual(["reporter", "writer", "reviewer"],
                         [prompts.kind(sim.contract, by_id[e]) for e in ("survey", "implement", "review-a")])
        sim, _stop = self.run_sim(investigate(), {"pursue": [lambda t: "cause: A, shown by the log"], "assess": [OK]})
        assess = sim.contract["executions"][1]
        self.assertEqual({"account": "cause: A, shown by the log"}, prompts.inputs(sim.contract, sim.state, assess))
        templates = {"reviewer": "{instructions}|{goal}|{criteria}|{changed}|{base}|{reports}|{feedback}|{scope}|{checks}"}
        text = prompts.compose(templates, sim.contract, assess, [{"fact": "assessed", "path": "a.py", "line": 3,
                                                                 "description": "wrong"}],
                               check_commands=[], changed=["a.py"], base="/base", reports={"account": "cause: A"})
        self.assertIn("- c1: add returns the sum|- a.py|/base|### account\n\ncause: A|- [assessed] a.py:3 wrong", text)

    def test_contract_rejects_a_reviewer_on_the_producers_model(self):
        value = implementation()
        value["executions"][2].update(agent="codex", model="m1")
        with self.assertRaises(InvalidContract):
            validate(value)
        value["unit"]["allow_same_model"] = True
        validate(value)

    def test_contract_admits_only_its_exact_shape(self):
        def broken(change):
            value = json.loads(json.dumps(implementation()))
            change(value)
            with self.assertRaises(InvalidContract):
                validate(value)
        broken(lambda v: v.update(command=["sh"]))
        broken(lambda v: v.update(repository="/work/../etc"))
        broken(lambda v: v["executions"][0].update(agent="--help"))
        broken(lambda v: v["executions"][0].update(model="-c evil"))
        broken(lambda v: v["executions"][0].update(argv=["x"]))
        broken(lambda v: v["unit"]["scope"].append("../outside"))
        broken(lambda v: v["facts"][3]["checks"].append({"name": "unit; rm -rf /", "expect": "pass"}))
        broken(lambda v: v["facts"][4].pop("block_at"))
        broken(lambda v: v["executions"][1]["reads"].append("candidate"))  # a fact that reads itself

    def test_checks_file_must_define_every_named_check(self):
        raw = {"checks": {"unit": {"argv": ["python", "-m", "unittest"], "test_paths": ["tests"]}}, "ignore": ["__pycache__"]}
        self.assertEqual((["unit"], ["unit"]), facts.check_names(implementation()))
        self.assertEqual({"checks": raw["checks"], "ignore": ["__pycache__"]}, facts.checks_config(raw, ["unit"], ["unit"]))
        for bad in ({"checks": {}}, {"checks": {"unit": ["python"]}}, {"checks": {"unit": {"argv": []}}},
                    {"checks": {"unit": {"argv": ["python"]}}},  # must fail on base, but names no tests
                    {"checks": {"unit": {"argv": ["python"], "test_paths": ["../x"]}}},
                    {"checks": raw["checks"], "ignore": ["a/b"]}, {"checks": raw["checks"], "run": "x"}):
            with self.assertRaises(InvalidContract):
                facts.checks_config(bad, ["unit"], ["unit"])

    def test_answer_retry_and_cancel_continue_or_end_a_stopped_unit(self):
        scripts = {"survey": [survey], "review-a": [OK], "review-b": [OK]}
        sim = Sim(implementation(), {**scripts, "implement": [
            lambda t: 'x\n{"question": "which rounding?", "id": "q-round"}', implement()]})
        self.assertEqual("waiting_for_answer", sim.run()["reason"])
        self.assertIsNone(facts.answer(sim.state, "q-other", "x"))
        self.assertIsNone(facts.retry(sim.state))
        sim.state = facts.answer(sim.state, "q-round", "half to even")
        self.assertEqual("done", sim.run()["reason"])
        self.assertIn("half to even", sim.feedback_seen["implement"][1][0]["description"])

        def lost(turn):
            turn.completed = False
        sim = Sim(implementation(), {**scripts, "implement": [lost, implement()]})
        self.assertEqual("unknown_result", sim.run()["reason"])
        sim.state = facts.retry(sim.state)
        self.assertEqual("done", sim.run()["reason"])
        self.assertEqual({"candidate", "checks", "in-scope", "quality", "authority"},
                         {k for k, v in facts.summary(sim.contract, sim.state)["facts"].items() if v["holds"]})
        self.assertEqual("cancelled", decide(sim.contract, facts.cancel(sim.state))[1]["reason"])


if __name__ == "__main__":
    unittest.main()
