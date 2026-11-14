"""The agent port over the transport's provider sessions, with a scripted protocol driver."""
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

from engine_support import SRC

TRANSPORT = os.path.join(os.path.dirname(os.path.dirname(SRC)), "cross-host-transport", "src")
if TRANSPORT not in sys.path:
    sys.path.insert(0, TRANSPORT)

from cross_host_transport.subsession import provider_catalog, provider_drivers  # noqa: E402
from cross_host_transport.subsession.core import CapabilityProbe, CapabilityStatus  # noqa: E402
from cross_host_transport.subsession.provider_port import DriverTraits, SessionHandle, TurnEnd  # noqa: E402
from cross_host_transport.subsession.turn_result import TurnCollector, TurnProgress  # noqa: E402

from loop_engine import composition  # noqa: E402
from loop_engine.agents import TurnRequest  # noqa: E402

ACTIONS = {"version": 1, "dropped": 0, "entries": []}


class Host:
    closed = False

    def alive(self):
        return not self.closed

    def close(self):
        self.closed = True


class Driver:
    """A protocol driver that records each turn and ends it as the test scripted."""

    transport = "scripted"
    enforces_writable_roots = False
    traits = DriverTraits(shared_host=True, startup_timeout=5.0, startup_grace=0.0, unload_timeout=1.0)

    def __init__(self):
        self.turns, self.mode, self.cancelled, self.hosts = [], "complete", threading.Event(), []

    def probe(self, request):
        return CapabilityProbe("scripted", "g", CapabilityStatus.CONFIRMED, frozenset(), "scripted")

    def host_key(self, request):
        return "host"

    def run_turn(self, turn, slot, started):
        if self.mode == "bind-error":
            raise RuntimeError("launcher exited 1")
        if slot.host is None:
            self.hosts.append(Host())
            slot.adopt(self.hosts[-1])
        self.turns.append(turn)
        started(SessionHandle("session-1", "session-1", None, None))
        collector = TurnCollector("session-1:turn")
        if self.mode == "hold":
            self.cancelled.wait(10)
            return TurnEnd(False, "cancelled", collector.seal_incomplete("turn_not_completed").to_wire())
        collector.observe_text('done\n{"verdict": "approve", "findings": []}')
        return TurnEnd(True, "completed", collector.seal_completed().to_wire(), detail={"actions": ACTIONS})

    def progress(self, session):
        return TurnProgress("", None)

    def cancel(self, session):
        self.cancelled.set()

    def release(self, session, timeout):
        pass


class TransportAgentsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = os.path.realpath(self._tmp.name)
        self.origin, self.cwd = os.path.join(base, "worktrees"), os.path.join(base, "worktrees", "ns", "u1")
        os.makedirs(self.cwd)
        self.driver = Driver()
        self.requested = []

        def driver_for(spec, *, origin, grants, state_dir):
            self.requested.append((spec, str(origin), dict(grants)))
            return self.driver
        for target, value in ((provider_drivers, "driver_for"), ):
            patcher = mock.patch.object(target, value, driver_for)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.agents = composition.TransportAgents(
            origin=self.origin, state_dir=os.path.join(base, "state"),
            grants={"filesystem_read": False, "filesystem_write": False, "terminal": False})
        self.addCleanup(self.agents.close)

    def request(self, agent="codex"):
        return TurnRequest("u1", "producer", 1, agent, "model-a", "high", self.cwd, "do the work")

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
        turn = self.driver.turns[0]
        self.assertEqual((self.cwd, "do the work", "model-a", "high", "codex"), (
            turn.cwd, turn.request.prompt, turn.request.model, turn.request.effort, turn.request.provider))
        self.assertIsNone(turn.request.writable_roots)
        self.assertEqual(self.origin, self.requested[0][1])
        self.assertEqual("codex", self.requested[0][0].key)

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
        self.assertTrue(self.driver.cancelled.is_set())

    def test_a_directory_outside_the_worktree_root_is_refused(self):
        outside = TurnRequest("u1", "producer", 1, "codex", None, None, os.path.dirname(self.origin), "x")
        outcome = self.agents.run(outside, threading.Event())
        self.assertFalse(outcome.completed)
        self.assertEqual([], self.driver.turns)

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


if __name__ == "__main__":
    unittest.main()
