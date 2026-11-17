import unittest

from provider_transport.turn_result import (
    COMMUNICATION_BUDGET_BYTES,
    HEAD_BUDGET_BYTES,
    MAX_LOSS_REASONS,
    TAIL_BUDGET_BYTES,
    UINT64_MAX,
    Communication,
    TransportEvidence,
    TurnCollector,
    TurnProgress,
    TurnResult,
)


class TurnResultTests(unittest.TestCase):
    def test_completed_present_and_absent_are_distinct(self):
        present = TurnCollector("turn-1")
        present.observe_text("No files changed because the request was already satisfied.")
        present_result = present.seal_completed()
        absent_result = TurnCollector("turn-2").seal_completed()

        self.assertEqual("present", present_result.communication.presence)
        self.assertEqual("completed", present_result.transport_evidence.completion)
        self.assertEqual("absent", absent_result.communication.presence)
        self.assertEqual("", absent_result.communication.text)
        self.assertEqual([], absent_result.to_wire()["transport_evidence"]["loss_reasons"])

    def test_incomplete_empty_is_unknown_and_partial_text_survives(self):
        empty = TurnCollector("turn-empty").seal_incomplete("transport_eof")
        partial_collector = TurnCollector("turn-partial")
        partial_collector.observe_text("partial report")
        partial = partial_collector.seal_incomplete("transport_timeout", epistemic_state="unknown")

        self.assertEqual("unknown", empty.communication.presence)
        self.assertEqual("incomplete", empty.transport_evidence.completion)
        self.assertEqual("present", partial.communication.presence)
        self.assertEqual("partial report", partial.communication.text)
        self.assertEqual("unknown", partial.transport_evidence.epistemic_state)

    def test_unicode_retention_is_deterministic_and_bounded(self):
        text = "先頭" + ("🙂中" * 10_000) + "末尾"
        whole = TurnCollector("whole")
        whole.observe_text(text)
        fragmented = TurnCollector("fragmented")
        for character in text:
            fragmented.observe_text(character)

        first = whole.seal_completed().communication
        second = fragmented.seal_completed().communication
        self.assertEqual(first.text, second.text)
        self.assertEqual(first.dropped_bytes, second.dropped_bytes)
        self.assertTrue(first.truncated)
        self.assertLessEqual(len(first.text.encode("utf-8")), COMMUNICATION_BUDGET_BYTES)
        self.assertLessEqual(len(first.text.encode("utf-8")[:HEAD_BUDGET_BYTES]), HEAD_BUDGET_BYTES)
        self.assertLessEqual(len(first.text.encode("utf-8")[-TAIL_BUDGET_BYTES:]), TAIL_BUDGET_BYTES)
        first.text.encode("utf-8").decode("utf-8")

    def test_over_budget_retention_keeps_exact_head_and_tail(self):
        head = "A" * HEAD_BUDGET_BYTES
        tail = "Z" * TAIL_BUDGET_BYTES
        collector = TurnCollector("head-tail")
        collector.observe_text(head + ("M" * 100) + tail)
        communication = collector.seal_completed().communication
        self.assertEqual(head + tail, communication.text)
        self.assertTrue(communication.text.endswith(tail))
        self.assertEqual(100, communication.dropped_bytes)
        self.assertTrue(communication.truncated)

    def test_unicode_tail_marker_survives_truncation(self):
        text = "先頭" + ("🙂中" * 10_000) + "末尾"
        collector = TurnCollector("tail-marker")
        collector.observe_text(text)
        communication = collector.seal_completed().communication
        self.assertTrue(communication.text.startswith("先頭"))
        self.assertTrue(communication.text.endswith("末尾"))

    def test_invalid_envelope_rows_cannot_be_constructed(self):
        valid_evidence = TransportEvidence("completed", "determinate", ())
        cases = {
            "incomplete-absent": (
                Communication("", "absent", False, 0, False),
                TransportEvidence("incomplete", "inconclusive", ("transport_eof",)),
            ),
            "incomplete-without-loss": (
                Communication("t", "present", False, 0, False),
                TransportEvidence("incomplete", "inconclusive", ()),
            ),
            "absent-with-text": (
                Communication("t", "absent", False, 0, False),
                valid_evidence,
            ),
            "absent-with-loss": (
                Communication("", "absent", False, 0, False),
                TransportEvidence("completed", "determinate", ("transport_eof",)),
            ),
            "truncated-flag-mismatch": (
                Communication("t", "present", True, 0, False),
                valid_evidence,
            ),
            "saturated-mismatch": (
                Communication("t", "present", True, 1, True),
                valid_evidence,
            ),
            "too-many-loss-reasons": (
                Communication("t", "present", False, 0, False),
                TransportEvidence(
                    "incomplete",
                    "inconclusive",
                    tuple(f"reason-{index}" for index in range(MAX_LOSS_REASONS + 1)),
                ),
            ),
        }
        for name, (communication, evidence) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    TurnResult("turn", communication, evidence)

    def test_from_wire_rejects_invalid_truth_table_rows(self):
        base = TurnCollector("turn").seal_completed().to_wire()
        wire = {
            **base,
            "communication": {**base["communication"], "text": "hidden"},
        }
        with self.assertRaises(ValueError):
            TurnResult.from_wire(wire)
        wire = {
            **base,
            "transport_evidence": {
                "completion": "incomplete",
                "epistemic_state": "inconclusive",
                "loss_reasons": ["transport_eof"],
            },
        }
        with self.assertRaises(ValueError):
            TurnResult.from_wire(wire)

    def test_loss_reason_overflow_sentinel_replaces_last_slot(self):
        collector = TurnCollector("overflow")
        for index in range(MAX_LOSS_REASONS + 2):
            collector.note_loss(f"reason-{index}")
        result = collector.seal_incomplete("final")
        reasons = result.transport_evidence.loss_reasons
        self.assertEqual(MAX_LOSS_REASONS, len(reasons))
        self.assertIn("loss_reason_overflow", reasons)

    def test_reference_projection_matches_collector_projection(self):
        text = "頭" * 9_000 + "尾" * 3_000
        collector = TurnCollector("reference")
        collector.observe_text(text)
        self.assertTrue(collector.truncated)
        self.assertEqual(
            collector.text_preview(), collector.project_reference_text(text)
        )

    def test_seal_is_exactly_once_and_late_text_is_ignored(self):
        collector = TurnCollector("turn")
        collector.observe_text("first")
        first = collector.seal_completed()
        self.assertFalse(collector.observe_text("late"))
        self.assertIs(first, collector.seal_completed())
        self.assertEqual("first", first.communication.text)

    def test_progress_reports_text_and_the_latest_event_of_any_kind(self):
        times = iter((10.0, 20.0, 30.0))
        collector = TurnCollector("turn", clock=lambda: next(times))
        self.assertEqual(TurnProgress("", None), collector.progress())
        collector.observe_text("reading")
        self.assertEqual(TurnProgress("reading", 10.0), collector.progress())
        collector.note_activity()
        self.assertEqual(TurnProgress("reading", 20.0), collector.progress())
        collector.seal_completed()
        collector.note_activity()
        self.assertEqual(TurnProgress("reading", 20.0), collector.progress())

    def test_dropped_byte_counter_saturates(self):
        collector = TurnCollector("turn", initial_total_bytes=UINT64_MAX)
        collector.observe_text("x" * (COMMUNICATION_BUDGET_BYTES + 1))
        result = collector.seal_completed().communication
        self.assertEqual(UINT64_MAX, result.dropped_bytes)
        self.assertTrue(result.dropped_bytes_saturated)

    def test_million_fragments_keep_fixed_projection_size(self):
        collector = TurnCollector("million-fragments")
        for _ in range(1_000_000):
            collector.observe_text("x")
        communication = collector.seal_completed().communication
        self.assertEqual(COMMUNICATION_BUDGET_BYTES, len(communication.text.encode("utf-8")))
        self.assertEqual(1_000_000 - COMMUNICATION_BUDGET_BYTES, communication.dropped_bytes)
        self.assertTrue(communication.truncated)

    def test_wire_round_trip_and_unknown_version_rejection(self):
        result = TurnCollector("turn").seal_completed()
        self.assertEqual(result, TurnResult.from_wire(result.to_wire()))
        wire = result.to_wire()
        wire["version"] = 2
        with self.assertRaisesRegex(ValueError, "unsupported"):
            TurnResult.from_wire(wire)
        wire = result.to_wire()
        wire["acceptance"] = "accepted"
        with self.assertRaisesRegex(ValueError, "malformed"):
            TurnResult.from_wire(wire)

    def test_envelope_has_no_consumer_or_lifecycle_authority_fields(self):
        wire = TurnCollector("turn").seal_completed().to_wire()
        self.assertEqual(
            {"version", "turn_identity", "communication", "transport_evidence"},
            set(wire),
        )
        serialized = repr(wire)
        for forbidden in ("acceptance", "verification", "artifact", "lifecycle", "cleanup"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
