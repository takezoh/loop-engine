"""One provider lifecycle runs turns, sessions and hosts for every protocol through its driver port."""
from types import SimpleNamespace
import time
import unittest

from provider_transport.provider_request import ActionRequest
from provider_transport.provider_port import (
    ProviderCleanupFailure,
    ProviderStartupTimeout,
    TurnEnd,
)
from provider_transport.provider_sessions import ProviderSessions
from .protocol_driver_fake import FakeAdmission, FakeDriver


def start_request(operation="op-start", model="m"):
    return ActionRequest("start", operation, provider="fake", model=model,
                         cwd="/work", prompt="do it")


def continue_request(execution, operation="op-continue", model="m"):
    return ActionRequest("continue", operation, provider="fake", model=model, cwd="/work", prompt="again")


def request(action, execution, operation="op-act"):
    return ActionRequest(action, operation)


def snapshot(execution):
    return SimpleNamespace(execution_id=execution)


def eventually(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        self.evidence = []
        self.sessions.set_terminal_observer(lambda *args: self.evidence.append(args))

    def start(self, execution="exec-1", operation="op-start", **script):
        self.driver.script(**script)
        self.sessions.bind_pending_execution(execution, operation)
        admission = FakeAdmission()
        receipt = self.sessions.start(start_request(operation), admission)
        if receipt.get("session_id") is not None:
            self.sessions.bind_execution(execution, receipt["session_id"])
        return receipt, admission

    def observe(self, execution="exec-1"):
        return self.sessions.action(request("observe", execution), snapshot(execution))


class StartTests(Lifecycle):
    def test_a_start_returns_a_running_receipt_and_its_end_becomes_evidence_and_an_unload(self):
        receipt, admission = self.start(hold=True)
        self.assertEqual(("running", "session-1", "session-1"),
                         (receipt["outcome"], receipt["session_id"], receipt["provider_thread_id"]))
        self.assertEqual(1, admission.spawned)
        self.assertEqual("op-start", self.sessions.observed_turn("exec-1"))
        self.assertTrue(self.sessions.turn_running("exec-1"))
        running = self.observe()
        self.assertEqual(("preview:session-1", "2026-11-13T16:24:27+00:00"), (running["output"], running["activity_at"]))
        script = self.sessions._turn("exec-1").session.state
        script.release.set()
        self.assertTrue(self.sessions.wait_turn("exec-1", 2))
        self.assertEqual("completed", self.observe()["outcome"])
        self.assertEqual("exec-1", self.evidence[-1][0])
        self.assertEqual(("completed", "op-start"),
                         (self.evidence[-1][2]["outcome"], self.evidence[-1][2]["turn_operation"]))
        self.assertTrue(eventually(lambda: self.driver.released == ["session-1"]))

    def test_a_turn_that_ends_within_the_startup_grace_answers_at_once(self):
        self.driver = FakeDriver(startup_grace=1.0)
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        receipt, _ = self.start(end=TurnEnd(False, "incomplete", {"turn_identity": "x"}))
        self.assertEqual(("post_start_failed", "incomplete"), (receipt["outcome"], receipt["provider_status"]))

    def test_a_session_that_fails_to_bind_raises_and_closes_the_host_it_launched(self):
        self.driver.script(bind_error=OSError("initialize failed"))
        with self.assertRaisesRegex(OSError, "initialize failed"):
            self.sessions.start(start_request(), FakeAdmission())
        self.assertTrue(self.driver.hosts[0].closed)
        self.assertIsNone(self.sessions.observed_turn("exec-1"))

    def test_a_session_that_never_binds_times_out_and_an_unproven_worker_is_quarantined(self):
        self.driver = FakeDriver(startup_timeout=0.05)
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        self.sessions.TERMINATION_JOIN_TIMEOUT = 0.05
        script = self.driver.script(never_bind=True)
        self.sessions.bind_pending_execution("exec-1", "op-start")
        with self.assertRaises(ProviderCleanupFailure) as caught:
            self.sessions.start(start_request(), FakeAdmission())
        self.assertIsInstance(caught.exception.primary, ProviderStartupTimeout)
        self.assertIn("turn-worker-timeout", caught.exception.cleanup_diagnostics)
        self.assertTrue(self.driver.hosts[0].closed)
        # The unproven worker stays owned: closing it still cannot prove its end.
        self.assertEqual("cleanup_failed", self.sessions.action(request("close", "exec-1"), snapshot("exec-1"))["outcome"])
        script.cancelled.set()


class HostTests(Lifecycle):
    def test_a_shared_host_serves_the_next_session_without_its_admission(self):
        self.start(hold=True)
        second, admission = self.start("exec-2", "op-2", hold=True)
        self.assertEqual(0, admission.spawned)
        self.assertEqual(1, len(self.driver.hosts))
        self.assertTrue(self.sessions.uses_shared_host(second["session_id"]))

    def test_a_driver_without_shared_hosts_launches_one_per_turn_and_ends_it_with_the_session(self):
        self.driver = FakeDriver(shared_host=False)
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        receipt, admission = self.start()
        self.assertEqual(1, admission.spawned)
        self.assertFalse(self.sessions.uses_shared_host(receipt["session_id"]))
        self.assertTrue(eventually(lambda: self.driver.hosts[0].closed))
        self.assertTrue(self.sessions.continuation_needs_spawn("exec-1"))

    def test_a_dead_shared_host_leaves_its_sessions_to_reload_in_a_new_host(self):
        self.start()
        self.assertTrue(eventually(lambda: self.driver.released == ["session-1"]))
        self.driver.hosts[0].dead = True
        admission = FakeAdmission()
        receipt = self.sessions.action(continue_request("exec-1"), snapshot("exec-1"), admission)
        self.assertEqual(1, admission.spawned)
        self.assertEqual(2, len(self.driver.hosts))
        self.assertEqual([("session-1", None), ("session-1", "session-1")], self.driver.turns)
        self.assertIn(receipt["outcome"], {"running", "completed"})


class ContinueTests(Lifecycle):
    def test_a_continue_waits_for_the_unload_then_reloads_the_durable_session_on_the_live_host(self):
        self.start()
        self.assertTrue(eventually(lambda: self.driver.released == ["session-1"]))
        self.assertTrue(self.sessions.continuation_needs_spawn("exec-1"))
        admission = FakeAdmission()
        self.driver.script(hold=True)
        receipt = self.sessions.action(continue_request("exec-1"), snapshot("exec-1"), admission)
        self.assertEqual(("running", "session-1"), (receipt["outcome"], receipt["session_id"]))
        self.assertEqual(0, admission.spawned)
        self.assertTrue(self.sessions.uses_shared_host("session-1"))
        self.assertEqual(("session-1", "session-1"), self.driver.turns[-1])

    def test_a_continue_refuses_while_the_unload_is_pending_or_failed(self):
        self.driver = FakeDriver(unload_timeout=0.05)
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        from threading import Event
        self.driver.release_gate = Event()
        self.start()
        self.assertEqual("unloading", self.sessions.action(
            continue_request("exec-1"), snapshot("exec-1"), FakeAdmission())["provider_status"])
        self.driver.release_gate.set()
        self.driver.release_gate = None
        self.driver.release_error = OSError("unsubscribe failed")
        self.sessions.start(start_request("op-2"), FakeAdmission())
        self.sessions.bind_execution("exec-2", "session-2")
        self.assertTrue(eventually(lambda: "session-2" in self.sessions._unload_errors))
        refusal = self.sessions.action(continue_request("exec-2"), snapshot("exec-2"), FakeAdmission())
        self.assertEqual(("recovery_pending", "OSError:unsubscribe failed"), (refusal["outcome"], refusal["cleanup"]))

    def test_an_unbound_or_unadmitted_continue_is_refused(self):
        self.assertEqual({"outcome": "session_lost"},
                         self.sessions.action(continue_request("exec-9"), snapshot("exec-9"), FakeAdmission()))
        self.start()
        self.assertTrue(eventually(lambda: self.driver.released == ["session-1"]))
        self.assertEqual({"outcome": "recovery_pending", "session_id": "session-1"},
                         self.sessions.action(continue_request("exec-1"), snapshot("exec-1"), None))

    def test_a_recovered_binding_continues_the_durable_session_of_an_earlier_host(self):
        self.assertTrue(self.sessions.recover_binding("exec-old", "thread-old"))
        admission = FakeAdmission()
        receipt = self.sessions.action(continue_request("exec-old"), snapshot("exec-old"), admission)
        self.assertEqual("thread-old", receipt["session_id"])
        self.assertEqual(("thread-old", "thread-old"), self.driver.turns[-1])
        self.assertEqual(1, admission.spawned)

    def test_an_execution_bound_after_its_turn_ended_still_records_evidence(self):
        self.driver.script()
        self.sessions.start(start_request(), FakeAdmission())
        self.assertTrue(eventually(lambda: self.driver.released == ["session-1"]))
        self.assertEqual([], self.evidence)
        self.sessions.bind_execution("exec-1", "session-1")
        self.assertEqual("exec-1", self.evidence[-1][0])


class ReleaseTests(Lifecycle):
    def test_wait_release_returns_only_after_the_driver_released_the_session(self):
        from threading import Event, Thread
        self.driver.release_gate = Event()
        self.start()
        self.assertTrue(self.sessions.wait_turn("exec-1", 2))
        answers = []
        waiter = Thread(target=lambda: answers.append(self.sessions.wait_release("exec-1")))
        waiter.start()
        self.assertTrue(eventually(lambda: "session-1" in self.sessions._unloads))
        waiter.join(0.2)
        self.assertEqual([], answers, "wait_release answered while the release was still running")
        self.driver.release_gate.set()
        waiter.join(2)
        self.assertEqual([{"session_id": "session-1", "unload_status": "completed"}], answers)
        self.assertEqual(["session-1"], self.driver.released)

    def test_a_release_that_does_not_end_within_the_bound_or_fails_is_not_completed(self):
        from threading import Event
        self.driver = FakeDriver(unload_timeout=0.05)
        self.sessions = ProviderSessions(self.driver, cell_id="cell")
        self.driver.release_gate = Event()
        self.start()
        self.assertTrue(self.sessions.wait_turn("exec-1", 2))
        self.assertEqual({"session_id": "session-1", "unload_status": "pending", "cleanup": "unload-not-finished"},
                         self.sessions.wait_release("exec-1"))
        self.driver.release_gate.set()
        self.driver.release_gate = None
        self.driver.release_error = OSError("unsubscribe failed")
        self.start("exec-2", "op-2")
        self.assertTrue(self.sessions.wait_turn("exec-2", 2))
        self.assertEqual({"session_id": "session-2", "unload_status": "failed", "cleanup": "OSError:unsubscribe failed"},
                         self.sessions.wait_release("exec-2"))

    def test_a_running_turn_holds_its_session(self):
        self.start(hold=True)
        self.assertEqual({"session_id": "session-1", "unload_status": "pending", "cleanup": "turn-running"},
                         self.sessions.wait_release("exec-1"))
        self.assertEqual([], self.driver.released)
        self.sessions._turn("exec-1").session.state.release.set()

    def test_shutdown_releases_every_session_before_it_closes_a_host(self):
        from threading import Event, Thread
        self.driver.release_gate = Event()
        self.start()
        self.start("exec-2", "op-2", hold=True)
        stopper = Thread(target=self.sessions.shutdown)
        stopper.start()
        stopper.join(0.2)
        self.assertTrue(stopper.is_alive(), "shutdown did not wait for the releases")
        self.assertFalse(any(host.closed for host in self.driver.hosts))
        self.driver.release_gate.set()
        stopper.join(5)
        self.assertFalse(stopper.is_alive())
        self.assertEqual([("released", "session-1"), ("released", "session-2"), ("closed", 1)],
                         sorted(self.driver.log[:2]) + self.driver.log[2:])


class StopTests(Lifecycle):
    def test_a_cancel_ends_the_running_turn(self):
        self.start(hold=True)
        self.assertEqual("cancel_reserved", self.sessions.action(request("cancel", "exec-1"), snapshot("exec-1"))["outcome"])
        self.assertTrue(self.sessions.wait_turn("exec-1", 2))
        self.assertEqual(("post_start_failed", "cancelled"),
                         (self.observe()["outcome"], self.observe()["provider_status"]))

    def test_a_close_stops_the_turn_and_reports_the_unload(self):
        self.start(hold=True)
        closed = self.sessions.action(request("close", "exec-1"), snapshot("exec-1"))
        self.assertEqual(("completed", "session-1"), (closed["outcome"], closed["session_id"]))
        self.assertIn(closed["unload_status"], {"pending", "completed"})
        self.assertIsNone(self.sessions.observed_turn("exec-1"))
        self.assertEqual("session_lost", self.observe()["outcome"])

    def test_a_turn_whose_stop_is_unproven_is_quarantined(self):
        self.sessions.TERMINATION_JOIN_TIMEOUT = 0.05
        self.start(hold=True, ignore_cancel=True)
        closed = self.sessions.action(request("close", "exec-1"), snapshot("exec-1"))
        self.assertEqual("cleanup_failed", closed["outcome"])
        self.assertIn("turn-worker-timeout", closed["cleanup"])
        refused = self.sessions.action(continue_request("exec-1"), snapshot("exec-1"), FakeAdmission())
        self.assertEqual(("recovery_pending", "termination-unproven"), (refused["outcome"], refused["cleanup"]))
        self.assertFalse(self.sessions.continuation_needs_spawn("exec-1"))
        self.sessions._turn("exec-1").session.state.release.set()

    def test_interrupt_cancels_only_a_running_turn(self):
        self.start(hold=True)
        self.assertEqual("fake:interrupt:session-1:op-start",
                         self.sessions.interrupt(request("cancel", "exec-1"), snapshot("exec-1"))["receipt_ref"])
        self.assertTrue(self.sessions.wait_turn("exec-1", 2))
        self.assertEqual("fake:no-active-turn:session-1",
                         self.sessions.interrupt(request("cancel", "exec-1"), snapshot("exec-1"))["receipt_ref"])

    def test_shutdown_stops_every_turn_and_closes_every_host(self):
        self.start(hold=True)
        self.start("exec-2", "op-2", hold=True)
        self.sessions.shutdown()
        self.assertTrue(all(host.closed for host in self.driver.hosts))
        self.assertFalse(self.sessions.turn_running("exec-1"))


if __name__ == "__main__":
    unittest.main()
