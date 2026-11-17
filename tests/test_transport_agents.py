"""The agent port over the transport's provider sessions, with a scripted protocol driver.

`AgentTurnsContract` is what the engine relies on from its agent port. It runs
on the transport adapter and on the scripted agents the other engine tests use.
"""
import os
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from engine_support import RUNNER, SRC, EngineCase, ScriptedAgents, approve, produced

TRANSPORT = os.path.join(os.path.dirname(os.path.dirname(SRC)), "packages", "cross-host-transport", "src")
if TRANSPORT not in sys.path:
    sys.path.insert(0, TRANSPORT)

from cross_host_transport.subsession import provider_catalog, provider_drivers  # noqa: E402
from cross_host_transport.subsession.provider_port import DriverTraits, SessionHandle, TurnEnd  # noqa: E402
from cross_host_transport.subsession.turn_result import TurnCollector, TurnProgress  # noqa: E402

from loop_engine import composition  # noqa: E402
from loop_engine.agents import TurnOutcome, TurnRequest  # noqa: E402
from loop_engine.git import Git  # noqa: E402
from loop_engine.platform_facts import CURRENT  # noqa: E402
from loop_engine.service import Engine  # noqa: E402

ACTIONS = {"version": 1, "dropped": 0, "entries": [], "refused": [
    {"kind": "command", "description": "rm -rf build"}, {"kind": "input", "description": "pick one"}],
    "refused_dropped": 0}


class Host:
    closed = False

    def __init__(self, log):
        self.log = log

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True
        self.log.append("host-closed")


class Driver:
    """A protocol driver that records each turn and ends it as the test scripted.

    It keeps the sessions it opened, as a provider does, and binds a durable
    session only when it has it.
    """

    transport = "scripted"
    traits = DriverTraits(shared_host=True, startup_timeout=5.0, startup_grace=0.0, unload_timeout=1.0)

    def __init__(self):
        self.turns, self.mode, self.cancelled, self.hosts = [], "complete", threading.Event(), []
        self.sessions, self.released, self.cancels = set(), [], 0
        self.log = []  # releases, host closes and process termination, in order
        self.release_begun = threading.Event()
        self.release_gate = None  # when set to an event, a release waits for it
        self.release_delay = 0.0
        self.leave = None  # a file a turn puts in its working directory, which its release removes
        self.leaves = lambda turn: True  # which turns put it there
        self.keep_left = False  # the release leaves that file in place
        self.respond = None  # turn -> (text, actions), instead of the fixed approval
        self._cwds = {}

    def host_key(self, request):
        return "host"

    def run_turn(self, turn, slot, started):
        if self.mode == "bind-error":
            raise RuntimeError("launcher exited 1")
        if slot.host is None:
            self.hosts.append(Host(self.log))
            slot.adopt(self.hosts[-1])
        if turn.durable_id is not None and turn.durable_id not in self.sessions:
            raise RuntimeError("no session with this id")
        session = turn.durable_id or f"session-{len(self.sessions) + 1}"
        self.sessions.add(session)
        self.turns.append(turn)
        self._cwds[session] = turn.cwd
        if self.leave is not None and self.leaves(turn):
            with open(os.path.join(turn.cwd, self.leave), "w", encoding="utf-8") as handle:
                handle.write("held by the agent's sandbox\n")
        started(SessionHandle(session, session, None, None))
        collector = TurnCollector(f"{session}:turn")
        if self.mode == "hold":
            self.cancelled.wait(10)
            self.cancelled.clear()
            return TurnEnd(False, "cancelled", collector.seal_incomplete("turn_not_completed").to_wire())
        text, actions = self.respond(turn) if self.respond else ('done\n{"verdict": "approve", "findings": []}', ACTIONS)
        collector.observe_text(text)
        return TurnEnd(True, "completed", collector.seal_completed().to_wire(), detail={"actions": actions})

    def progress(self, session):
        return TurnProgress("", None)

    def cancel(self, session):
        self.cancels += 1
        self.cancelled.set()

    def release(self, session, timeout):
        self.release_begun.set()
        if self.release_delay:
            threading.Event().wait(self.release_delay)
        if self.release_gate is not None and not self.release_gate.wait(10):
            raise TimeoutError("release-timeout")
        left = os.path.join(self._cwds.get(session.session_id, ""), self.leave or "")
        if self.leave is not None and not self.keep_left and os.path.exists(left):
            os.remove(left)
        self.released.append(session.session_id)
        self.log.append("released")


class AgentTurnsContract:
    """What the engine relies on from an `AgentTurns`: sessions, continuation, loss and cancel."""

    def open_agents(self):
        raise NotImplementedError

    def close_agents(self, agents):
        agents.close()

    def hold_next_turn(self):
        """Keep the next turn running until it is cancelled."""
        raise NotImplementedError

    def forget(self, session):
        """Make the provider lose the session."""
        raise NotImplementedError

    def turn(self, agents, session=None, role="producer", cancelled=None):
        return agents.run(TurnRequest("u1", role, 1, "codex", "model-a", "high", self.cwd, "do the work", session),
                          cancelled or threading.Event())

    def test_a_turn_returns_its_session_and_a_continued_turn_runs_in_the_same_one(self):
        agents = self.open_agents()
        first = self.turn(agents)
        self.assertTrue(first.completed)
        self.assertIsInstance(first.session, str)
        again = self.turn(agents, first.session)
        self.assertTrue(again.completed)
        self.assertEqual(first.session, again.session)
        self.assertFalse(again.session_lost)
        self.close_agents(agents)

    def test_two_new_turns_never_share_a_session(self):
        agents = self.open_agents()
        tokens = {self.turn(agents, role=role).session for role in ("producer", "reviewer", "producer")}
        self.assertEqual(3, len(tokens))
        self.close_agents(agents)

    def test_a_session_the_provider_lost_is_reported_and_no_turn_runs(self):
        agents = self.open_agents()
        first = self.turn(agents)
        self.forget(first.session)
        before = self.turns_run()
        lost = self.turn(agents, first.session)
        self.assertEqual((False, True, None), (lost.completed, lost.session_lost, lost.text))
        self.assertEqual(before, self.turns_run())
        self.close_agents(agents)

    def test_cancel_interrupts_a_continued_turn(self):
        agents = self.open_agents()
        first = self.turn(agents)
        self.hold_next_turn()
        cancelled = threading.Event()
        threading.Timer(0.3, cancelled.set).start()
        began = time.monotonic()
        held = self.turn(agents, first.session, cancelled=cancelled)
        self.assertLess(time.monotonic() - began, 5, "the cancel did not reach the turn")
        self.assertFalse(held.completed)
        self.assertFalse(held.session_lost)
        self.assertEqual(first.session, held.session)
        self.close_agents(agents)


class ScriptedAgentsContractTest(AgentTurnsContract, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cwd = self._tmp.name
        self.hold = False
        self.count = 0

    def script(self, request, commands, cancelled):
        self.count += 1
        if self.hold:
            self.hold = False
            cancelled.wait(10)
            return TurnOutcome(False, None, None, "cancelled")
        return TurnOutcome(True, "done", ACTIONS)

    def open_agents(self):
        self.agents = ScriptedAgents(self._tmp.name, self._tmp.name, self.script)
        self.agents.run = self._run_without_unit(self.agents.run)
        return self.agents

    def _run_without_unit(self, run):
        # The scripted agents read the unit's checks; this contract runs without a unit.
        def without(request, cancelled):
            with mock.patch("engine_support.store.UnitStore") as unit, \
                    mock.patch("engine_support.facts.check_names", return_value=([], [])):
                unit.return_value.published.return_value = "checks.json"
                return run(request, cancelled)
        return without

    def hold_next_turn(self):
        self.hold = True

    def forget(self, session):
        self.agents.lost.add(session)

    def turns_run(self):
        return self.count


class TransportAgentsTest(AgentTurnsContract, unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = os.path.realpath(self._tmp.name)
        self.origin, self.cwd = os.path.join(base, "worktrees"), os.path.join(base, "worktrees", "ns", "u1")
        os.makedirs(self.cwd)
        self.driver = Driver()
        self.requested = []

        def driver_for(spec, *, origin, state_dir):
            self.requested.append((spec, str(origin)))
            return self.driver
        for target, value in ((provider_drivers, "driver_for"), ):
            patcher = mock.patch.object(target, value, driver_for)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agents = composition.TransportAgents(origin=self.origin, state_dir=os.path.join(base, "state"))
        self.addCleanup(self.agents.close)

    def open_agents(self):
        return self.agents

    def close_agents(self, agents):
        pass  # closed by the cleanup

    def hold_next_turn(self):
        self.driver.mode = "hold"

    def forget(self, session):
        self.driver.sessions.discard(session)

    def turns_run(self):
        return len(self.driver.turns)

    def request(self, agent="codex"):
        return TurnRequest("u1", "producer", 1, agent, "model-a", "high", self.cwd, "do the work")

    def test_no_setting_lets_the_engine_or_the_transport_grant_an_agent_request(self):
        import inspect
        self.assertNotIn("grant", inspect.getsource(provider_drivers))  # the module's own source, not the patch
        self.assertEqual(["origin", "state_dir"], list(inspect.signature(composition.TransportAgents).parameters))
        # No environment variable reaches the agent port: only `build` reads the environment, for the
        # state home, the worktrees root and git.
        self.assertNotIn("environ", inspect.getsource(composition.TransportAgents))
        with open(composition.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("os.environ", source)
        self.assertNotIn("getenv", source)

    def test_catalog_decides_which_agents_exist(self):
        self.assertEqual(set(provider_catalog.provider_names()), set(self.agents.names))
        for agent in ("claude", "codex", "pi"):
            self.assertTrue(self.agents.known(agent), agent)
        self.assertFalse(self.agents.known("sh"))

    def test_turn_runs_in_the_unit_worktree_and_returns_text_and_actions(self):
        outcome = self.agents.run(self.request(), threading.Event())
        self.assertTrue(outcome.completed)
        self.assertTrue(outcome.text.endswith('{"verdict": "approve", "findings": []}'))
        self.assertEqual(ACTIONS, outcome.actions)
        self.assertEqual(2, outcome.refused)
        turn = self.driver.turns[0]
        self.assertEqual((self.cwd, "do the work", "model-a", "high", "codex"), (
            turn.cwd, turn.request.prompt, turn.request.model, turn.request.effort, turn.request.provider))
        self.assertEqual(self.origin, self.requested[0][1])
        self.assertEqual("codex", self.requested[0][0].key)

    def test_a_continued_turn_sends_its_prompt_to_the_same_durable_session(self):
        first = self.agents.run(self.request(), threading.Event())
        again = self.agents.run(TurnRequest("u1", "producer", 3, "codex", "model-a", "high", self.cwd,
                                            "only the findings", first.session), threading.Event())
        self.assertEqual([None, first.session], [turn.durable_id for turn in self.driver.turns])
        self.assertEqual("only the findings", self.driver.turns[1].request.prompt)
        self.assertEqual(first.session, again.session)

    def test_a_session_survives_a_new_adapter_as_after_an_engine_restart(self):
        first = self.agents.run(self.request(), threading.Event())
        self.agents.close()
        later = composition.TransportAgents(origin=self.origin, state_dir=os.path.join(self.origin, "..", "state"))
        self.addCleanup(later.close)
        again = later.run(TurnRequest("u1", "producer", 3, "codex", None, None, self.cwd, "go on", first.session),
                          threading.Event())
        self.assertTrue(again.completed)
        self.assertEqual(first.session, self.driver.turns[-1].durable_id)
        self.assertEqual(first.session, again.session)

    def test_ending_a_session_forgets_it_in_this_process(self):
        first = self.agents.run(self.request(), threading.Event())
        self.agents.end_session("codex", first.session)
        self.assertEqual({}, self.agents._held)
        self.agents.end_session("codex", "never-opened")  # nothing to close

    def test_one_session_set_per_agent_serves_later_turns(self):
        self.agents.run(self.request(), threading.Event())
        self.agents.run(self.request(), threading.Event())
        self.assertEqual(1, len(self.requested))
        self.assertEqual(2, len(self.driver.turns))
        self.assertEqual(1, len(self.driver.hosts))

    def test_a_turn_that_cannot_start_is_incomplete_not_an_error(self):
        self.driver.mode = "bind-error"
        outcome = self.agents.run(self.request(), threading.Event())
        self.assertEqual((False, None, None), (outcome.completed, outcome.text, outcome.actions))
        self.assertIn("agent-start-failed", outcome.detail)
        self.assertIn("launcher exited 1", outcome.detail)

    def test_cancel_stops_the_running_turn(self):
        self.driver.mode = "hold"
        cancelled = threading.Event()
        threading.Timer(0.3, cancelled.set).start()
        outcome = self.agents.run(self.request(), cancelled)
        self.assertFalse(outcome.completed)
        self.assertEqual(1, self.driver.cancels)

    def test_a_directory_outside_the_worktree_root_is_refused(self):
        outside = TurnRequest("u1", "producer", 1, "codex", None, None, os.path.dirname(self.origin), "x")
        outcome = self.agents.run(outside, threading.Event())
        self.assertFalse(outcome.completed)
        self.assertEqual([], self.driver.turns)

    def run_in_thread(self, request):
        """Run a turn on its own thread; the list gets the outcome, and 'returned' in the driver's log."""
        answers = []

        def run():
            answers.append(self.agents.run(request, threading.Event()))
            self.driver.log.append("returned")
        thread = threading.Thread(target=run)
        thread.start()
        return thread, answers

    def test_a_turn_returns_only_after_its_session_is_released(self):
        first = self.agents.run(self.request(), threading.Event())
        for session in (None, first.session):  # a new session, then a continued one
            self.driver.log.clear()
            self.driver.release_begun.clear()
            self.driver.release_gate = threading.Event()
            request = TurnRequest("u1", "producer", 2, "codex", "model-a", "high", self.cwd, "go", session)
            thread, answers = self.run_in_thread(request)
            self.assertTrue(self.driver.release_begun.wait(5))
            thread.join(0.2)
            self.assertEqual([], answers, "the turn returned while its session was being released")
            self.driver.release_gate.set()
            thread.join(5)
            self.assertEqual(["released", "returned"], self.driver.log)
            self.assertTrue(answers[0].completed)
            self.assertTrue(answers[0].released)

    def test_a_cancelled_turn_also_returns_after_its_release(self):
        self.driver.mode = "hold"
        self.driver.release_delay = 0.2
        cancelled = threading.Event()
        threading.Timer(0.2, cancelled.set).start()
        outcome = self.agents.run(self.request(), cancelled)
        self.driver.log.append("returned")
        self.assertEqual(["released", "returned"], self.driver.log)
        self.assertFalse(outcome.completed)
        self.assertTrue(outcome.released)

    def test_a_release_that_does_not_complete_is_a_distinct_outcome_that_is_not_completed(self):
        self.driver.traits = DriverTraits(shared_host=True, startup_timeout=5.0, startup_grace=0.0, unload_timeout=0.2)
        first = self.agents.run(self.request(), threading.Event())
        self.driver.release_gate = threading.Event()
        self.addCleanup(self.driver.release_gate.set)
        for session in (None, first.session):
            outcome = self.agents.run(TurnRequest("u1", "producer", 2, "codex", "model-a", "high", self.cwd, "go",
                                                  session), threading.Event())
            self.assertEqual((False, False, False), (outcome.completed, outcome.released, outcome.session_lost))
            self.assertEqual("agent-session-not-released: unload-not-finished", outcome.detail)
            self.assertIsInstance(outcome.session, str)
            self.driver.release_gate.set()
            self.driver.release_gate = threading.Event()
        self.driver.release_gate = None
        failing = mock.patch.object(self.driver, "release", side_effect=OSError("unsubscribe failed"))
        with failing:
            outcome = self.agents.run(self.request(), threading.Event())
        self.assertFalse(outcome.released)
        self.assertEqual("agent-session-not-released: OSError:unsubscribe failed", outcome.detail)

    def test_a_turn_continues_its_session_after_a_completed_release(self):
        self.driver.release_delay = 0.1
        first = self.agents.run(self.request(), threading.Event())
        self.assertEqual(["released"], self.driver.log)
        again = self.agents.run(TurnRequest("u1", "producer", 2, "codex", "model-a", "high", self.cwd, "go on",
                                            first.session), threading.Event())
        self.assertTrue(again.completed)
        self.assertEqual(first.session, again.session)
        self.assertEqual([None, first.session], [turn.durable_id for turn in self.driver.turns])
        self.assertEqual(1, len(self.driver.hosts))  # continued on the host that is still running

    def test_closing_releases_the_sessions_before_it_ends_any_process(self):
        self.driver.mode = "hold"
        self.driver.release_delay = 0.2
        self.agents.ledger.terminate_owned = lambda: self.driver.log.append("terminated")
        thread, answers = self.run_in_thread(self.request())
        while not self.driver.turns:
            thread.join(0.01)
        self.agents.close()
        thread.join(5)
        closing = [entry for entry in self.driver.log if entry != "returned"]
        self.assertEqual(["released", "host-closed", "terminated"], closing)

    def test_terminal_view_without_a_complete_message_is_incomplete(self):
        complete = {"outcome": "completed", "actions": ACTIONS, "turn_result": {
            "communication": {"text": "hi", "presence": "present"},
            "transport_evidence": {"completion": "completed"}}}
        self.assertTrue(composition.turn_outcome(complete).completed)
        for change in ({"outcome": "post_start_failed"}, {"turn_result": None},
                       {"turn_result": {"communication": {"text": "hi", "presence": "present"},
                                        "transport_evidence": {"completion": "incomplete"}}},
                       {"turn_result": {"communication": {"text": "", "presence": "absent"},
                                        "transport_evidence": {"completion": "completed"}}}):
            outcome = composition.turn_outcome({**complete, **change})
            self.assertFalse(outcome.completed, change)
            self.assertTrue(outcome.detail)


class WorktreeAfterReleaseTest(EngineCase):
    """The engine judges the worktree after the agent's session is released, and judges all of it."""

    def run_unit(self, keep_left):
        driver = Driver()
        driver.leave, driver.keep_left, driver.release_delay = "held-by-the-sandbox", keep_left, 0.3
        reviewer = lambda turn: turn.request.effort == "high"  # noqa: E731
        driver.leaves = reviewer

        def respond(turn):
            request = SimpleNamespace(cwd=turn.cwd)
            outcome = approve(request) if reviewer(turn) else produced(request, {})
            return outcome.text, outcome.actions
        driver.respond = respond
        patcher = mock.patch.object(provider_drivers, "driver_for", lambda spec, *, origin, state_dir: driver)
        patcher.start()
        self.addCleanup(patcher.stop)
        worktrees = os.path.join(self.home, "worktrees")
        agents = composition.TransportAgents(origin=worktrees, state_dir=os.path.join(self.home, "agent-state"))
        engine = Engine(self.root, published=self.published, git=Git(os.environ), agents=agents,
                        templates=composition.load_templates(),
                        runner=(sys.executable, RUNNER), worktrees=worktrees, platform=CURRENT,
                        clock=lambda: "2026-01-01T00:00:00+00:00")
        self.addCleanup(engine.halt)
        contract = self.with_agent(self.contract(checks=False), producer="codex", reviewer="codex")
        self.assertTrue(engine.handle({"request": "start", "contract": contract})["ok"])
        return self.run_to_stop(engine)

    def finished(self):
        return [(event["execution"], event["completed"], event["detail"])
                for event in self.events() if event["type"] == "turn_finished"]

    def test_a_file_the_sandbox_holds_only_until_the_release_is_not_a_change_of_a_read_only_turn(self):
        status = self.run_unit(keep_left=False)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual([("producer", True, None), ("reviewer", True, None)], self.finished())

    def test_a_file_still_there_after_the_release_is_a_change_of_a_read_only_turn(self):
        status = self.run_unit(keep_left=True)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertEqual(("reviewer", False, "a-turn-that-must-not-write-changed-the-worktree"), self.finished()[-1])


if __name__ == "__main__":
    unittest.main()
