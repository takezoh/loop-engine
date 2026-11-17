"""The unit tools and the MCP entry: purpose-specific tools that only pass requests and read records."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from engine_support import SRC, EngineCase, approve, produced

from provider_transport.admitted_process import EXECUTION_ROLE_VARIABLE  # noqa: E402

from loop_engine import client, composition, mcp_stdio, store, unit_tools  # noqa: E402
from loop_engine.testing import FakeEngineClient  # noqa: E402
from loop_engine.unit_tools import Refused, UnitTools  # noqa: E402

MAIN = os.path.join(SRC, "loop_engine", "__main__.py")


class ConnectionCase(unittest.TestCase):
    """A connection directory with a repository, and records of units of this and another connection."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.top = Path(os.path.realpath(self._tmp.name))
        self.connection = self.top / "project"
        self.repository = str(self.connection / "repo")
        os.makedirs(self.repository)
        self.root = str(self.top / "home" / "records")
        # "gone" is recorded as this connection's, and the engine no longer knows it.
        for unit_id, repository in (("u1", self.repository), ("running", self.repository),
                                    ("gone", self.repository), ("theirs", str(self.top))):
            directory = store.unit_dir(self.root, unit_id)
            os.makedirs(directory)
            with open(os.path.join(directory, "contract.json"), "w", encoding="utf-8") as handle:
                json.dump({"unit_id": unit_id, "repository": repository}, handle)
        self.client = FakeEngineClient()
        for unit_id in ("u1", "running"):
            self.client.request(self.root, {"request": "start", "contract": {"unit_id": unit_id}}, environ={})
        self.client.stop("u1", "done", patch=b"x" * (unit_tools.MAX_RESULT_BYTES + 5))
        self.client.sent.clear()
        self.tools = UnitTools(self.connection, self.root, client=self.client, environ={})

    def contract(self, **changes):
        return {"unit_id": "new", "repository": self.repository, **changes}


class UnitToolsTest(ConnectionCase):
    def test_start_hands_the_contract_to_the_engine_unchanged(self):
        status = self.tools.invoke("unit_start", {"contract": self.contract(unit={"goal": "g"})})
        self.assertEqual({"state": "running", "revision": 0}, status)
        self.assertEqual([(self.root, {"request": "start", "contract": self.contract(unit={"goal": "g"})})],
                         self.client.sent)
        self.assertEqual(status, self.tools.start(self.contract()))

    def test_a_repository_outside_the_connection_is_refused_before_anything_is_sent(self):
        for repository in (str(self.top), str(self.connection / "missing"), "repo",
                           str(self.connection / "repo" / ".." / ".."), None, 3):
            with self.subTest(repository=repository), self.assertRaisesRegex(Refused, "inside this connection"):
                self.tools.invoke("unit_start", {"contract": self.contract(repository=repository)})
        with self.assertRaises(Refused):
            self.tools.start("not a contract")
        self.assertEqual([], self.client.sent)

    def test_start_refuses_an_id_held_by_another_connection_without_describing_it(self):
        with self.assertRaisesRegex(Refused, "^this unit_id cannot be used$"):
            self.tools.invoke("unit_start", {"contract": self.contract(unit_id="theirs")})
        self.assertEqual([], self.client.sent)
        self.tools.admit(self.repository, "u1")  # an id of this connection is the engine's to judge

    def test_requests_are_passed_through_and_an_engine_refusal_is_an_error(self):
        self.tools.invoke("unit_answer", {"unit_id": "u1", "question_id": "q-1", "answer": "yes"})
        self.tools.invoke("unit_retry", {"unit_id": "u1"})
        self.tools.invoke("unit_cancel", {"unit_id": "u1"})
        self.assertEqual([{"request": "answer", "unit_id": "u1", "question_id": "q-1", "answer": "yes"},
                          {"request": "retry", "unit_id": "u1"}, {"request": "cancel", "unit_id": "u1"}],
                         [payload for _root, payload in self.client.sent])
        with self.assertRaisesRegex(Refused, "no such unit"):
            self.tools.invoke("unit_retry", {"unit_id": "gone"})

    def test_status_wait_and_result_only_read_records(self):
        self.assertEqual("done", self.tools.invoke("unit_status", {"unit_id": "u1"})["stop_reason"])
        self.assertEqual("stopped",
                         self.tools.invoke("unit_wait", {"unit_id": "u1", "after_revision": 3, "timeout_ms": 1500})["state"])
        self.assertEqual([("u1", 3, 1.5)], self.client.waits)
        result = self.tools.invoke("unit_result", {"unit_id": "u1", "name": "patch.diff"})
        self.assertEqual((True, unit_tools.MAX_RESULT_BYTES), (result["truncated"], len(result["text"])))
        with self.assertRaises(Refused):
            self.tools.invoke("unit_result", {"unit_id": "running", "name": "stop.json"})
        self.assertEqual([], self.client.sent)

    def test_a_unit_of_another_connection_does_not_exist_for_this_one(self):
        for unit_id in ("theirs", "unknown"):
            for name, extra in (("unit_status", {}), ("unit_wait", {"timeout_ms": 0}), ("unit_retry", {}),
                                ("unit_cancel", {}), ("unit_answer", {"question_id": "q", "answer": "a"}),
                                ("unit_result", {"name": "patch.diff"})):
                with self.assertRaisesRegex(Refused, "no such unit"):
                    self.tools.invoke(name, {"unit_id": unit_id, **extra})
        self.assertEqual([], self.client.sent)

    def test_anything_outside_a_tools_schema_is_refused(self):
        for name, arguments in (
                ("unit_exec", {"argv": ["sh"]}), ("unit_status", {"unit_id": "../u1"}),
                ("unit_status", {"unit_id": "u1\n"}), ("unit_status", {}), ("unit_status", ["u1"]),
                ("unit_wait", {"unit_id": "u1", "timeout_ms": 60000}), ("unit_wait", {"unit_id": "u1", "timeout_ms": True}),
                ("unit_wait", {"unit_id": "u1", "timeout_ms": 1, "after_revision": -1}),
                ("unit_answer", {"unit_id": "u1", "question_id": "q", "answer": ""}),
                ("unit_result", {"unit_id": "u1", "name": "../../contract.json"}),
                ("unit_start", {"contract": self.contract(), "env": {"X": "1"}}),
                ("unit_start", {"contract": "{}"}), ("unit_cancel", {"unit_id": "u1", "path": "/x"})):
            with self.subTest(name=name, arguments=arguments), self.assertRaises(Refused):
                self.tools.invoke(name, arguments)
        self.assertEqual([], self.client.sent)

    def test_every_tool_declares_a_closed_schema_and_none_takes_a_program_or_a_place(self):
        for name, (description, declared) in unit_tools.TOOLS.items():
            self.assertTrue(description, name)
            self.assertIs(False, declared["additionalProperties"], name)
            self.assertFalse(set(declared["properties"]) & {"command", "argv", "env", "path", "home", "cwd"}, name)

    def test_an_agent_running_inside_a_unit_cannot_open_the_entry(self):
        home = {"LOOP_ENGINE_HOME": str(self.top / "home")}
        self.assertIsInstance(composition.unit_entry(home, self.connection), UnitTools)
        for role in ("producer", "", "other", "PRODUCER"):
            with self.subTest(role=role), self.assertRaisesRegex(PermissionError, "inside a unit cannot open"):
                composition.unit_entry({**home, EXECUTION_ROLE_VARIABLE: role}, self.connection)

    def test_the_entry_does_not_open_when_the_records_lie_inside_the_connection(self):
        with self.assertRaisesRegex(PermissionError, "outside the connection"):
            composition.unit_entry({"LOOP_ENGINE_HOME": str(self.connection / "state")}, self.connection)


def finishing(request, commands, cancelled):
    """A writer that makes a file and runs its checks, and reviewers that approve."""
    if "review" in request.role:
        return approve(request)
    return produced(request, commands)


class EngineClientContract:
    """What the unit tools rely on from an `EngineClient`."""

    def open_host(self):
        """The implementation under contract, for the port registry."""
        raise NotImplementedError

    def close_host(self, host):
        pass

    def finish(self, unit_id):
        """Let the started unit reach its stop."""

    def call(self, host, payload):
        return host.request(self.root, payload, environ=self.environ())

    def test_a_unit_nobody_started_has_no_status_and_no_result_and_its_requests_are_refused(self):
        host = self.open_host()
        self.assertIsNone(host.status(self.root, "absent"))
        self.assertIsNone(host.result(self.root, "absent", "stop.json"))
        self.assertIsNone(host.wait(self.root, "absent", after_revision=None, timeout=0, environ=self.environ()))
        for kind in ("retry", "cancel"):
            response = self.call(host, {"request": kind, "unit_id": "absent"})
            self.assertEqual((False, str), (response["ok"], type(response["error"])), kind)

    def test_a_started_unit_has_a_status_and_once_it_stopped_its_two_results(self):
        host = self.open_host()
        response = self.call(host, {"request": "start", "contract": self.contract()})
        self.assertIs(True, response["ok"], response)
        self.assertIn(response["result"]["state"], ("running", "stopped"))
        self.finish("u1")
        status = host.wait(self.root, "u1", after_revision=None, timeout=60, environ=self.environ())
        self.assertEqual("stopped", status["state"])
        self.assertEqual(status["stop_reason"], host.status(self.root, "u1")["stop_reason"])
        self.assertEqual(status["stop_reason"], json.loads(host.result(self.root, "u1", "stop.json"))["stop_reason"])
        self.assertIsInstance(host.result(self.root, "u1", "patch.diff"), bytes)

    def test_a_result_has_one_of_two_names_and_a_request_one_of_the_known_kinds(self):
        host = self.open_host()
        with self.assertRaises(ValueError):
            host.result(self.root, "u1", "../../contract.json")
        response = self.call(host, {"request": "exec", "argv": ["sh"]})
        self.assertEqual((False, str), (response["ok"], type(response["error"])))


class FakeEngineClientContractTest(EngineClientContract, unittest.TestCase):
    root = "records"

    def setUp(self):
        self.host = FakeEngineClient()

    def open_host(self):
        return self.host

    def environ(self):
        return {}

    def contract(self):
        return {"unit_id": "u1"}

    def finish(self, unit_id):
        self.host.stop(unit_id, "done")


class EntryClientContractTest(EngineClientContract, EngineCase):
    """The entry's client, over an engine serving its state home with scripted agents."""

    def open_host(self):
        engine = self.engine(finishing, idle_seconds=60.0)
        thread = threading.Thread(target=lambda: engine.serve(client.lock_path(self.root)), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(engine._halting.set)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # requests go to this engine once it holds the state home
            probe = store.ProcessLock(client.lock_path(self.root))
            if not probe.acquire():
                break
            probe.release()
            time.sleep(0.01)
        return client

    def environ(self):
        return {**os.environ, "LOOP_ENGINE_HOME": self.home, "LOOP_ENGINE_WORKTREES": os.path.join(self.home, "worktrees")}


class McpStdioTest(ConnectionCase):
    def ask(self, method, params=None, request_id=1):
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        return mcp_stdio.respond(self.tools, message if params is None else {**message, "params": params})

    def test_initialize_offers_the_tools_and_the_clients_protocol_revision_when_it_is_known(self):
        result = self.ask("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})["result"]
        self.assertEqual(("2024-11-05", {"tools": {}}, unit_tools.INSTRUCTIONS),
                         (result["protocolVersion"], result["capabilities"], result["instructions"]))
        self.assertEqual("loop-engine", result["serverInfo"]["name"])
        unknown = self.ask("initialize", {"protocolVersion": "1999-01-01"})["result"]
        self.assertEqual(mcp_stdio.VERSIONS[0], unknown["protocolVersion"])

    def test_tools_are_listed_with_their_schemas(self):
        tools = self.ask("tools/list")["result"]["tools"]
        self.assertEqual(list(unit_tools.TOOLS), [tool["name"] for tool in tools])
        self.assertEqual(unit_tools.TOOLS["unit_wait"][1], tools[2]["inputSchema"])

    def test_a_call_returns_the_tools_value_and_a_refusal_is_a_tool_error(self):
        done = self.ask("tools/call", {"name": "unit_status", "arguments": {"unit_id": "u1"}})["result"]
        self.assertIs(False, done["isError"])
        self.assertEqual("done", json.loads(done["content"][0]["text"])["stop_reason"])
        refused = self.ask("tools/call", {"name": "unit_status", "arguments": {"unit_id": "theirs"}})["result"]
        self.assertEqual((True, "no such unit"), (refused["isError"], refused["content"][0]["text"]))
        self.assertIs(True, self.ask("tools/call", {"name": "unit_exec"})["result"]["isError"])

    def test_what_is_not_a_request_of_the_protocol_gets_an_error_or_no_answer(self):
        self.assertEqual(mcp_stdio.METHOD_NOT_FOUND, self.ask("resources/list")["error"]["code"])
        self.assertEqual(mcp_stdio.INVALID_PARAMS, self.ask("tools/call", {"arguments": {}})["error"]["code"])
        self.assertEqual(mcp_stdio.INVALID_PARAMS, self.ask("tools/call", {"name": "unit_status", "arguments": []})["error"]["code"])
        self.assertEqual(mcp_stdio.INVALID_REQUEST, mcp_stdio.respond(self.tools, [1])["error"]["code"])
        self.assertEqual(mcp_stdio.INVALID_REQUEST, mcp_stdio.respond(self.tools, {"id": 4})["error"]["code"])
        self.assertEqual({}, self.ask("ping")["result"])
        for silent in ({"jsonrpc": "2.0", "method": "notifications/initialized"},
                       {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "unit_cancel"}},
                       {"jsonrpc": "2.0", "id": 9, "result": {}}):
            self.assertIsNone(mcp_stdio.respond(self.tools, silent))

    def test_the_server_answers_one_message_per_line_until_the_reader_ends(self):
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                 json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}), "", "{not json",
                 json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                             "params": {"name": "unit_wait", "arguments": {"unit_id": "u1", "timeout_ms": 5}}})]
        writer = io.BytesIO()
        mcp_stdio.serve(self.tools, io.BytesIO("\n".join(lines).encode("utf-8") + b"\n"), writer)
        answers = [json.loads(line) for line in writer.getvalue().decode("utf-8").splitlines()]
        self.assertEqual({1, None, 2}, {answer["id"] for answer in answers})
        self.assertEqual(mcp_stdio.PARSE_ERROR, next(a for a in answers if a["id"] is None)["error"]["code"])
        waited = next(a for a in answers if a["id"] == 2)["result"]
        self.assertEqual("stopped", json.loads(waited["content"][0]["text"])["state"])
        self.assertEqual([("u1", None, 0.005)], self.client.waits)

    def run_entry(self, environ, text):
        base = {key: value for key, value in os.environ.items() if key != EXECUTION_ROLE_VARIABLE}
        return subprocess.run([sys.executable, "-I", MAIN, "mcp"], input=text.encode("utf-8"), capture_output=True,
                              cwd=str(self.connection), env={**base, **environ}, timeout=60)

    def test_the_mcp_entry_serves_the_connection_it_is_started_in_and_starts_no_engine_to_read(self):
        home = str(self.top / "home")
        text = "\n".join(json.dumps(message) for message in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "unit_status", "arguments": {"unit_id": "theirs"}}})) + "\n"
        done = self.run_entry({"LOOP_ENGINE_HOME": home}, text)
        self.assertEqual((0, b""), (done.returncode, done.stderr))
        answers = {answer["id"]: answer for answer in map(json.loads, done.stdout.decode("utf-8").splitlines())}
        self.assertEqual(list(unit_tools.TOOLS), [tool["name"] for tool in answers[2]["result"]["tools"]])
        self.assertEqual("no such unit", answers[3]["result"]["content"][0]["text"])
        self.assertEqual(["units"], os.listdir(self.root))  # no request, no lock: nothing started an engine

    def test_the_mcp_entry_does_not_open_for_an_agent_running_inside_a_unit(self):
        done = self.run_entry({"LOOP_ENGINE_HOME": str(self.top / "home"), EXECUTION_ROLE_VARIABLE: "producer"},
                              json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
        self.assertEqual((2, b""), (done.returncode, done.stdout))
        self.assertIn(b"inside a unit cannot open", done.stderr)


if __name__ == "__main__":
    unittest.main()
