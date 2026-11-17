import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from provider_transport.acp_client import (
    AcpBinding,
    AcpClientRequestPolicy,
    AcpMessageCapture,
    AcpPromptError,
    AcpSessionClient,
    ParentAuthority,
    AcpStdioConnection,
)
from provider_transport.cwd_authority import ChildLedger, SpawnAdmission
from provider_transport.directory_authority import create_cwd_authority
from provider_transport.testing.cell_fake import FakeCellMembership


class FakeRpc:
    def __init__(self):
        self.requests = []
        self.notifications = []
        self.results = {
            "session/new": {"sessionId": "s-new"},
            "session/load": {},
            "session/prompt": {"stopReason": "end_turn"},
            "session/set_config_option": {
                "configOptions": [{"id": "model", "currentValue": "haiku"}]
            },
            "session/close": {},
        }

    def request(self, method, params, *, timeout=None):
        self.requests.append((method, params))
        return self.results[method]

    def notify(self, method, params):
        self.notifications.append((method, params))


class AcpSessionClientTests(unittest.TestCase):
    def test_resume_capability_uses_resume_without_replay_and_keeps_identity(self):
        rpc = FakeRpc()
        rpc.results["session/resume"] = {"configOptions": [
            {"id": "model", "currentValue": "claude-opus-5-5"},
            {"id": "effort", "currentValue": "high"},
        ]}
        client = AcpSessionClient(rpc, provider="claude", generation="claude-test",
                                  capabilities={"loadSession": False, "sessionCapabilities": {"resume": {}}})
        binding = client.load_session("child-1", cwd="/work")
        self.assertEqual(("session/resume", {"sessionId": "child-1", "cwd": "/work", "mcpServers": []}), rpc.requests[-1])
        self.assertEqual("child-1", binding.session_id)
        rpc.results["session/resume"] = {"sessionId": "foreign"}
        with self.assertRaisesRegex(ValueError, "foreign-session"):
            client.load_session("child-1", cwd="/work")

    def test_model_and_effort_bind_to_exact_wire_values(self):
        rpc = FakeRpc()
        client = AcpSessionClient(rpc, provider="claude", generation="g", capabilities={})
        binding = client.new_session(cwd="/work")
        for key, value in (("model", "claude-opus-5-5"), ("effort", "high")):
            rpc.results["session/set_config_option"] = {"configOptions": [{"id": key, "currentValue": value}]}
            client.set_config_option(binding, config_id=key, value=value)
            self.assertEqual({"sessionId": binding.session_id, "configId": key, "value": value}, rpc.requests[-1][1])
            rpc.results["session/set_config_option"] = {"configOptions": [{"id": key, "currentValue": "wrong"}]}
            with self.assertRaisesRegex(RuntimeError, "readback-mismatch"):
                client.set_config_option(binding, config_id=key, value=value)

    def test_session_meta_is_sent_on_new_and_load(self):
        rpc = FakeRpc()
        rpc.results["session/load"] = {}
        client = AcpSessionClient(rpc, provider="claude", generation="g", capabilities={"loadSession": True})
        meta = {"claudeCode": {"options": {"disallowedTools": ["Skill(dev:design)"]}}}
        client.new_session(cwd="/work", meta=meta)
        self.assertEqual(meta, rpc.requests[-1][1]["_meta"])
        client.load_session("child-1", cwd="/work", meta=meta)
        self.assertEqual(("session/load", meta), (rpc.requests[-1][0], rpc.requests[-1][1]["_meta"]))

    def test_close_forwards_explicit_timeout_to_rpc(self):
        calls = []

        class CloseRpc:
            def request(self, method, params, *, timeout=None):
                calls.append((method, params, timeout))
                return {}

        client = self.client(CloseRpc(), closeSession=True)
        client.close(
            AcpBinding("s-old", "claude", "provider-gen-1"), timeout=5.0
        )

        self.assertEqual(
            [("session/close", {"sessionId": "s-old"}, 5.0)], calls
        )

    def test_prompt_waits_without_a_turn_duration_timeout(self):
        from provider_transport.jsonl_rpc import NO_TIMEOUT

        calls = []

        class PromptRpc:
            def request(self, method, params, *, timeout=None):
                calls.append((method, timeout))
                return {"stopReason": "end_turn"}

            def notify(self, method, params):
                pass

        client = self.client(PromptRpc(), loadSession=False)
        client.prompt(AcpBinding("s-old", "claude", "provider-gen-1"), "work")

        self.assertEqual([("session/prompt", NO_TIMEOUT)], calls)

    def test_stdio_spawn_uses_admitted_renamed_directory_identity(self):
        script = r'''
import json, os, sys
for line in sys.stdin:
    request = json.loads(line)
    if request.get("method") == "initialize":
        print(json.dumps({"id": request["id"], "result": {"agentCapabilities": {"cwd": os.getcwd()}}}), flush=True)
'''
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"; target.mkdir()
            authority = create_cwd_authority(root)
            cell = FakeCellMembership()
            ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
            admission = SpawnAdmission(
                reservation=ledger.reserve(), cwd_lease=authority.admit("target"),
                cell_id="cell", epoch=0, fence_check=lambda epoch: epoch == 0,
                cell_membership=cell,
            )
            renamed = root / "renamed"
            target.rename(renamed)
            target.mkdir()
            connection = AcpStdioConnection.launch(
                (sys.executable, "-c", script),
                authority=ParentAuthority((root,)),
                request_timeout=2,
                spawn_admission=admission,
                host_cell_id="cell",
            )
            try:
                self.assertEqual(str(renamed), connection.capabilities["cwd"])
                self.assertNotEqual(str(target), connection.capabilities["cwd"])
            finally:
                connection.rpc.close()
                self.assertTrue(ledger.wait_empty(1))
                authority.close()

    def client(self, rpc, **capabilities):
        return AcpSessionClient(
            rpc,
            provider="claude",
            generation="provider-gen-1",
            capabilities=capabilities,
        )

    def test_uses_exact_session_method_directions(self):
        rpc = FakeRpc()
        client = self.client(rpc, loadSession=True, closeSession=True)
        created = client.new_session(cwd="/work")
        loaded = client.load_session("s-old", cwd="/work")
        outcome = client.prompt(loaded, "fix the findings")
        client.cancel(loaded)
        client.close(loaded)

        self.assertEqual("s-new", created.session_id)
        self.assertEqual("end_turn", outcome.stop_reason)
        self.assertEqual(
            ["session/new", "session/load", "session/prompt", "session/close"],
            [method for method, _ in rpc.requests],
        )
        self.assertEqual(
            [("session/cancel", {"sessionId": "s-old"})], rpc.notifications
        )

    def test_load_and_close_are_capability_gated(self):
        rpc = FakeRpc()
        client = self.client(rpc)
        with self.assertRaisesRegex(RuntimeError, "not-capability-confirmed"):
            client.load_session("s-old", cwd="/work")
        client.close(AcpBinding("s-old", "claude", "provider-gen-1"))
        self.assertEqual([], rpc.requests)

    def test_empty_close_capability_object_enables_session_close(self):
        rpc = FakeRpc()
        client = self.client(rpc, sessionCapabilities={"close": {}})

        client.close(AcpBinding("s-old", "claude", "provider-gen-1"))

        self.assertEqual(
            [("session/close", {"sessionId": "s-old"})], rpc.requests
        )

    def test_foreign_or_stale_binding_is_rejected(self):
        rpc = FakeRpc()
        client = self.client(rpc)
        with self.assertRaisesRegex(ValueError, "stale-or-foreign"):
            client.prompt(AcpBinding("s", "grok", "provider-gen-1"), "feedback")

    def test_config_binding_requires_exact_provider_readback(self):
        rpc = FakeRpc()
        client = self.client(rpc)
        binding = AcpBinding("s", "claude", "provider-gen-1")
        client.set_config_option(binding, config_id="model", value="haiku")
        with self.assertRaisesRegex(RuntimeError, "readback-mismatch"):
            client.set_config_option(binding, config_id="effort", value="high")

    def test_formal_fable_transport_literal_accepts_exact_normalized_readback(self):
        rpc = FakeRpc()
        rpc.results["session/set_config_option"] = {
            "configOptions": [{"id": "model", "currentValue": "claude-fable-5[1m]"}]
        }
        client = self.client(rpc)
        client.set_config_option(
            AcpBinding("s", "claude", "provider-gen-1"),
            config_id="model",
            value="claude-fable-5[1m]",
        )

    def test_bounded_capture_observes_feedback_without_persisting_raw_updates(self):
        capture = AcpMessageCapture(max_chars=32)
        binding = AcpBinding("s", "claude", "provider-gen-1")
        capture.begin(binding, invocation_sequence=1)
        capture.observe(
            {
                "method": "session/update",
                "params": {
                    "update": {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "WRONG"},
                    }
                },
            }
        )
        result = capture.finish(completed=False, binding=binding)
        self.assertEqual("", result.communication.text)
        self.assertIn(
            "identity_missing_or_invalid",
            result.transport_evidence.loss_reasons,
        )

    def test_capture_publishes_progress_without_raw_content(self):
        activities = []
        capture = AcpMessageCapture(
            activity_observer=lambda phase, reason: activities.append((phase, reason))
        )
        binding = AcpBinding("s-old", "claude", "provider-gen-1")
        capture.begin(binding, invocation_sequence=1)
        capture.observe({
            "method": "session/update",
            "params": {
                "sessionId": "s-old",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "secret output"},
                },
            },
        })
        capture.finish(completed=False, reason="transport_timeout")
        self.assertEqual(
            [
                ("prompt-starting", "session-prompt-starting"),
                ("running", "agent-message-chunk"),
                ("degraded", "transport_timeout"),
            ],
            activities,
        )
        self.assertNotIn("secret output", repr(activities))

    def test_progress_projects_bounded_active_text_without_sealing(self):
        capture = AcpMessageCapture(max_chars=32)
        binding = AcpBinding("s-old", "claude", "provider-gen-1")
        capture.begin(binding, invocation_sequence=1)
        capture.observe({
            "method": "session/update",
            "params": {
                "sessionId": "s-old",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "partial output"},
                },
            },
        })
        self.assertEqual("partial output", capture.progress(binding).output)
        # progress does not seal or consume the active turn
        self.assertEqual("partial output", capture.progress(binding).output)
        self.assertIsNotNone(capture.progress(binding).activity_at)
        self.assertFalse(capture._collectors[binding.session_id].sealed)
        capture.finish(completed=True, binding=binding)
        self.assertEqual("", capture.progress(binding).output)

    def test_every_session_update_of_the_turn_counts_as_activity(self):
        capture = AcpMessageCapture(max_chars=32)
        binding = AcpBinding("s-tool", "claude", "provider-gen-1")
        capture.begin(binding, invocation_sequence=1)
        self.assertIsNone(capture.progress(binding).activity_at)
        capture.observe({
            "method": "session/update",
            "params": {"sessionId": "s-tool", "update": {"sessionUpdate": "tool_call", "toolCallId": "t1"}},
        })
        self.assertEqual("", capture.progress(binding).output)
        self.assertIsNotNone(capture.progress(binding).activity_at)

    def test_prompt_returns_only_matching_session_agent_communication(self):
        capture = AcpMessageCapture()

        class EmittingRpc(FakeRpc):
            def request(self, method, params, *, timeout=None):
                if method == "session/prompt":
                    capture.observe({
                        "method": "session/update",
                        "params": {
                            "sessionId": "foreign",
                            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "FOREIGN"}},
                        },
                    })
                    capture.observe({
                        "method": "session/update",
                        "params": {
                            "sessionId": "s-old",
                            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "report"}},
                        },
                    })
                return super().request(method, params)

        rpc = EmittingRpc()
        client = AcpSessionClient(
            rpc, provider="claude", generation="provider-gen-1",
            capabilities={}, message_capture=capture,
        )
        outcome = client.prompt(AcpBinding("s-old", "claude", "provider-gen-1"), "go")
        self.assertEqual("report", outcome.turn_result.communication.text)
        self.assertEqual("completed", outcome.turn_result.transport_evidence.completion)

    def test_prompt_continuation_uses_fresh_identity_and_empty_collector(self):
        capture = AcpMessageCapture()

        class OnceRpc(FakeRpc):
            emitted = False
            def request(self, method, params, *, timeout=None):
                if method == "session/prompt" and not self.emitted:
                    self.emitted = True
                    capture.observe({
                        "method": "session/update",
                        "params": {
                            "sessionId": "s-old",
                            "update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "first"}},
                        },
                    })
                return super().request(method, params)

        client = AcpSessionClient(
            OnceRpc(), provider="claude", generation="provider-gen-1",
            capabilities={}, message_capture=capture,
        )
        binding = AcpBinding("s-old", "claude", "provider-gen-1")
        first = client.prompt(binding, "one").turn_result
        second = client.prompt(binding, "two").turn_result
        self.assertNotEqual(first.turn_identity, second.turn_identity)
        self.assertEqual("first", first.communication.text)
        self.assertEqual("absent", second.communication.presence)

    def test_idle_window_late_chunk_is_excluded_from_both_turns(self):
        capture = AcpMessageCapture()
        client = AcpSessionClient(
            FakeRpc(), provider="claude", generation="provider-gen-1",
            capabilities={}, message_capture=capture,
        )
        binding = AcpBinding("s-old", "claude", "provider-gen-1")
        first = client.prompt(binding, "one").turn_result
        # A late first-turn chunk delivered after seal, before the next prompt.
        capture.observe({
            "method": "session/update",
            "params": {
                "sessionId": "s-old",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "LATE"},
                },
            },
        })
        second = client.prompt(binding, "two").turn_result
        self.assertEqual("", first.communication.text)
        self.assertEqual("absent", second.communication.presence)
        self.assertEqual("completed", second.transport_evidence.completion)
        self.assertEqual((), second.transport_evidence.loss_reasons)

    def test_missing_identity_and_prompt_failure_return_incomplete_evidence(self):
        capture = AcpMessageCapture()

        class FailingRpc(FakeRpc):
            def request(self, method, params, *, timeout=None):
                if method == "session/prompt":
                    capture.observe({
                        "method": "session/update",
                        "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "untrusted"}}},
                    })
                    raise TimeoutError("lost terminal")
                return super().request(method, params)

        client = AcpSessionClient(
            FailingRpc(), provider="claude", generation="provider-gen-1",
            capabilities={}, message_capture=capture,
        )
        with self.assertRaises(AcpPromptError) as caught:
            client.prompt(AcpBinding("s-old", "claude", "provider-gen-1"), "go")
        result = caught.exception.turn_result
        self.assertEqual("incomplete", result.transport_evidence.completion)
        self.assertEqual("unknown", result.communication.presence)
        self.assertIn(
            "identity_missing_or_invalid", result.transport_evidence.loss_reasons
        )

    def test_resume_load_sends_identity_only_and_no_config(self):
        rpc = FakeRpc()
        rpc.results["session/load"] = {
            "sessionId": "s-old",
            "configOptions": [{"id": "model", "currentValue": "live-model"}],
        }
        client = self.client(rpc, loadSession=True)
        client.load_session("s-old", cwd="/work")

        method, params = rpc.requests[0]
        self.assertEqual("session/load", method)
        self.assertEqual("s-old", params["sessionId"])
        self.assertNotIn("model", params)
        self.assertNotIn("effort", params)
        self.assertEqual(["session/load"], [m for m, _ in rpc.requests])


class AcpClientRequestPolicyTests(unittest.TestCase):
    OPTIONS = [
        {"kind": "allow_always", "optionId": "always"},
        {"kind": "allow_once", "optionId": "once"},
        {"kind": "reject_once", "optionId": "reject"},
        {"kind": "reject_always", "optionId": "never"},
    ]

    def test_every_permission_request_selects_the_reject_once_option(self):
        refused = []
        policy = AcpClientRequestPolicy(ParentAuthority((Path("/work"),)),
                                        refused=lambda *item: refused.append(item))
        for kind in ("execute", "read", "search", "edit", "delete", "move", "fetch", "other"):
            with self.subTest(kind=kind):
                response = policy.handle("session/request_permission", {
                    "sessionId": "s-1",
                    "toolCall": {"kind": kind, "title": f"{kind} something",
                                 "locations": [{"path": "/work/file"}]},
                    "options": self.OPTIONS,
                })
                self.assertEqual({"outcome": {"outcome": "selected", "optionId": "reject"}}, response)
        self.assertEqual(
            [("s-1", "command"), ("s-1", "permissions"), ("s-1", "permissions"), ("s-1", "file_change"),
             ("s-1", "file_change"), ("s-1", "file_change"), ("s-1", "permissions"), ("s-1", "permissions")],
            [(session, kind) for session, kind, _ in refused])
        self.assertEqual("execute something", refused[0][2])
        self.assertEqual([], policy.failures)

    def test_a_request_without_a_reject_once_option_is_cancelled(self):
        policy = AcpClientRequestPolicy(ParentAuthority((Path("/work"),)))
        for offered in (None, [], [{"kind": "allow_once", "optionId": "once"}],
                        [{"kind": "allow_always", "optionId": "always"}, {"kind": "reject_always", "optionId": "x"}],
                        [{"kind": "reject_once", "optionId": ""}], "reject_once"):
            with self.subTest(options=offered):
                response = policy.handle("session/request_permission", {
                    "toolCall": {"kind": "execute"}, "options": offered})
                self.assertEqual({"outcome": {"outcome": "cancelled"}}, response)

    def test_the_option_is_chosen_by_its_kind_not_its_id(self):
        policy = AcpClientRequestPolicy(ParentAuthority((Path("/work"),)))
        for option_id in ("reject", "deny", "provider-specific-id"):
            with self.subTest(option_id=option_id):
                response = policy.handle("session/request_permission", {
                    "toolCall": {"kind": "edit"},
                    "options": [{"kind": "allow_once", "optionId": "allow"},
                                {"kind": "reject_once", "optionId": option_id}],
                })
                self.assertEqual({"outcome": "selected", "optionId": option_id}, response["outcome"])

    def test_a_refusal_is_recorded_only_in_the_running_prompt_of_its_session(self):
        capture = AcpMessageCapture()
        policy = AcpClientRequestPolicy(ParentAuthority((Path("/work"),)), refused=capture.refused)
        running = AcpBinding("running", "claude", "g")
        capture.begin(running, 1)
        request = {"toolCall": {"kind": "execute", "title": "rm -rf build"}, "options": self.OPTIONS}
        for session in ("running", "foreign", None):
            reply = policy.handle("session/request_permission", {**request, "sessionId": session})
            self.assertEqual("reject", reply["outcome"]["optionId"])
        actions = capture.take_actions(running)
        self.assertEqual([{"kind": "command", "description": "rm -rf build"}], actions["refused"])

    def test_filesystem_escape_and_terminal_are_fail_closed(self):
        with TemporaryDirectory() as root:
            policy = AcpClientRequestPolicy(ParentAuthority((Path(root),)))
            with self.assertRaisesRegex(PermissionError, "outside-parent"):
                policy.handle("fs/read_text_file", {"path": "/etc/passwd"})
            with self.assertRaisesRegex(PermissionError, "not-authorized"):
                policy.handle("terminal/create", {})
            self.assertEqual(
                [
                    ("fs/read_text_file", "filesystem_outside_parent_authority"),
                    ("terminal/create", "terminal_request_not_authorized"),
                ],
                [
                    (item["method"], item["error_code"])
                    for item in policy.failures
                ],
            )
            self.assertNotIn("/etc/passwd", repr(policy.failures))
            self.assertTrue(all(item["retry_decision"] == "caller_owned" for item in policy.failures))


if __name__ == "__main__":
    unittest.main()
