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
            "scope": ["src", "tests"], **extra}


def contract(unit_value, facts, executions):
    return validate({"unit_id": "u1", "repository": "/work/repo", "unit": unit_value, "facts": facts,
                     "executions": executions}, "posix")


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
        on_base = f" base={sha(self.sim.base)}" if tree == "base" else ""
        self.cmd(expected_command("RUNNER", name, tree),
                 f"...\nloop-engine-check {name} tree={tree} digest={sha(self.sim.files)}{on_base} exit={code}")

    def shell_write(self, path, content):  # a change the record does not mark as an edit
        self.sim.files[path] = content
        self.cmd(f"printf x > {path}")


class Sim:
    def __init__(self, contract, scripts, files=BASE):
        self.contract, self.scripts = contract, {k: list(v) for k, v in scripts.items()}
        self.base, self.files = dict(files), dict(files)
        self.state, self.feedback_seen = new_state(sha(self.files), base_tree=sha(self.base)), {}

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
        # as many findings as the previous refutation: the work is not converging
        self.assertEqual(("stagnation", "quality", "1 findings after 1 the previous time"),
                         (stop["reason"], stop["fact"], stop["detail"]))
        self.assertEqual(2, sim.state["log"].count("implement"))
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

    def test_a_report_under_judgment_is_quoted_like_a_file(self):
        def assess(quote):
            return lambda turn: json.dumps({"criteria": [{"id": "c1", "met": False, "evidence": []}], "findings": [
                {"severity": "major", "criterion": "c1", "quote": quote, "description": "the log does not show it"}]})
        sim, stop = self.run_sim(investigate(), {"pursue": [lambda t: "cause: A, shown by the log", lambda t: "cause: B, see trace 12"],
                                                 "assess": [assess("shown by the log"), OK]})
        self.assertEqual(("done", ["pursue", "assess", "pursue", "assess"]), (stop["reason"], sim.state["log"]))
        sim, stop = self.run_sim(investigate(), {"pursue": [lambda t: "cause: A, shown by the log"],
                                                 "assess": [assess("a sentence the report does not contain")]})
        self.assertEqual("invalid_result", stop["reason"])
        sim, stop = self.run_sim(implementation(("review-a",)), {
            "survey": [survey], "implement": [implement()], "review-a": [assess("changed add")]})
        self.assertEqual("invalid_result", stop["reason"])  # files are quoted from the tree, not from the author's report

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
        templates = {"reviewer": "{instructions}|{goal}|{criteria}|{changed}|{base}|{reports}|{answers}|{feedback}|"
                                 "{scope}|{checks}"}
        text = prompts.compose(templates, sim.contract, assess, [{"fact": "assessed", "path": "a.py", "line": 3,
                                                                 "description": "wrong"}],
                               check_commands=[], changed=["a.py"], base="/base", reports={"account": "cause: A"},
                               answers=[{"id": "q-1", "question": "which log?", "answer": "the trace",
                                         "asked_by": "pursue"}], agent_config=((".x",), ()))
        self.assertIn("- c1: add returns the sum|- a.py|/base|### account\n\ncause: A|"
                      "### q-1\n\nQuestion: which log?\n\nAnswer: the trace|- [assessed] a.py:3 wrong", text)

    def test_contract_rejects_a_reviewer_on_the_producers_model(self):
        value = implementation()
        value["executions"][2].update(agent="codex", model="m1")
        with self.assertRaises(InvalidContract):
            validate(value, "posix")
        value["unit"]["allow_same_model"] = True
        validate(value, "posix")

    def test_contract_admits_only_its_exact_shape(self):
        def broken(change):
            value = json.loads(json.dumps(implementation()))
            change(value)
            with self.assertRaises(InvalidContract):
                validate(value, "posix")
        broken(lambda v: v.update(command=["sh"]))
        broken(lambda v: v.update(repository="/work/../etc"))
        broken(lambda v: v["executions"][0].update(agent="--help"))
        broken(lambda v: v["executions"][0].update(model="-c evil"))
        broken(lambda v: v["executions"][0].update(argv=["x"]))
        broken(lambda v: v["unit"]["scope"].append("../outside"))
        broken(lambda v: v["facts"][3]["checks"].append({"name": "unit; rm -rf /", "expect": "pass"}))
        broken(lambda v: v["facts"][4].pop("block_at"))
        broken(lambda v: v["executions"][1]["reads"].append("candidate"))  # a fact that reads itself

    def test_the_structure_alone_is_checked_without_what_a_run_adds(self):
        value = implementation()
        structure_facts = [{k: v for k, v in f.items() if k not in facts.FACT_RUN_KEYS.get(f["by"], ())}
                           for f in value["facts"]]
        structure_executions = [{k: v for k, v in e.items() if k not in facts.EXECUTION_RUN_KEYS}
                                for e in value["executions"]]
        facts.check_structure(structure_facts, structure_executions)

        def refused(change, message):
            changed_facts, changed_executions = copy.deepcopy(structure_facts), copy.deepcopy(structure_executions)
            change(changed_facts, changed_executions)
            with self.assertRaises(InvalidContract) as caught:
                facts.check_structure(changed_facts, changed_executions)
            self.assertEqual(message, str(caught.exception))
        refused(lambda f, e: e[1].update(establishs=e[1].pop("establishes")),
                "execution implement: unknown key establishs; missing key establishes")
        refused(lambda f, e: e[0].update(agent="codex"), "execution survey: unknown key agent")
        refused(lambda f, e: f[4].pop("block_at"), "fact quality: missing key block_at")
        refused(lambda f, e: f[4].update(by="opinion"), "fact quality: by must be one of given, report, git, checks, verdict")
        refused(lambda f, e: f[4].update(block_at="fatal"), "fact quality: block_at must be one of minor, major, blocker")
        refused(lambda f, e: f[2].update(subject="nothing"), "fact in-scope: subject 'nothing' is not a fact of the unit")
        refused(lambda f, e: e[2].update(independent_of=["nobody"]),
                "execution review-a: unknown execution nobody in independent_of")

    def test_checks_file_must_define_every_named_check(self):
        raw = {"checks": {"unit": {"argv": ["python", "-m", "unittest"], "test_paths": ["tests"]}}, "ignore": ["__pycache__"]}
        self.assertEqual((["unit"], ["unit"]), facts.check_names(implementation()))
        self.assertEqual({"checks": raw["checks"], "ignore": ["__pycache__"]}, facts.checks_config(raw, ["unit"], ["unit"], "posix"))
        for bad in ({"checks": {}}, {"checks": {"unit": ["python"]}}, {"checks": {"unit": {"argv": []}}},
                    {"checks": {"unit": {"argv": ["python"]}}},  # must fail on base, but names no tests
                    {"checks": {"unit": {"argv": ["python"], "test_paths": ["../x"]}}},
                    {"checks": raw["checks"], "ignore": ["a/b"]}, {"checks": raw["checks"], "run": "x"}):
            with self.assertRaises(InvalidContract):
                facts.checks_config(bad, ["unit"], ["unit"], "posix")

    def test_answer_retry_and_cancel_continue_or_end_a_stopped_unit(self):
        scripts = {"survey": [survey], "review-a": [OK], "review-b": [OK]}
        sim = Sim(implementation(), {**scripts, "implement": [
            lambda t: 'x\n{"question": "which rounding?", "id": "q-round"}', implement()]})
        self.assertEqual("waiting_for_answer", sim.run()["reason"])
        self.assertIsNone(facts.answer(sim.state, "q-other", "x"))
        self.assertIsNone(facts.retry(sim.state))
        sim.state = facts.answer(sim.state, "q-round", "half to even")
        self.assertEqual([{"id": "q-round", "question": "which rounding?", "answer": "half to even",
                           "asked_by": "implement"}], sim.state["answers"])
        self.assertEqual("done", sim.run()["reason"])
        self.assertEqual([], sim.feedback_seen["implement"][1])  # every execution is given the answers instead

        def lost(turn):
            turn.completed = False
        sim = Sim(implementation(), {**scripts, "implement": [lost, implement()]})
        self.assertEqual("unknown_result", sim.run()["reason"])
        sim.state = facts.retry(sim.state)
        self.assertEqual("done", sim.run()["reason"])
        self.assertEqual({"candidate", "checks", "in-scope", "quality", "authority"},
                         {k for k, v in facts.summary(sim.contract, sim.state)["facts"].items() if v["holds"]})
        self.assertEqual("cancelled", decide(sim.contract, facts.cancel(sim.state))[1]["reason"])


class TurnCompletionTest(unittest.TestCase):
    """Whether a finished turn counts, decided from the turn and one observation of the worktree."""

    def completion(self, execution, turn, wrote=False):
        return facts.turn_completion(implementation(), execution, turn, wrote=wrote)

    def test_a_turn_the_agent_did_not_complete_keeps_its_own_detail(self):
        self.assertEqual((False, "cancelled"), self.completion("implement", {"completed": False, "detail": "cancelled"}))

    def test_a_judged_turn_without_an_execution_record_proves_nothing_unless_it_asks(self):
        done = {"completed": True, "detail": None, "text": "done", "actions": None}
        for execution in ("implement", "review-a"):  # one establishes checks, the other a verdict
            with self.subTest(execution=execution):
                self.assertEqual((False, "no-execution-record"), self.completion(execution, done))
                self.assertEqual((True, None), self.completion(execution, {**done, "actions": {"entries": []}}))
                asked = {**done, "text": 'x\n{"question": "which?", "id": "q-1"}'}
                self.assertEqual((True, None), self.completion(execution, asked))
        self.assertEqual((True, None), self.completion("survey", done))  # a report is not judged from a record

    def test_a_turn_that_must_not_write_proves_nothing_once_it_changed_the_worktree(self):
        done = {"completed": True, "detail": None, "text": "done", "actions": {"entries": []}}
        self.assertEqual((False, "a-turn-that-must-not-write-changed-the-worktree"),
                         self.completion("review-a", done, wrote=True))
        self.assertEqual((True, None), self.completion("implement", done, wrote=True))

    def test_changed_agent_configuration_stops_the_unit_and_leaves_the_given_state_as_it_was(self):
        state = new_state("tree")
        stopped = facts.unsafe_agent_config(state, "implement")
        self.assertEqual({"reason": "unsafe_agent_config", "execution": "implement"}, stopped["stop"])
        self.assertIsNone(state["stop"])


class ConvergenceTest(unittest.TestCase):
    """Work goes back while each refutation of a fact leaves fewer findings than the one before."""

    def run_sim(self, contract, scripts):
        sim = Sim(contract, scripts)
        return sim, sim.run()

    def test_a_shrinking_list_of_findings_keeps_going_until_none_is_left(self):
        three = [{**MAJOR, "description": f"problem {i}"} for i in range(3)]
        variants = [implement(f"def add(a, b):\n    return a + b + {i}\n") for i in range(4)]
        sim, stop = self.run_sim(implementation(("review-a",)), {
            "survey": [survey], "implement": variants,
            "review-a": [review(three, met=False), review(three[:2], met=False), review(three[:1], met=False), OK]})
        self.assertEqual(("done", 3), (stop["reason"], sim.state["rounds"]))
        self.assertEqual({"quality": 1}, sim.state["refuted"])

    def test_more_findings_than_the_previous_refutation_stop_the_unit(self):
        variants = [implement(f"def add(a, b):\n    return a + b + {i}\n") for i in range(4)]
        two = [{**MAJOR, "description": f"problem {i}"} for i in range(2)]
        sim, stop = self.run_sim(implementation(("review-a",)), {
            "survey": [survey], "implement": variants,
            "review-a": [review(two[:1], met=False), review(two, met=False)]})
        self.assertEqual(("stagnation", "2 findings after 1 the previous time"), (stop["reason"], stop["detail"]))
        self.assertEqual(["problem 0", "problem 1"], [f["description"] for f in stop["findings"]])

    def test_each_fact_is_compared_with_its_own_previous_refutation(self):
        # checks fail once, then the reviewer refutes once: neither repeats, so the unit goes on to done
        variants = [implement(unit_exit=1), implement("def add(a, b):\n    return b + a\n"), implement()]
        sim, stop = self.run_sim(implementation(("review-a",)), {
            "survey": [survey], "implement": variants, "review-a": [review([MAJOR], met=False), OK]})
        self.assertEqual(("done", {"checks": 1, "quality": 1}), (stop["reason"], sim.state["refuted"]))


class AnswerTest(unittest.TestCase):
    """The requester's answers reach every execution, and a judgment holds only for the answers it was given."""

    def test_an_answer_voids_a_judgment_made_before_it_and_the_same_candidate_is_judged_again(self):
        asking = lambda t: 'the reviewer wants a choice\n{"question": "may the plan defer the choice?", "id": "q-defer"}'
        sim = Sim(implementation(("review-a",)), {
            "survey": [survey], "implement": [implement(), asking, implement()],
            "review-a": [review([MAJOR], met=False), OK]})
        self.assertEqual("waiting_for_answer", sim.run()["reason"])
        self.assertFalse(held(sim.contract, sim.state, "quality"))
        refuted = sim.state["facts"]["quality"]["basis"]
        sim.state = facts.answer(sim.state, "q-defer", "yes, defer it")
        self.assertNotEqual(refuted, facts.basis(sim.contract, sim.state, "quality"))
        stop = sim.run()
        # the producer returned the same candidate after the answer: not stagnation, the reviewer judged it again
        self.assertEqual("done", stop["reason"])
        self.assertEqual(["survey", "implement", "review-a", "implement", "implement", "review-a"], sim.state["log"])

    def test_without_an_answer_the_same_candidate_after_findings_is_stagnation(self):
        sim = Sim(implementation(("review-a",)), {"survey": [survey], "implement": [implement()],
                                                  "review-a": [review([MAJOR], met=False)]})
        self.assertEqual(("stagnation", "candidate unchanged"), (lambda s: (s["reason"], s["detail"]))(sim.run()))

    def test_reports_and_files_judged_before_an_answer_still_hold(self):
        sim = Sim(implementation(("review-a",)), {"survey": [survey], "implement": [implement()], "review-a": [OK]})
        self.assertEqual("done", sim.run()["reason"])
        state = json.loads(json.dumps(sim.state))
        state["answers"].append({"id": "q-x", "question": "?", "answer": "!", "asked_by": "implement"})
        self.assertEqual((True, True, True, False), tuple(held(sim.contract, state, fid) for fid in
                                                          ("authority", "candidate", "checks", "quality")))


# Names that do not contain one another: a record shows a file examined when its name appears in it.
ADDER = implement(path="src/adder.py")


class CorrectedReplyTest(unittest.TestCase):
    """A reply that was not counted is corrected in the next turn without examining everything again."""

    @staticmethod
    def reading(*paths):
        def run(turn):
            turn.read(*paths)
            return json.dumps({"criteria": [{"id": "c1", "met": True, "evidence": [cite(turn.sim)]}], "findings": []})
        return run

    def test_files_examined_for_a_reply_not_counted_still_count_on_the_same_subject(self):
        sim = Sim(implementation(("review-a",)), {"survey": [survey], "implement": [ADDER],
                                                  "review-a": [self.reading("src/adder.py"),
                                                               self.reading("tests/test_calc.py")]})
        self.assertEqual("done", sim.run()["reason"])
        self.assertEqual(["survey", "implement", "review-a", "review-a"], sim.state["log"])
        self.assertIn("read them: tests/test_calc.py", sim.feedback_seen["review-a"][1][0]["description"])

    def test_files_examined_before_the_subject_changed_do_not_count(self):
        sim = Sim(implementation(("review-a",)), {"survey": [survey], "implement": [ADDER],
                                                  "review-a": [self.reading("src/adder.py")]})
        with self.assertRaises(AssertionError):
            sim.run(max_turns=3)  # survey, implement, and a reply not counted
        self.assertEqual(["src/adder.py"], sim.state["examined"]["quality/review-a"]["paths"])
        state = json.loads(json.dumps(sim.state))
        state["tree"] = "changed-since"  # the candidate the reply was about is no longer the present one
        turn = Turn(sim, [])
        turn.read("tests/test_calc.py")
        text = json.dumps({"criteria": [{"id": "c1", "met": True, "evidence": [cite(sim)]}], "findings": []})
        state = apply_turn(sim.contract, state, "review-a", {
            "completed": True, "text": text, "record": {"entries": turn.entries}, "tree": state["tree"],
            "changed": sim.changed()}, read=sim.files.get)
        self.assertIn("read them: src/adder.py", state["feedback"]["review-a"][0]["description"])


class PlatformFactsTest(unittest.TestCase):
    """Paths are judged by the path style the platform's facts give, on whatever machine the test runs."""

    def windows(self, change=lambda v: None):
        value = json.loads(json.dumps(implementation()))
        value["repository"] = "C:\\work\\repo"
        change(value)
        return validate(value, "windows")

    def test_a_windows_contract_names_a_windows_absolute_path(self):
        self.assertEqual("C:\\work\\repo", self.windows()["repository"])
        self.windows(lambda v: v.update(repository="\\\\server\\share\\repo"))
        for repository in ("work\\repo", "C:work\\repo", "C:/work/repo", "/work/repo", "C:\\work\\..\\repo",
                           "C:\\work\\.\\repo", "C:\\work\\\\repo", "C:\\", "\\\\server\\share", "C:\\work\\repo\\",
                           "C:\\work\\re:po", "\\\\?\\C:\\work\\repo", "C:\\work\\CON\\repo",
                           "C:\\work\\nul.txt", "C:\\work.\\repo", "C:\\work \\repo", "\\\\server\\aux\\repo"):
            with self.subTest(repository), self.assertRaises(InvalidContract):
                self.windows(lambda v: v.update(repository=repository))
        with self.assertRaises(InvalidContract):  # a Windows path is not absolute on a POSIX machine
            validate({**implementation(), "repository": "C:\\work\\repo"}, "posix")
        with self.assertRaises(ValueError):
            validate(implementation(), "macos")

    def test_paths_inside_the_repository_are_gits_form_on_every_platform(self):
        self.assertEqual(["src", "tests/"], self.windows(lambda v: v["unit"].update(scope=["src", "tests/"]))["unit"]["scope"])
        for scope in ("src\\calc.py", "C:src", "src/con:x", "src/a.", "src/a ", "/src", "src/CON", "nul",
                      "src/nul.txt", "Com1/x", "src/lpt9.tar.gz", "aux.", "PRN"):
            with self.subTest(scope), self.assertRaises(InvalidContract):
                self.windows(lambda v: v["unit"].update(scope=[scope]))
        # a device name is refused as a whole name only, with or without an extension
        self.windows(lambda v: v["unit"].update(scope=["src/console.py", "src/connector", "src/com10", "lpt/x"]))
        # on POSIX a backslash is a character of a file name, and CON is a name like any other
        validate({**implementation(), "unit": unit(scope=["src\\calc.py", "src/CON", "src/a."])}, "posix")
        raw = {"checks": {"unit": {"argv": ["python"], "test_paths": ["tests/test_calc.py"]}}, "ignore": ["__pycache__"]}
        self.assertEqual(["tests/test_calc.py"], facts.checks_config(raw, ["unit"], ["unit"], "windows")["checks"]["unit"]["test_paths"])
        for bad in ({**raw, "checks": {"unit": {"argv": ["python"], "test_paths": ["tests\\test_calc.py"]}}},
                    {**raw, "checks": {"unit": {"argv": ["python"], "test_paths": ["C:\\tests"]}}},
                    {**raw, "checks": {"unit": {"argv": ["python"], "test_paths": ["tests/NUL.py"]}}},
                    {**raw, "checks": {"unit": {"argv": ["python"], "test_paths": ["tests/x "]}}},
                    {**raw, "ignore": ["cache\\x"]}, {**raw, "ignore": ["a:b"]}, {**raw, "ignore": ["com3"]},
                    {**raw, "ignore": ["cache."]}):
            with self.subTest(bad), self.assertRaises(InvalidContract):
                facts.checks_config(bad, ["unit"], ["unit"], "windows")

    def test_a_path_on_another_drive_or_share_is_not_inside_a_root(self):
        for path, root in (("D:\\dev\\game", "D:\\dev\\game"), ("D:\\dev\\game\\tools\\engine", "D:\\dev\\game"),
                           ("d:\\Dev\\Game\\src", "D:\\dev\\game"), ("C:\\", "C:\\"), ("C:\\x", "C:\\"),
                           ("\\\\server\\share\\repo\\src", "\\\\server\\share\\repo"),
                           ("\\\\server\\share\\repo", "\\\\server\\share")):
            with self.subTest(path=path, root=root):
                self.assertTrue(facts.inside(path, root, "windows"))
        for path, root in (("C:\\Users\\me\\engine", "D:\\dev\\game"), ("D:\\dev\\gameplay", "D:\\dev\\game"),
                           ("D:\\dev", "D:\\dev\\game"), ("D:\\other\\game", "D:\\dev\\game"),
                           ("\\\\server\\share\\repo", "D:\\repo"), ("\\\\other\\share\\repo", "\\\\server\\share\\repo"),
                           ("\\\\server\\other\\repo", "\\\\server\\share\\repo")):
            with self.subTest(path=path, root=root):
                self.assertFalse(facts.inside(path, root, "windows"))
        for path, root in (("/srv/repo", "/srv/repo"), ("/srv/repo/packages", "/srv/repo"), ("/", "/"),
                           ("/srv/repo", "/")):
            with self.subTest(path=path, root=root):
                self.assertTrue(facts.inside(path, root, "posix"))
        for path in ("/srv/repository", "/srv", "/opt/repo", "/SRV/repo/packages"):
            with self.subTest(path=path):
                self.assertFalse(facts.inside(path, "/srv/repo", "posix"))


class TrailingNewlineTest(unittest.TestCase):
    """An identifier is the whole value: one that ends with a newline is refused."""

    def refused(self, change):
        value = json.loads(json.dumps(implementation()))
        change(value)
        with self.assertRaises(InvalidContract):
            validate(value, "posix")

    def test_every_identifier_of_a_contract_refuses_a_trailing_newline(self):
        self.refused(lambda v: v.update(unit_id="u1\n"))
        self.refused(lambda v: v["unit"]["criteria"][0].update(id="c1\n"))
        self.refused(lambda v: v["facts"][3]["checks"][0].update(name="unit\n"))
        self.refused(lambda v: v["executions"][0].update(agent="codex\n"))
        self.refused(lambda v: v["executions"][0].update(model="m1\n"))
        self.refused(lambda v: v["unit"].update(base="0" * 40 + "\n"))
        self.refused(lambda v: v["facts"][0].update(id="authority\n"))
        validate(json.loads(json.dumps(implementation())) | {"unit": unit(base="0" * 40)}, "posix")

    def test_the_skills_a_contract_excludes_are_one_plugin_and_admitted_names(self):
        skills = {"plugin": "dev", "names": ["design", "test-harness"]}
        validate(json.loads(json.dumps(implementation())) | {"unit": unit(excluded_skills=skills)}, "posix")
        for bad in (None, [], {"plugin": "dev"}, {**skills, "extra": 1}, {**skills, "plugin": ""},
                    {**skills, "plugin": "dev\n"}, {**skills, "names": []}, {**skills, "names": "design"},
                    {**skills, "names": ["a,b"]}, {**skills, "names": ["a", "a"]}, {**skills, "names": ["-c"]},
                    {**skills, "names": ["a b"]}, {**skills, "names": [3]}):
            with self.subTest(bad=bad):
                self.refused(lambda v, bad=bad: v["unit"].update(excluded_skills=bad))

    def test_a_question_id_with_a_trailing_newline_is_no_question(self):
        self.assertIsNotNone(facts.question('x\n{"question": "which?", "id": "q-1"}'))
        self.assertIsNone(facts.question('x\n{"question": "which?", "id": "q-1\\n"}'))


class VerdictReasonTest(unittest.TestCase):
    """A reply the engine does not accept is answered with what the engine saw in it."""

    UNIT = {"criteria": [{"id": "c1", "text": "one"}, {"id": "c2", "text": "two"}]}
    FACT = {"block_at": "major"}

    def reason(self, text, record=None, paths=()):
        result = facts.judge_verdict(self.UNIT, self.FACT, text, record or {"entries": []},
                                     lambda path: "line one is here\n", list(paths))
        self.assertEqual("invalid", result[0])
        return result[1]

    def test_findings_nested_in_the_last_criterion_name_the_keys_of_the_object_the_reply_ends_with(self):
        evidence = '{"path": "a.py", "line": 1, "quote": "line one is here"}'
        text = ('reviewed\n{"criteria":[{"id":"c1","met":true,"evidence":[' + evidence + ']},'
                '{"id":"c2","met":false,"evidence":[' + evidence + '],"findings":[{"severity":"major",'
                '"criterion":"c2","path":"a.py","line":1,"quote":"line one is here","description":"wrong"}]}')
        self.assertEqual(
            'the object your reply ends with has the keys "evidence", "findings", "id", "met"; the reply must end '
            "with one object whose keys are criteria and findings, and every list and object before findings must "
            "be closed", self.reason(text))
        self.assertEqual("reply must end with the criteria/findings object", self.reason("no object here"))
        self.assertIn("has the keys none;", self.reason("x {}"))

    def test_other_reasons_name_what_the_reply_held(self):
        def reply(criteria, findings=()):
            return json.dumps({"criteria": criteria, "findings": list(findings)})
        met = {"met": True, "evidence": [{"path": "a.py", "line": 1, "quote": "line one is here"}]}
        self.assertEqual('criteria must be a list of judgments, not "c1"', self.reason(reply("c1")))
        self.assertEqual('every criterion needs exactly one judgment: the criteria are c1, c2; your judgments name '
                         '["c1", "c3"]', self.reason(reply([{"id": "c1", **met}, {"id": "c3", **met}])))
        self.assertEqual('findings must be a list, not {"severity": "major"}', self.reason(
            json.dumps({"criteria": [{"id": "c1", **met}, {"id": "c2", **met}], "findings": {"severity": "major"}})))
        self.assertEqual('findings need a severity of minor, major, blocker: finding 1 has "high"; finding 1 does not '
                         'quote the present file or report: None:None', self.reason(
            reply([{"id": "c1", **met}, {"id": "c2", **met}], [{"severity": "high", "quote": "line one is here"}])))
        self.assertEqual('criterion c2: met must be true or false, not "partly"',
                         self.reason(reply([{"id": "c1", **met}, {"id": "c2", "met": "partly"}])))
        self.assertEqual("the record does not show these changed files being examined (1 of 2); read them: src/b.py",
                         self.reason(reply([{"id": "c1", **met}, {"id": "c2", **met}]),
                                     {"entries": [{"kind": "read", "paths": ["src/a.py"], "command": None}]},
                                     ["src/a.py", "src/b.py"]))

    def test_an_item_that_is_not_an_object_is_named_and_never_raises(self):
        met = {"met": True, "evidence": [{"path": "a.py", "line": 1, "quote": "line one is here"}]}
        self.assertEqual('judgment 2 of criteria must be an object, not "c2"',
                         self.reason(json.dumps({"criteria": [{"id": "c1", **met}, "c2"], "findings": []})))
        self.assertEqual('finding 1 must be an object, not ["major"]', self.reason(json.dumps(
            {"criteria": [{"id": "c1", **met}, {"id": "c2", **met}], "findings": [["major"]]})))
        self.assertEqual('criterion c2: evidence item 2 must be an object, not "ran the tests"', self.reason(json.dumps(
            {"criteria": [{"id": "c1", **met}, {"id": "c2", "met": True, "evidence": [
                met["evidence"][0], "ran the tests"]}], "findings": []})))
        self.assertEqual('criterion c1: evidence must be a list, not "see a.py"', self.reason(json.dumps(
            {"criteria": [{"id": "c1", "met": True, "evidence": "see a.py"}, {"id": "c2", **met}], "findings": []})))
        self.assertEqual('criterion c1: evidence must be a list, not {"path": "a.py"}', self.reason(json.dumps(
            {"criteria": [{"id": "c1", "met": False, "evidence": {"path": "a.py"}}, {"id": "c2", **met}],
             "findings": []})))

    def test_a_cited_command_is_one_line_or_part_of_a_command_run_in_this_turn_and_nothing_added(self):
        script = "cd /work\npython3 -m unittest tests.test_stats -v\necho done"
        record = {"entries": [{"kind": "command", "command": script}]}

        def reply(command):
            return json.dumps({"criteria": [
                {"id": c, "met": True, "evidence": [{"command": command}]} for c in ("c1", "c2")], "findings": []})
        accepted = facts.judge_verdict(self.UNIT, self.FACT, reply("python3 -m unittest tests.test_stats -v"),
                                       record, lambda path: "", [])
        self.assertEqual("ok", accepted[0])
        reasons = self.reason(reply("python3 -m unittest tests.test_stats -v (run against original stats.py)"), record)
        for criterion in ("c1", "c2"):  # every reason in one answer
            self.assertIn(
                f"evidence for {criterion}: the command 'python3 -m unittest tests.test_stats -v (run against original "
                "stats.py)' is not among the commands you ran in this turn; copy the cited text exactly from one of "
                "them, one line or part of it and nothing added, or run it again", reasons)


if __name__ == "__main__":
    unittest.main()
