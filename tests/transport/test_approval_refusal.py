"""An agent's request for approval is refused explicitly and its turn ends normally.

The ACP driver's refusal runs over real stdio in `test_acp_permission_boundary.py`.
Here the Codex and Pi drivers run a turn whose agent asks while it works; each
set asserts the same three things: the agent receives an explicit refusal, the
turn then completes, and the turn's action record lists what was refused.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from queue import Queue
from tempfile import TemporaryDirectory
import unittest

from provider_transport.codex_app_server import CodexAppServerAdapter, approval_reply
from provider_transport.codex_driver import CodexDriver
from provider_transport.jsonl_rpc import JsonlRpcClient
from provider_transport.pi_driver import PiDriver, dialog_refusal
from provider_transport.provider_port import HostSlot, TurnSpec
from .codex_rpc_fake import FakeCodexRpc
from .pi_rpc_fake import FakePi
from .test_jsonl_rpc import QueueReader, RecordingWriter
from .test_protocol_drivers import WireAdmission, request


CODEX_REQUESTS = (
    ("item/commandExecution/requestApproval",
     {"threadId": "thread-1", "turnId": "turn-1", "itemId": "i1", "command": "rm -rf build", "reason": "cleanup"}),
    ("item/fileChange/requestApproval",
     {"thread_id": "thread-1", "turn_id": "turn-1", "item_id": "i2", "reason": "write outside the root"}),
    ("item/permissions/requestApproval",
     {"threadId": "thread-1", "turnId": "turn-1", "itemId": "i3", "cwd": "/work", "reason": "network access",
      "permissions": {"network": {"enabled": True}}}),
    ("mcpServer/elicitation/request",
     {"threadId": "thread-1", "turnId": "turn-1", "serverName": "docs", "mode": "form",
      "message": "Which project?", "requestedSchema": {}}),
)
CODEX_REFUSALS = [
    {"kind": "command", "description": "rm -rf build"},
    {"kind": "file_change", "description": "write outside the root"},
    {"kind": "permissions", "description": "network access"},
    {"kind": "input", "description": "docs: Which project?"},
]


class CodexApprovalDecisionTests(unittest.TestCase):
    def test_each_approval_request_has_its_explicit_refusal(self):
        replies = [approval_reply(method, params)[0] for method, params in CODEX_REQUESTS]
        self.assertEqual([
            {"decision": "decline"},
            {"decision": "decline"},
            {"permissions": {}},
            {"action": "decline", "content": None, "_meta": None},
        ], replies)

    def test_any_other_request_is_not_answered_by_the_decision(self):
        for method in ("item/tool/requestUserInput", "item/tool/call", "account/chatgptAuthTokens/refresh",
                       "applyPatchApproval", "execCommandApproval"):
            with self.subTest(method=method):
                self.assertIsNone(approval_reply(method, {"threadId": "thread-1"}))


class AskingCodexRpc(FakeCodexRpc):
    """The App Server fake whose agent asks every approval while its turn runs, then finishes it."""

    def __init__(self, admission):
        super().__init__(admission)
        self.replies = []

    def request(self, method, params):
        result = super().request(method, params)
        if method == "turn/start":
            for asked, asked_params in CODEX_REQUESTS:
                self.replies.append(self.server_request_handler(asked, asked_params))
            self.finish(params["threadId"])
        return result


class CodexRefusalTests(unittest.TestCase):
    def test_the_agent_is_refused_the_turn_completes_and_the_record_lists_the_refusals(self):
        peers = []

        def launch(admission, cell_id):
            peers.append(AskingCodexRpc(admission))
            return peers[-1]

        driver = CodexDriver(timeout=5.0, launch=launch)
        sessions = []
        slot = HostSlot(None, WireAdmission(), "cell", lambda host: None)
        end = driver.run_turn(TurnSpec(request(), "/work", None), slot, sessions.append)
        self.assertEqual([
            {"decision": "decline"}, {"decision": "decline"}, {"permissions": {}},
            {"action": "decline", "content": None, "_meta": None},
        ], peers[0].replies)
        self.assertTrue(end.completed)
        self.assertEqual("completed", end.provider_status)
        self.assertEqual(CODEX_REFUSALS, end.detail["actions"]["refused"])
        start = next(params for method, params in peers[0].calls if method == "thread/start")
        self.assertNotIn("approvalPolicy", start)
        driver.release(sessions[0], 2.0)

    def test_a_resumed_thread_is_sent_no_approval_setting(self):
        peers = []
        driver = CodexDriver(timeout=5.0, launch=lambda admission, cell_id: peers.append(
            AskingCodexRpc(admission)) or peers[-1])
        slot = HostSlot(None, WireAdmission(), "cell", lambda host: None)
        sessions = []
        driver.run_turn(TurnSpec(request(), "/work", "thread-7"), slot, sessions.append)
        resume = next(params for method, params in peers[0].calls if method == "thread/resume")
        self.assertNotIn("approvalPolicy", resume)
        self.assertNotIn("sandbox", resume)
        driver.release(sessions[0], 2.0)

    def test_the_refusal_goes_back_on_the_wire_with_the_request_id(self):
        writer, reader = RecordingWriter(), QueueReader()
        client = JsonlRpcClient(stdin=writer, stdout=reader, request_timeout=1.0)
        self.addCleanup(client.close)
        self.addCleanup(reader.lines.put, None)
        CodexAppServerAdapter(client, generation="codex-app-server:v2")
        for number, (method, params) in enumerate(CODEX_REQUESTS, 40):
            reader.lines.put(json.dumps({"id": number, "method": method, "params": params}) + "\n")
        answered = [writer.messages.get(timeout=1) for _ in CODEX_REQUESTS]
        self.assertEqual([40, 41, 42, 43], [message["id"] for message in answered])
        self.assertEqual("decline", answered[0]["result"]["decision"])
        self.assertEqual({"permissions": {}}, answered[2]["result"])
        reader.lines.put(json.dumps({"id": 50, "method": "item/tool/call", "params": {}}) + "\n")
        self.assertEqual(-32601, writer.messages.get(timeout=1)["error"]["code"])


PI_DIALOGS = (
    {"id": "ui-1", "method": "confirm", "title": "Allow bash: rm -rf build?", "message": "dangerous"},
    {"id": "ui-2", "method": "notify", "message": "just so you know"},
    {"id": "ui-3", "method": "select", "title": "Pick a profile", "options": ["a", "b"]},
    {"id": "ui-4", "method": "setStatus", "statusKey": "k", "statusText": "busy"},
)


class PiRefusalTests(unittest.TestCase):
    def test_only_dialogs_are_answered(self):
        decided = [dialog_refusal({"type": "extension_ui_request", **dialog}) for dialog in PI_DIALOGS]
        self.assertEqual([("ui-1", "Allow bash: rm -rf build?"), None, ("ui-3", "Pick a profile"), None], decided)
        self.assertIsNone(dialog_refusal({"type": "message_update", "id": "ui-1", "method": "confirm"}))

    def test_the_agent_is_refused_the_turn_completes_and_the_record_lists_the_refusals(self):
        with TemporaryDirectory() as directory:
            peers = []

            def launch(command, admission, cell_id):
                admission.spawned(os.getpid())
                peers.append(FakePi(command, dialogs=PI_DIALOGS))
                return peers[-1].client

            driver = PiDriver(session_dir=Path(directory), timeout=5.0, launch=launch)
            sessions = []
            slot = HostSlot(None, WireAdmission(), "cell", lambda host: None)
            end = driver.run_turn(TurnSpec(request(), "/work", None), slot, sessions.append)
            self.assertEqual([
                {"type": "extension_ui_response", "id": "ui-1", "cancelled": True},
                {"type": "extension_ui_response", "id": "ui-3", "cancelled": True},
            ], peers[0].ui_responses)
            self.assertTrue(end.completed)
            self.assertEqual("stop", end.provider_status)
            self.assertEqual([{"kind": "input", "description": "Allow bash: rm -rf build?"},
                              {"kind": "input", "description": "Pick a profile"}], end.detail["actions"]["refused"])
            driver.release(sessions[0], 2.0)


if __name__ == "__main__":
    unittest.main()


class CommandRunningCodexRpc(FakeCodexRpc):
    """The App Server fake whose first turn on each thread runs a command and asks once."""

    def __init__(self, admission):
        super().__init__(admission)
        self.turns_started = 0

    def request(self, method, params):
        result = super().request(method, params)
        if method == "turn/start":
            self.turns_started += 1
            if self.turns_started == 1:
                with self._condition:
                    self._publish_locked({"method": "item/completed", "params": {
                        "threadId": params["threadId"], "turnId": result["turn"]["id"], "item": {
                            "type": "commandExecution", "id": "cmd-1", "command": "make check",
                            "aggregatedOutput": "loop-engine-check unit tree=candidate digest=ab exit=0\n",
                            "exitCode": 0}}})
                self.server_request_handler("item/commandExecution/requestApproval",
                                            {"threadId": params["threadId"], "command": "make check"})
            self.finish(params["threadId"])
        return result


class TurnRecordScopeTests(unittest.TestCase):
    """A continued session's turn record holds only what that turn did."""

    def test_a_resumed_codex_thread_reports_only_the_new_turns_actions(self):
        peers = []
        driver = CodexDriver(timeout=5.0, launch=lambda admission, cell_id: peers.append(
            CommandRunningCodexRpc(admission)) or peers[-1])
        first_slot = HostSlot(None, WireAdmission(), "cell", lambda host: None)
        sessions = []
        first = driver.run_turn(TurnSpec(request(), "/work", None), first_slot, sessions.append)
        self.assertEqual(["make check"], [entry["command"] for entry in first.detail["actions"]["entries"]])
        self.assertEqual(1, len(first.detail["actions"]["refused"]))
        driver.release(sessions[0], 2.0)
        again = driver.run_turn(TurnSpec(request("op-2"), "/work", sessions[0].durable_id),
                                HostSlot(first_slot.host, None, "cell", lambda host: None), sessions.append)
        self.assertEqual(sessions[0].durable_id, sessions[1].durable_id)
        self.assertEqual({"entries": [], "refused": []},
                         {key: again.detail["actions"][key] for key in ("entries", "refused")})
        driver.release(sessions[1], 2.0)

    def test_an_acp_session_starts_each_prompt_with_an_empty_record(self):
        from provider_transport.acp_client import AcpBinding, AcpMessageCapture

        capture = AcpMessageCapture()
        binding = AcpBinding("s-1", "claude", "g")
        capture.begin(binding, 1)
        capture.observe({"method": "session/update", "params": {"sessionId": "s-1", "update": {
            "sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "execute", "rawInput": {"command": "make check"},
            "status": "completed", "rawOutput": "exit=0"}}})
        capture.refused("s-1", "command", "make check")
        capture.finish(completed=True, binding=binding)
        first = capture.take_actions(binding)
        capture.begin(binding, 2)
        capture.finish(completed=True, binding=binding)
        second = capture.take_actions(binding)
        self.assertEqual((1, 1), (len(first["entries"]), len(first["refused"])))
        self.assertEqual(([], []), (second["entries"], second["refused"]))
