"""One unit from start to each kind of stop, over a real repository and scripted agents."""
import json
import os
import threading
import unittest

from engine_support import (
    EngineCase, actions, approve, changes, git, git_out, produced, run_command, status_line, write,
)

from loop_engine import store
from loop_engine.agents import TurnOutcome


class FlowTest(EngineCase):
    def test_checks_pass_then_reviewer_approves(self):
        def script(request, commands, cancelled):
            return produced(request, commands) if request.role == "producer" else approve(request)
        engine, status = self.start(script)
        self.assertEqual(("stopped", "done", 0), (status["state"], status["stop_reason"], status["round"]))
        self.assertIn(b"+made", self.result("patch.diff"))
        stop = json.loads(self.result("stop.json"))
        self.assertEqual("done", stop["stop_reason"])
        self.assertEqual({"candidate": "report", "checks": "checks", "quality": "verdict"},
                         {name: fact["by"] for name, fact in stop["facts"].items() if fact["holds"]})
        self.assertEqual(["producer", "reviewer"], [request.role for request in self.agents.requests])
        producer, reviewer = self.agents.requests
        self.assertEqual(("alpha", "m-1", None), (producer.agent, producer.model, producer.effort))
        self.assertEqual(("beta", None, "high"), (reviewer.agent, reviewer.model, reviewer.effort))
        self.assertEqual(producer.cwd, reviewer.cwd)
        self.assertFalse(os.path.commonpath([producer.cwd, self.repo]) == self.repo)
        self.assertIn("You produce.", producer.prompt)
        self.assertIn("check_runner.py", producer.prompt)
        self.assertNotIn("wrote out.txt", reviewer.prompt)  # a reviewer reads the files, not the author's account
        self.assertIn("- out.txt", reviewer.prompt)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "out.txt")))

    def test_roles_are_pointed_only_at_what_is_published_never_at_the_records(self):
        def script(request, commands, cancelled):
            return produced(request, commands) if request.role == "producer" else approve(request)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        # neither place is inside the other, nor a prefix of the other's path as a string
        self.assertFalse(os.path.commonpath([self.root, self.published]) in (self.root, self.published))
        self.assertFalse(self.root.startswith(self.published) or self.published.startswith(self.root))
        for request in self.agents.requests:
            self.assertNotIn(self.root + os.sep, request.prompt)
            self.assertIn(self.published + os.sep, request.prompt)  # the check program or the copy of the base
        published = sorted(os.path.relpath(os.path.join(folder, name), self.published)
                           for folder, _dirs, names in os.walk(self.published) for name in names)
        self.assertEqual(["units/u1/checks.json"], [name for name in published if not name.startswith("check/")])
        records = os.listdir(self.unit().dir)
        self.assertNotIn("checks.json", records)
        self.assertFalse(any(name.startswith("base-") for name in records))

    def test_the_real_check_program_output_is_what_gets_judged(self):
        outputs = []

        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            write(request.cwd, "out.txt", "made by the producer\n")
            outputs.append(run_command(commands["unit"], request.cwd))
            return TurnOutcome(True, "done", actions(("edit", None, None), ("command", commands["unit"], outputs[-1])))
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        self.assertRegex(outputs[0].rstrip().splitlines()[-1],
                         r"^loop-engine-check unit tree=candidate digest=[0-9a-f]{32} exit=0$")

    def test_missing_or_failing_checks_send_the_producer_back_without_a_reviewer(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            turn = self.agents.nth["producer"]
            if request.unit_id == "u1" and turn == 1:
                write(request.cwd, "out.txt", "made by the producer\n")
                return TurnOutcome(True, "all checks passed, trust me", actions(("edit", None, None)))
            if request.unit_id == "u1":
                return TurnOutcome(True, "ran it", actions(
                    ("command", commands["unit"], "boom\n" + status_line(request.cwd, "unit", 1) + "\n")))
            if turn == 1:  # the check ran, then the file changed through the shell
                record = actions(("command", commands["unit"], status_line(request.cwd, "unit", 0)),
                                 ("command", "printf later >> out.txt", ""))
                write(request.cwd, "out.txt", "made by the producer, then changed\n")
                return TurnOutcome(True, "ran it", record)
            return produced(request, commands)
        _engine, status = self.start(script)
        # the check failed again with no fewer failures than before: sending it back again would not converge
        self.assertEqual(("stagnation", 1), (status["stop_reason"], status["round"]))
        self.assertEqual(["producer", "producer"], [request.role for request in self.agents.requests])
        self.assertIn("[checks] unit:candidate not run", self.agents.requests[1].prompt)
        stop = json.loads(self.result("stop.json"))["stop"]
        self.assertEqual(("checks", ["exit 1"]), (stop["fact"], [finding["description"] for finding in stop["findings"]]))
        _engine, status = self.start(script, unit_id="u2")
        self.assertEqual(("done", 1), (status["stop_reason"], status["round"]))
        self.assertIn("[checks] unit:candidate not run on the present tree", self.agents.requests[1].prompt)

    def test_reviewer_findings_come_back_until_approved(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands, text=f"attempt number {self.agents.nth['producer']}\n")
            return changes(request, "handle the empty case") if self.agents.nth["reviewer"] == 1 else approve(request)
        _engine, status = self.start(script)
        self.assertEqual(("done", 1), (status["stop_reason"], status["round"]))
        self.assertIn("[quality] out.txt:1 handle the empty case", self.agents.requests[2].prompt)
        self.assertIn(b"+attempt number 2", self.result("patch.diff"))

    def test_work_goes_back_while_the_findings_shrink_and_stops_when_they_do_not(self):
        def shrinking(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands, text=f"attempt number {self.agents.nth['producer']}\n")
            left = 4 - self.agents.nth["reviewer"]  # 3, 2, 1, then none
            return changes(request, *[f"problem {n}" for n in range(left)]) if left else approve(request)
        _engine, status = self.start(shrinking)
        self.assertEqual(("done", 3), (status["stop_reason"], status["round"]))

        def as_many_new(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands, text=f"attempt number {self.agents.nth['producer']}\n")
            return changes(request, f"problem {self.agents.nth['reviewer']}")
        _engine, status = self.start(as_many_new, unit_id="u2")
        self.assertEqual(("stagnation", 1), (status["stop_reason"], status["round"]))
        stop = json.loads(self.result("stop.json", "u2"))["stop"]
        self.assertEqual(("quality", "1 findings after 1 the previous time"), (stop["fact"], stop["detail"]))

        def same_finding(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands, text=f"attempt number {self.agents.nth['producer']}\n")
            return changes(request, "still wrong")
        _engine, status = self.start(same_finding, unit_id="u3")
        self.assertEqual("stagnation", status["stop_reason"])

    def test_no_execution_record_stops_as_unknown_and_an_unreadable_verdict_is_asked_for_again(self):
        def no_record(request, commands, cancelled):
            write(request.cwd, "out.txt", "made by the producer\n")
            return TurnOutcome(True, "done", None)
        _engine, status = self.start(no_record)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual(("no-execution-record",) * 2, self.stop_details())
        self.assertEqual(1, len(self.agents.requests))

        def vague_reviewer(request, commands, cancelled):
            return produced(request, commands) if request.role == "producer" else TurnOutcome(True, "LGTM", actions())
        engine = self.engine(vague_reviewer)
        engine.handle({"request": "start", "contract": self.contract(unit_id="u2")})
        self.assertEqual("invalid_result", self.run_to_stop(engine, "u2")["stop_reason"])
        self.assertEqual(["producer", "reviewer", "reviewer"], [request.role for request in self.agents.requests])
        self.assertIn("result not accepted", self.agents.requests[2].prompt)

    def test_incomplete_turn_stops_as_unknown_and_retry_runs_it_again(self):
        calls = []

        def script(request, commands, cancelled):
            calls.append(request.role)
            if len(calls) == 1:
                return TurnOutcome(False, None, None, "agent-start-failed: boom")
            return produced(request, commands) if request.role == "producer" else approve(request)
        engine, status = self.start(script)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual(("agent-start-failed: boom",) * 2, self.stop_details())
        self.assertFalse(engine.handle({"request": "answer", "unit_id": "u1", "question_id": "q", "answer": "a"})["ok"])
        self.assertTrue(engine.handle({"request": "retry", "unit_id": "u1"})["ok"])
        self.assertEqual(("done", 0), (lambda s: (s["stop_reason"], s["round"]))(self.run_to_stop(engine)))
        self.assertFalse(engine.handle({"request": "retry", "unit_id": "u1"})["ok"])

    def test_question_waits_for_its_answer_and_the_answer_reaches_the_producer(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            if self.agents.nth["producer"] == 1:
                return TurnOutcome(True, 'need a decision\n{"question": "tabs or spaces?", "id": "q-style"}', actions())
            return produced(request, commands)
        engine, status = self.start(script)
        self.assertEqual("waiting_for_answer", status["stop_reason"])
        self.assertEqual({"id": "q-style", "text": "tabs or spaces?"}, status["question"])
        wrong = engine.handle({"request": "answer", "unit_id": "u1", "question_id": "q-other", "answer": "x"})
        self.assertFalse(wrong["ok"])
        self.assertTrue(engine.handle(
            {"request": "answer", "unit_id": "u1", "question_id": "q-style", "answer": "spaces"})["ok"])
        self.assertEqual("done", self.run_to_stop(engine)["stop_reason"])
        self.assertIn("Answer: spaces", self.agents.requests[1].prompt)

    def test_changed_agent_config_stops_before_any_other_agent_starts(self):
        def script(request, commands, cancelled):
            write(request.cwd, ".claude/settings.json", '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}')
            return produced(request, commands)
        _engine, status = self.start(script)
        self.assertEqual("unsafe_agent_config", status["stop_reason"])
        self.assertEqual(["producer"], [request.role for request in self.agents.requests])

    def test_the_environment_entries_among_agent_config_names_do_not_stop_and_empty_config_files_do(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            if request.unit_id == "u1":  # what an agent's host places: empty directories, empty read-only files
                os.makedirs(os.path.join(request.cwd, ".codex"), exist_ok=True)
                os.makedirs(os.path.join(request.cwd, ".agents"), exist_ok=True)
                for name in (".mcp.json", ".pi"):
                    if not os.path.lexists(os.path.join(request.cwd, name)):
                        write(request.cwd, name, "")
                        os.chmod(os.path.join(request.cwd, name), 0o444)
            else:  # files that define nothing yet are still agent configuration
                write(request.cwd, {"u2": ".codex/config.toml", "u3": ".mcp.json", "u4": ".agents/x"}[request.unit_id], "")
            return produced(request, commands)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        for unit_id in ("u2", "u3", "u4"):
            with self.subTest(unit_id):
                _engine, status = self.start(script, unit_id=unit_id)
                self.assertEqual("unsafe_agent_config", status["stop_reason"])

    def test_reviewer_that_changes_the_candidate_is_not_an_approval(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                return produced(request, commands)
            write(request.cwd, "out.txt", "the reviewer was here\n")
            return approve(request)
        _engine, status = self.start(script)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual(("a-turn-that-must-not-write-changed-the-worktree",) * 2, self.stop_details())

    def test_caches_a_reviewer_leaves_in_ignored_paths_do_not_void_its_judgment(self):
        def script(request, commands, cancelled):
            if request.role == "producer":
                write(request.cwd, ".gitignore", "__pycache__/\n")
                return produced(request, commands)
            outcome = approve(request)
            write(request.cwd, "__pycache__/tests.pyc", "compiled while the reviewer ran the tests\n")
            return outcome
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])

    def test_cancel_interrupts_a_running_turn_and_records_cancelled(self):
        entered = threading.Event()

        def script(request, commands, cancelled):
            entered.set()
            self.assertTrue(cancelled.wait(10))
            return TurnOutcome(False, None, None, "interrupted")
        engine = self.engine(script)
        engine.handle({"request": "start", "contract": self.contract()})
        self.assertTrue(entered.wait(10))
        self.assertTrue(engine.handle({"request": "cancel", "unit_id": "u1"})["ok"])
        status = self.run_to_stop(engine)
        self.assertEqual("cancelled", status["stop_reason"])
        self.assertTrue(engine.handle({"request": "cancel", "unit_id": "u1"})["ok"])
        self.assertEqual(["cancel_requested"], [event["type"] for event in self.events() if "cancel" in event["type"]])
        self.assertEqual("cancelled", self.events()[-1]["stop_reason"])

    def test_cancel_after_the_end_keeps_done_and_ends_a_waiting_unit(self):
        def script(request, commands, cancelled):
            if request.unit_id == "ask":
                return TurnOutcome(True, '{"question": "which?", "id": "q-1"}', actions())
            return produced(request, commands) if request.role == "producer" else approve(request)
        engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual("done", engine.handle({"request": "cancel", "unit_id": "u1"})["result"]["stop_reason"])
        self.assertNotIn("cancel_requested", [event["type"] for event in self.events()])
        engine.handle({"request": "start", "contract": self.contract(unit_id="ask")})
        self.assertEqual("waiting_for_answer", self.run_to_stop(engine, "ask")["stop_reason"])
        self.assertTrue(engine.handle({"request": "cancel", "unit_id": "ask"})["ok"])
        self.assertEqual("cancelled", self.run_to_stop(engine, "ask")["stop_reason"])
        refused = engine.handle({"request": "answer", "unit_id": "ask", "question_id": "q-1", "answer": "a"})
        self.assertFalse(refused["ok"])

    def test_a_halted_engine_leaves_the_unit_running_and_the_next_engine_finishes_it(self):
        entered = threading.Event()

        def hang(request, commands, cancelled):
            write(request.cwd, "partial.txt", "half\n")
            entered.set()
            cancelled.wait(10)
            return TurnOutcome(False, None, None, "interrupted")
        first = self.engine(hang)
        first.handle({"request": "start", "contract": self.contract()})
        self.assertTrue(entered.wait(10))
        first.halt()
        self.assertTrue(self.agents.closed)
        self.assertEqual("running", self.unit("u1").status()["state"])
        self.assertNotIn("turn_finished", [event["type"] for event in self.events()])

        def finish(request, commands, cancelled):
            return produced(request, commands) if request.role == "producer" else approve(request)
        second = self.engine(finish)
        second.resume()
        status = self.run_to_stop(second)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual(1, [event["type"] for event in self.events()].count("unit_started"))
        self.assertIn(b"partial.txt", self.result("patch.diff"))

    def test_a_change_outside_the_scope_goes_back_before_any_review(self):
        contract = self.contract()
        contract["facts"].append({"id": "in-scope", "required": True, "by": "git", "subject": "candidate"})
        contract["executions"][1]["reads"].append("in-scope")

        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            other = os.path.join(request.cwd, "other.txt")
            if self.agents.nth["producer"] == 1:
                write(request.cwd, "other.txt", "not what was asked for\n")
            elif os.path.exists(other):
                os.unlink(other)
            return produced(request, commands)
        _engine, status = self.start(script, facts=contract["facts"], executions=contract["executions"])
        self.assertEqual(("done", 1), (status["stop_reason"], status["round"]))
        self.assertEqual(["producer", "producer", "reviewer"], [request.role for request in self.agents.requests])
        self.assertIn("[in-scope] other.txt changed outside the declared scope", self.agents.requests[1].prompt)

    def test_an_existing_change_is_judged_against_the_base_the_contract_names(self):
        base = git_out(self.repo, "rev-parse", "HEAD")
        write(self.repo, "out.txt", "made before the unit started\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "the change under review")

        def execution(eid, agent):
            return {"id": eid, "agent": agent, "model": None, "effort": None, "instructions": "You review.",
                    "reads": [], "establishes": ["quality"], "writes": False, "independent_of": []}
        contract = self.contract(facts=[
            {"id": "subject", "required": False, "by": "given"},
            {"id": "quality", "required": True, "by": "verdict", "subject": "subject", "block_at": "major"},
        ], executions=[execution("first", "alpha"), execution("second", "beta")])
        contract["unit"]["base"] = base

        def script(request, commands, cancelled):
            return approve(request) if request.role == "first" else changes(request, "wrong wording")
        engine = self.engine(script)
        self.assertTrue(engine.handle({"request": "start", "contract": contract})["ok"])
        status = self.run_to_stop(engine)
        self.assertEqual("rejected", status["stop_reason"])
        self.assertEqual(["first", "second"], [request.role for request in self.agents.requests])
        self.assertIn("- out.txt", self.agents.requests[0].prompt)
        self.assertIn(b"+made before the unit started", self.result("patch.diff"))
        stop = json.loads(self.result("stop.json"))
        self.assertEqual(["wrong wording"], [f["description"] for f in stop["stop"]["findings"]])
        unknown = self.contract(unit_id="u2")
        unknown["unit"]["base"] = "0" * 40
        self.assertIn("unit.base", engine.handle({"request": "start", "contract": unknown})["error"])

    def test_a_unit_that_ended_for_good_leaves_no_worktree_and_one_that_can_continue_keeps_it(self):
        asked = []

        def script(request, commands, cancelled):
            if request.unit_id == "ask" and not asked:
                asked.append(request.role)
                return TurnOutcome(True, '{"question": "which?", "id": "q-1"}', actions())
            return produced(request, commands) if request.role == "producer" else approve(request)
        engine, status = self.start(script)
        self.assertEqual(("done", True), (status["stop_reason"], status["worktree_removed"]))
        self.assertFalse(os.path.exists(status["worktree"]))
        self.assertFalse(os.path.exists(status["worktree"] + ".base"))
        self.assertIn(b"+made by the producer", self.result("patch.diff"))
        self.assertNotIn(os.path.basename(status["worktree"]), git_out(self.repo, "worktree", "list"))
        engine.handle({"request": "start", "contract": self.contract(unit_id="ask")})
        waiting = self.run_to_stop(engine, "ask")
        self.assertEqual(("waiting_for_answer", False), (waiting["stop_reason"], waiting["worktree_removed"]))
        self.assertTrue(os.path.isdir(waiting["worktree"]))
        engine.handle({"request": "answer", "unit_id": "ask", "question_id": "q-1", "answer": "the first"})
        self.assertEqual(("done", True), (lambda s: (s["stop_reason"], s["worktree_removed"]))(self.run_to_stop(engine, "ask")))

    def test_unit_without_checks_goes_straight_to_review(self):
        def script(request, commands, cancelled):
            self.assertEqual({}, commands)
            if request.role == "producer":
                write(request.cwd, "notes.md", "findings of the investigation\n")
                return TurnOutcome(True, "investigated", None)
            return approve(request)
        _engine, status = self.start(script, checks=False)
        self.assertEqual("done", status["stop_reason"])


if __name__ == "__main__":
    unittest.main()
