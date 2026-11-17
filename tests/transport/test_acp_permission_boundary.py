"""Credential-free permission callback through the provider sessions and real ACP stdio.

Every permission request is refused explicitly and the turn goes on to its end.
"""

from dataclasses import replace
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
import os
from unittest.mock import patch

from provider_transport.acp_router import load_provider
from provider_transport.cwd_authority import ChildLedger, SpawnAdmission
from provider_transport.directory_authority import create_cwd_authority
from provider_transport.provider_request import ActionRequest
from provider_transport.provider_drivers import driver_for
from provider_transport.provider_sessions import ProviderSessions
from provider_transport.testing.cell_fake import FakeCellMembership


# The peer replays an ACP provider's permission options. It has no LLM, credentials,
# network or real provider; JSON-RPC and client permission handling are real.
PEER = r'''
import json, sys
from pathlib import Path
pending = None
for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    if method == "initialize":
        result = {"agentCapabilities": {"loadSession": True}}
    elif method in ("session/new", "session/load"):
        result = {"sessionId": "permission-session"}
    elif method == "session/set_config_option":
        result = {"configOptions": [{"id": request["params"]["configId"], "currentValue": request["params"]["value"]}]}
    elif method == "session/prompt":
        pending = request["id"]
        permission = json.loads(Path("permission.json").read_text())
        print(json.dumps({"jsonrpc": "2.0", "id": 999, "method": "session/request_permission", "params": permission}), flush=True)
        continue
    elif method is None and request.get("id") == 999:
        response = request.get("result", {})
        Path("response.json").write_text(json.dumps(response))
        chosen = response.get("outcome", {})
        offered = {option["optionId"]: option["kind"] for option in permission["options"]}
        if chosen.get("outcome") == "selected" and offered.get(chosen.get("optionId"), "").startswith("allow"):
            Path("work.txt").write_text("done")
        print(json.dumps({"jsonrpc": "2.0", "id": pending, "result": {"stopReason": "end_turn"}}), flush=True)
        continue
    else:
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''


REFUSE_AND_ALLOW = [
    {"optionId": "always", "kind": "allow_always", "name": "Always allow"},
    {"optionId": "once", "kind": "allow_once", "name": "Allow once"},
    {"optionId": "reject", "kind": "reject_once", "name": "Reject"},
]


class AcpPermissionBoundaryTests(unittest.TestCase):
    def run_permission(self, *, kind, options=REFUSE_AND_ALLOW, outside=False):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            peer = root / "peer.py"
            peer.write_text(PEER)
            permission = {
                "sessionId": "permission-session",
                "toolCall": {
                    "toolCallId": "tool-1", "kind": kind, "title": f"{kind} the work file",
                    "locations": (
                        [{"path": "/etc/permission-target" if outside else str(root / "work.txt")}]
                        if kind != "execute" else []
                    ),
                    "rawInput": {"secret": "do-not-copy-to-diagnostics"},
                },
                "options": options,
            }
            (root / "permission.json").write_text(json.dumps(permission))
            spec = replace(load_provider("claude"), command_prefix=(sys.executable, str(peer)), command_base="path")
            membership = FakeCellMembership()
            ledger = ChildLedger(cell_id="cell", cell=membership, max_children=4)
            authority = create_cwd_authority(root)
            with patch("provider_transport.acp_router.load_provider", return_value=spec):
                sessions = ProviderSessions(
                    driver_for(spec, origin=root, state_dir=root / "state"), cell_id="cell",
                )
                execution = SimpleNamespace(execution_id="exec-1")
                try:
                    admission = SpawnAdmission(
                        reservation=ledger.reserve(), cwd_lease=authority.admit("."), cell_id="cell",
                        epoch=1, fence_check=lambda _epoch: True, cell_membership=membership,
                    )
                    sessions.bind_pending_execution("exec-1", "start")
                    start = sessions.start(ActionRequest(
                        action="start", operation_id="start", provider="claude",
                        model="test-model", cwd=str(root), prompt="perform isolated work",
                    ), admission)
                    self.assertEqual("running", start["outcome"])
                    self.assertTrue(sessions.wait_turn("exec-1", 5))
                    observed = sessions.action(
                        ActionRequest(action="observe", operation_id="observe"), execution,
                    )
                    response = json.loads((root / "response.json").read_text())
                    did_work = (root / "work.txt").exists()
                    closed = sessions.action(
                        ActionRequest(action="close", operation_id="close"), execution,
                    )
                    self.assertEqual("completed", closed["outcome"])
                    return response, did_work, observed
                finally:
                    sessions.shutdown()
                    ledger.terminate_owned()
                    authority.close()

    def assert_refused_and_completed(self, observed, kind):
        self.assertEqual("completed", observed["outcome"])
        self.assertEqual("end_turn", observed["provider_status"])
        self.assertEqual([{"kind": kind, "description": observed["actions"]["refused"][0]["description"]}],
                         observed["actions"]["refused"])
        self.assertNotIn("do-not-copy-to-diagnostics", json.dumps(observed))

    def test_every_permission_request_is_answered_with_its_reject_option_and_the_turn_ends(self):
        for kind, refused in (("execute", "command"), ("read", "permissions"), ("edit", "file_change")):
            with self.subTest(kind=kind):
                response, did_work, observed = self.run_permission(kind=kind)
                self.assertEqual({"outcome": {"outcome": "selected", "optionId": "reject"}}, response)
                self.assertFalse(did_work)
                self.assert_refused_and_completed(observed, refused)
                self.assertEqual(f"{kind} the work file", observed["actions"]["refused"][0]["description"])

    def test_a_request_without_a_reject_option_is_cancelled_and_the_turn_ends(self):
        response, did_work, observed = self.run_permission(
            kind="execute", options=[{"optionId": "once", "kind": "allow_once", "name": "Allow once"}])
        self.assertEqual({"outcome": {"outcome": "cancelled"}}, response)
        self.assertFalse(did_work)
        self.assert_refused_and_completed(observed, "command")

    def test_a_path_outside_the_roots_is_refused_the_same_way(self):
        response, did_work, observed = self.run_permission(kind="read", outside=True)
        self.assertEqual({"outcome": {"outcome": "selected", "optionId": "reject"}}, response)
        self.assertFalse(did_work)
        self.assertNotIn("/etc/permission-target", json.dumps(observed))

    def test_no_environment_variable_makes_a_request_allowed(self):
        names = ("DEV_SKILLS_MCP_FS_READ", "DEV_SKILLS_MCP_FS_WRITE", "DEV_SKILLS_MCP_TERMINAL")
        with patch.dict(os.environ, {name: "1" for name in names}):
            for kind in ("execute", "edit"):
                with self.subTest(kind=kind):
                    response, did_work, _observed = self.run_permission(kind=kind)
                    self.assertEqual("reject", response["outcome"]["optionId"])
                    self.assertFalse(did_work)


if __name__ == "__main__":
    unittest.main()
