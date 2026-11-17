import unittest

from provider_transport.codex_app_server import (
    CodexTurnError,
    CodexAppServerAdapter,
    LookupDisposition,
    TransportBinding,
)


class FakeRpc:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.agent_deltas: list[dict[str, object]] = []
        self.status = {"type": "idle"}
        self.read_model = "gpt-5.6-sol"
        self.wait_timeouts: list[float | None] = []
        self.notifications = {
            "turn/completed": {
                "threadId": "thr-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
            "thread/closed": {"threadId": "thr-1"},
        }

    def request(self, method, params):
        self.calls.append((method, dict(params)))
        if method == "model/list":
            return {"data": [{"id": "model"}], "nextCursor": None}
        if method == "thread/loaded/list":
            return {"data": [], "nextCursor": None}
        if method == "thread/start":
            return {"thread": {"id": "thr-1", "sessionId": "session-1"}}
        if method == "thread/resume":
            return {"thread": {"id": params["threadId"], "sessionId": "session-1"}}
        if method == "turn/start":
            return {"turn": {"id": f"turn-{len(self.calls)}"}}
        if method == "turn/steer":
            return {"turnId": params["expectedTurnId"]}
        if method == "thread/read":
            thread = {"id": "thr-1", "status": self.status}
            if self.read_model is not None:
                thread["model"] = self.read_model
            return {"thread": thread}
        if method in {"turn/interrupt", "thread/unsubscribe"}:
            return {}
        raise AssertionError(f"unexpected method: {method}")

    def wait_notification(self, method, *, predicate, timeout=None):
        self.wait_timeouts.append(timeout)
        value = self.notifications[method]
        if not predicate(value):
            raise TimeoutError
        return value

    def drain_notifications(self, method, *, predicate=lambda _: True):
        if method != "item/agentMessage/delta":
            return []
        values = [value for value in self.agent_deltas if predicate(value)]
        self.agent_deltas = [value for value in self.agent_deltas if value not in values]
        return values


class CodexAppServerAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rpc = FakeRpc()
        self.adapter = CodexAppServerAdapter(self.rpc, generation="server-1")

    def test_start_and_followup_keep_same_thread_binding(self) -> None:
        binding = self.adapter.start(cwd="/workspace/repo")
        first = self.adapter.continue_turn(binding, text="implement", effort="high")
        with self.assertRaisesRegex(RuntimeError, "already-active"):
            self.adapter.continue_turn(binding, text="overlap")
        self.rpc.notifications["turn/completed"]["turn"]["id"] = first
        self.adapter.wait(binding, turn_id=first)
        second = self.adapter.continue_turn(binding, text="fix the findings")

        self.assertNotEqual(first, second)
        turn_calls = [call for call in self.rpc.calls if call[0] == "turn/start"]
        self.assertEqual(
            ["thr-1", "thr-1"],
            [call[1]["threadId"] for call in turn_calls],
        )
        self.assertEqual("high", turn_calls[0][1]["effort"])
        start = next(call for call in self.rpc.calls if call[0] == "thread/start")
        # The thread runs under the user's own approval and sandbox settings.
        self.assertNotIn("approvalPolicy", start[1])
        self.assertNotIn("sandbox", start[1])

    def test_producer_thread_keeps_the_users_tools_plugins_and_mcp(self) -> None:
        # A producer is an ordinary agent: its thread start selects only cwd, model and
        # effort; nothing replaces the user's configuration.
        binding = self.adapter.start(cwd="/workspace/repo", model="m", effort="low")
        self.adapter.continue_turn(binding, text="implement")
        start = next(call[1] for call in self.rpc.calls if call[0] == "thread/start")
        self.assertEqual({"cwd", "serviceName", "model", "effort"}, set(start))
        turn = next(call[1] for call in self.rpc.calls if call[0] == "turn/start")
        self.assertFalse({"config", "dynamicTools", "permissions", "sandboxPolicy"} & set(turn))

    def test_resume_preserves_identity_and_sends_the_model(self) -> None:
        binding = self.adapter.resume(
            "thr-1", cwd="/workspace/repo",
            model="gpt-5.6-terra",
        )
        self.assertEqual("thr-1", binding.thread_id)
        method, params = self.rpc.calls[-1]
        self.assertEqual("thread/resume", method)
        self.assertNotIn("approvalPolicy", params)
        self.assertNotIn("sandbox", params)
        self.assertEqual("gpt-5.6-terra", params["model"])
        self.assertNotIn("effort", params)

    def test_new_session_sends_only_target_context_fields(self) -> None:
        self.adapter.start(
            cwd="/workspace/repo", model="gpt-5.6-terra", effort="medium"
        )
        method, params = self.rpc.calls[-1]
        self.assertEqual("thread/start", method)
        self.assertEqual("gpt-5.6-terra", params["model"])
        self.assertEqual("medium", params["effort"])
        self.assertNotIn("source_model", params)

    def test_new_session_omits_unsupported_effort(self) -> None:
        self.adapter.start(cwd="/workspace/repo", model="gpt-5.6-terra")
        _, params = self.rpc.calls[-1]
        self.assertNotIn("effort", params)

    def test_lookup_interrupt_and_release_are_normalized(self) -> None:
        binding = TransportBinding("thr-1", "session-1", "server-1")
        observation = self.adapter.lookup(binding)
        self.assertEqual(LookupDisposition.CURRENT, observation.disposition)

        self.adapter.interrupt(binding, turn_id="turn-1")
        self.adapter.release(binding)
        self.assertIn(("thread/unsubscribe", {"threadId": "thr-1"}), self.rpc.calls)
        self.assertEqual(65.0, self.rpc.wait_timeouts[-1])

    def test_wait_is_bound_to_thread_and_turn(self) -> None:
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        self.rpc.notifications["turn/completed"]["turn"]["id"] = turn_id
        outcome = self.adapter.wait(binding, turn_id=turn_id)
        self.assertEqual("completed", outcome.status)
        self.assertEqual(turn_id, outcome.turn_id)
        self.assertEqual("absent", outcome.turn_result.communication.presence)

    def test_timeout_is_observable_before_wait_returns_failure(self) -> None:
        activities = []
        adapter = CodexAppServerAdapter(
            self.rpc,
            generation="server-1",
            activity_observer=lambda phase, reason: activities.append((phase, reason)),
        )
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = adapter.continue_turn(binding, text="run")

        def timeout(*args, **kwargs):
            raise TimeoutError("stalled")

        self.rpc.wait_notification = timeout
        with self.assertRaises(CodexTurnError):
            adapter.wait(binding, turn_id=turn_id, timeout=0.01)
        self.assertEqual("prompt-starting", activities[0][0])
        self.assertEqual("degraded", activities[-1][0])
        self.assertIn("timeout", activities[-1][1])

    def test_ordered_deltas_are_authoritative_and_terminal_text_cross_checks(self):
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "delta": "hello "},
            {"threadId": "thr-1", "turnId": turn_id, "delta": "world"},
            {"threadId": "foreign", "turnId": turn_id, "delta": "FOREIGN"},
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        turn["outputText"] = "hello world"
        outcome = self.adapter.wait(binding, turn_id=turn_id)
        self.assertEqual("hello world", outcome.turn_result.communication.text)
        self.assertEqual("completed", outcome.turn_result.transport_evidence.completion)
        self.assertEqual(
            [
                {
                    "threadId": "foreign",
                    "turnId": turn_id,
                    "delta": "FOREIGN",
                }
            ],
            self.rpc.agent_deltas,
        )

    def test_pre_id_deltas_promote_own_turn_and_ignore_foreign_candidates(self):
        class PreIdRpc(FakeRpc):
            def request(self, method, params):
                result = super().request(method, params)
                if method == "turn/start":
                    turn_id = result["turn"]["id"]
                    self.agent_deltas.extend([
                        {"threadId": "thr-1", "turnId": turn_id, "delta": "pre-id"},
                        {"threadId": "thr-1", "turnId": "ambiguous", "delta": "other"},
                    ])
                return result

        rpc = PreIdRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = adapter.continue_turn(binding, text="run")
        turn = rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        result = adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("pre-id", result.communication.text)
        self.assertEqual("completed", result.transport_evidence.completion)
        self.assertEqual("determinate", result.transport_evidence.epistemic_state)

    def test_start_failure_excludes_unattributable_deltas_as_ambiguous(self):
        class StartFailRpc(FakeRpc):
            def request(self, method, params):
                if method == "turn/start":
                    self.agent_deltas.append(
                        {"threadId": "thr-1", "turnId": "t-x", "delta": "unproven"}
                    )
                    raise TimeoutError("start lost")
                return super().request(method, params)

        rpc = StartFailRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        binding = TransportBinding("thr-1", "session-1", "server-1")
        with self.assertRaises(CodexTurnError) as caught:
            adapter.continue_turn(binding, text="run")
        result = caught.exception.turn_result
        self.assertEqual("", result.communication.text)
        self.assertEqual("incomplete", result.transport_evidence.completion)
        self.assertIn(
            "ambiguous_correlation", result.transport_evidence.loss_reasons
        )
        self.assertIn("turn_timeout", result.transport_evidence.loss_reasons)

    def test_oversize_matching_sources_seal_truncated_completed(self):
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        full_text = "x" * 20_000
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "delta": full_text[:10_000]},
            {"threadId": "thr-1", "turnId": turn_id, "delta": full_text[10_000:]},
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        turn["outputText"] = full_text
        result = self.adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("completed", result.transport_evidence.completion)
        self.assertEqual("determinate", result.transport_evidence.epistemic_state)
        self.assertTrue(result.communication.truncated)
        self.assertEqual("present", result.communication.presence)

    def test_sealed_turn_late_deltas_never_enter_the_next_turn(self):
        binding = TransportBinding("thr-1", "session-1", "server-1")
        first = self.adapter.continue_turn(binding, text="one")
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": first, "delta": "first text"},
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = first
        self.adapter.wait(binding, turn_id=first)

        second = self.adapter.continue_turn(binding, text="two")
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": first, "delta": "stale late"},
            {"threadId": "thr-1", "turnId": second, "delta": "second text"},
        ]
        turn["id"] = second
        result = self.adapter.wait(binding, turn_id=second).turn_result
        self.assertEqual("second text", result.communication.text)
        self.assertEqual("completed", result.transport_evidence.completion)
        self.assertEqual((), result.transport_evidence.loss_reasons)

    def test_streamed_pending_deltas_over_budget_note_pending_overflow(self):
        adapter_holder = {}

        class SinkRpc(FakeRpc):
            def __init__(self):
                super().__init__()
                self.notification_sink = None

            def request(self, method, params):
                result = super().request(method, params)
                if method == "turn/start":
                    turn_id = result["turn"]["id"]
                    sink = self.notification_sink
                    for _ in range(3):
                        sink({
                            "method": "item/agentMessage/delta",
                            "params": {
                                "threadId": "thr-1",
                                "turnId": turn_id,
                                "delta": "y" * 8_000,
                            },
                        })
                return result

        rpc = SinkRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        self.assertIsNotNone(rpc.notification_sink)
        self.assertIs(rpc.codex_notification_dispatcher, rpc.notification_sink)
        adapter_holder["adapter"] = adapter
        binding = adapter.start(cwd="/workspace")
        turn_id = adapter.continue_turn(binding, text="run")
        turn = rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        result = adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertIn("pending_overflow", result.transport_evidence.loss_reasons)
        self.assertEqual("incomplete", result.transport_evidence.completion)
        self.assertLessEqual(
            len(result.communication.text.encode("utf-8")), 16_384
        )
        self.assertTrue(result.communication.text.startswith("y"))

    def test_active_turn_streamed_delta_is_collected_without_queueing(self):
        class SinkRpc(FakeRpc):
            def __init__(self):
                super().__init__()
                self.notification_sink = None

        rpc = SinkRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        binding = adapter.start(cwd="/workspace")
        turn_id = adapter.continue_turn(binding, text="run")
        consumed = rpc.notification_sink({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "thr-1", "turnId": turn_id, "delta": "live"},
        })
        self.assertTrue(consumed)
        turn = rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        result = adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("live", result.communication.text)
        self.assertEqual("completed", result.transport_evidence.completion)

    def test_every_event_of_the_turn_counts_as_activity_and_stays_queued(self):
        class SinkRpc(FakeRpc):
            def __init__(self):
                super().__init__()
                self.notification_sink = None

        rpc = SinkRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        binding = adapter.start(cwd="/workspace")
        turn_id = adapter.continue_turn(binding, text="run")
        self.assertEqual(("", None), (adapter.progress(binding).output, adapter.progress(binding).activity_at))
        foreign = rpc.notification_sink({"method": "item/started", "params": {"threadId": "thr-2", "turnId": "other"}})
        self.assertIsNone(adapter.progress(binding).activity_at)
        consumed = rpc.notification_sink({
            "method": "item/started",
            "params": {"threadId": "thr-1", "turnId": turn_id, "item": {"type": "commandExecution"}},
        })
        self.assertFalse(consumed or foreign)
        self.assertIsNotNone(adapter.progress(binding).activity_at)
        self.assertEqual("", adapter.progress(binding).output)

    def test_terminal_disagreement_is_conflicting_incomplete_evidence(self):
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "delta": "delta text"},
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        turn["outputText"] = "different terminal text"
        result = self.adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("delta text", result.communication.text)
        self.assertEqual("incomplete", result.transport_evidence.completion)
        self.assertEqual("conflicting_evidence", result.transport_evidence.epistemic_state)

    def test_progress_notes_before_the_answer_do_not_conflict_and_the_answer_is_the_text(self):
        """Shape of a live gpt-6.1-sol turn (2026-11-14): two commentary messages, then the answer."""
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        messages = (("msg-1", "I will read the files. "), ("msg-2", "Fixed; running the checks. "),
                    ("msg-3", "Changed stats.py and added two tests."))
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "itemId": item, "delta": part}
            for item, text in messages for part in (text[:9], text[9:])
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        turn["items"] = [{"type": "agentMessage", "id": "msg-3", "phase": "final_answer", "text": messages[2][1]}]
        result = self.adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("Changed stats.py and added two tests.", result.communication.text)
        self.assertEqual(("completed", "determinate"),
                         (result.transport_evidence.completion, result.transport_evidence.epistemic_state))

    def test_an_answer_that_differs_from_its_own_stream_still_conflicts(self):
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = self.adapter.continue_turn(binding, text="run")
        self.rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "itemId": "msg-1", "delta": "note "},
            {"threadId": "thr-1", "turnId": turn_id, "itemId": "msg-2", "delta": "streamed answer"},
        ]
        turn = self.rpc.notifications["turn/completed"]["turn"]
        turn["id"] = turn_id
        turn["items"] = [{"type": "agentMessage", "id": "msg-2", "phase": "final_answer", "text": "another answer"}]
        result = self.adapter.wait(binding, turn_id=turn_id).turn_result
        self.assertEqual("streamed answer", result.communication.text)
        self.assertEqual("conflicting_evidence", result.transport_evidence.epistemic_state)

    def test_wait_timeout_preserves_partial_delta_as_incomplete_result(self):
        class TimeoutAfterDeltaRpc(FakeRpc):
            def wait_notification(self, method, *, predicate, timeout=None):
                raise TimeoutError("terminal lost")

        rpc = TimeoutAfterDeltaRpc()
        adapter = CodexAppServerAdapter(rpc, generation="server-1")
        binding = TransportBinding("thr-1", "session-1", "server-1")
        turn_id = adapter.continue_turn(binding, text="run")
        rpc.agent_deltas = [
            {"threadId": "thr-1", "turnId": turn_id, "delta": "partial delta"},
        ]
        with self.assertRaises(CodexTurnError) as caught:
            adapter.wait(binding, turn_id=turn_id)
        result = caught.exception.turn_result
        self.assertEqual("partial delta", result.communication.text)
        self.assertEqual("incomplete", result.transport_evidence.completion)

    def test_generation_mismatch_fails_closed(self) -> None:
        stale = TransportBinding("thr-1", "session-1", "old-server")
        with self.assertRaisesRegex(ValueError, "stale-transport-generation"):
            self.adapter.continue_turn(stale, text="continue")


if __name__ == "__main__":
    unittest.main()
