"""The provider lifecycle's pure decisions map observations to one wire view for every protocol."""
import unittest

from provider_transport.provider_lifecycle import (
    continuation_refusal,
    continuation_spawns,
    joined_diagnostics,
    running_receipt,
    terminal_view,
    turn_evidence,
    release_status,
    unload_status,
)
from provider_transport.provider_port import SessionHandle, TurnEnd


SESSION = SessionHandle("session-1", "thread-1", {"provider": "fake"})


class ProviderLifecycleTests(unittest.TestCase):
    def test_a_running_receipt_names_the_session_and_its_durable_identity(self):
        self.assertEqual({
            "outcome": "running", "session_id": "session-1", "provider_thread_id": "thread-1",
            "binding_proof": {"provider": "fake"}, "provider_status": "running", "cleanup": "not_required",
        }, running_receipt(SESSION))

    def test_a_terminal_view_keeps_the_providers_verdict_and_cleanup(self):
        cases = (
            (TurnEnd(True, "end_turn", {"turn_identity": "t"}), "completed"),
            (TurnEnd(False, "refusal", {"turn_identity": "t"}), "post_start_failed"),
            (TurnEnd(True, "end_turn", {"turn_identity": "t"}, cleanup="close:failed"), "cleanup_failed"),
        )
        for end, outcome in cases:
            with self.subTest(outcome=outcome):
                view = terminal_view(session=SESSION, end=end, error=None, identity="session-1:op")
                self.assertEqual(outcome, view["outcome"])
                self.assertEqual(end.provider_status, view["provider_status"])
                self.assertEqual("thread-1", view["provider_thread_id"])
                self.assertEqual(end.cleanup or "not_required", view["cleanup"])

    def test_a_turn_ended_by_an_error_is_an_incomplete_failure(self):
        view = terminal_view(session=None, end=None, error=OSError("pipe closed"), identity="op:op")
        self.assertEqual("post_start_failed", view["outcome"])
        self.assertEqual({"reason": "OSError:pipe closed"}, view["view"])
        self.assertEqual("op:op", view["turn_result"]["turn_identity"])
        self.assertNotIn("provider_thread_id", view)

    def test_evidence_names_the_turn_result_identity_or_the_fallback(self):
        view = terminal_view(session=SESSION, end=TurnEnd(True, "end_turn", {"turn_identity": "r"}),
                             error=None, identity="fallback")
        self.assertEqual("r", turn_evidence(view, operation_id="op", fallback_identity="fallback")["turn_identity"])
        view = terminal_view(session=SESSION, end=TurnEnd(True, "end_turn", None), error=None, identity="fallback")
        evidence = turn_evidence(view, operation_id="op", fallback_identity="fallback")
        self.assertEqual(("fallback", "op", "completed"),
                         (evidence["turn_identity"], evidence["turn_operation"], evidence["outcome"]))

    def test_unload_status(self):
        self.assertEqual(("completed", None), unload_status(scheduled=False, done=False, error=None))
        self.assertEqual(("pending", None), unload_status(scheduled=True, done=False, error=None))
        self.assertEqual(("completed", None), unload_status(scheduled=True, done=True, error=None))
        self.assertEqual(("failed", "boom"), unload_status(scheduled=True, done=True, error="boom"))

    def test_a_session_is_released_only_once_its_turn_ended_and_its_unload_completed(self):
        self.assertEqual(("pending", "turn-running"),
                         release_status(turn_running=True, scheduled=True, done=True, error=None))
        self.assertEqual(("pending", "unload-not-finished"),
                         release_status(turn_running=False, scheduled=True, done=False, error=None))
        self.assertEqual(("failed", "boom"), release_status(turn_running=False, scheduled=True, done=True, error="boom"))
        self.assertEqual(("completed", None), release_status(turn_running=False, scheduled=True, done=True, error=None))

    def test_a_continue_is_refused_until_its_session_can_take_a_turn(self):
        ready = dict(session_id="s", quarantined=False, turn_running=False, unload="completed",
                     unload_error=None, needs_spawn=True, admitted=True)
        self.assertIsNone(continuation_refusal(**ready))
        cases = (
            ({"session_id": None}, {"outcome": "session_lost"}),
            ({"quarantined": True}, {"outcome": "recovery_pending", "session_id": "s",
                                     "cleanup": "termination-unproven"}),
            ({"turn_running": True}, {"outcome": "recovery_pending", "session_id": "s", "provider_status": "running"}),
            ({"unload": "pending"}, {"outcome": "recovery_pending", "session_id": "s", "provider_status": "unloading"}),
            ({"unload": "failed", "unload_error": "x"}, {"outcome": "recovery_pending", "session_id": "s",
                                                          "cleanup": "x"}),
            ({"admitted": False}, {"outcome": "recovery_pending", "session_id": "s"}),
        )
        for change, refusal in cases:
            with self.subTest(change=change):
                self.assertEqual(refusal, continuation_refusal(**{**ready, **change}))
        self.assertIsNone(continuation_refusal(**{**ready, "needs_spawn": False, "admitted": False}))

    def test_only_a_continue_that_can_begin_asks_for_a_spawn(self):
        ready = dict(known=True, quarantined=False, turn_running=False, unloading=False, loaded_on_live_host=False)
        self.assertTrue(continuation_spawns(**ready))
        self.assertTrue(continuation_spawns(**{**ready, "unloading": True, "loaded_on_live_host": True}))
        self.assertFalse(continuation_spawns(**{**ready, "loaded_on_live_host": True}))
        for refused in ("known", "quarantined", "turn_running"):
            with self.subTest(refused=refused):
                self.assertFalse(continuation_spawns(**{**ready, refused: refused != "known"}))

    def test_diagnostics_join_only_present_parts(self):
        self.assertIsNone(joined_diagnostics(None, ""))
        self.assertEqual("a;b", joined_diagnostics("a", None, "b"))


if __name__ == "__main__":
    unittest.main()
