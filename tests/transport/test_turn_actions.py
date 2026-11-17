"""Every wire protocol reports what the agent did in one provider-neutral record.

The event shapes below are the ones each provider sent in a live turn on
2026-11-14, reduced to the fields the drivers read.
"""
from __future__ import annotations

import unittest

from provider_transport.acp_client import AcpBinding, AcpMessageCapture, record_tool_update
from provider_transport.codex_app_server import (
    CodexAppServerAdapter, TransportBinding, record_item, shell_script,
)
from provider_transport.pi_driver import _PiRun, record_tool_event, turn_end
from provider_transport.turn_actions import (
    MAX_COMMAND_BYTES, MAX_ENTRIES, MAX_OUTPUT_TAIL_BYTES, ActionLog,
)


def entries(log):
    wire = log.to_wire() if isinstance(log, ActionLog) else log
    return [
        (entry["kind"], entry["command"], entry["output_tail"], entry["exit_code"], entry["ended"])
        for entry in wire["entries"]
    ]


class ActionLogTest(unittest.TestCase):
    def test_entries_keep_start_order_and_take_later_facts(self):
        log = ActionLog()
        log.begin("a", "command", command="true")
        log.begin("b", "edit", paths=("x.py",))
        log.begin("a", None, command="true --flag")
        log.end("b")
        log.end("a", output="done\n", exit_code=0)
        wire = log.to_wire()
        self.assertEqual([1, 2], [entry["seq"] for entry in wire["entries"]])
        self.assertEqual(
            [("command", "true --flag", "done\n", 0, True), ("edit", None, None, None, True)], entries(wire))
        self.assertEqual(["x.py"], wire["entries"][1]["paths"])

    def test_unknown_kind_and_unseen_call_never_read_as_command(self):
        log = ActionLog()
        log.begin("a", "shell-ish", command="true")
        log.begin("b", None)
        log.end("never-started", output="x", exit_code=0)
        self.assertEqual(["other", "other"], [entry["kind"] for entry in log.to_wire()["entries"]])

    def test_only_the_latest_entries_are_held_and_the_loss_is_counted(self):
        log = ActionLog()
        for index in range(MAX_ENTRIES + 3):
            log.begin(f"call-{index}", "command", command=f"c{index}")
        wire = log.to_wire()
        self.assertEqual(3, wire["dropped"])
        self.assertEqual(MAX_ENTRIES, len(wire["entries"]))
        self.assertEqual("c3", wire["entries"][0]["command"])
        self.assertEqual(MAX_ENTRIES + 3, wire["entries"][-1]["seq"])

    def test_long_command_is_cut_and_marked_and_output_keeps_its_end(self):
        log = ActionLog()
        log.begin("a", "command", command="x" * (MAX_COMMAND_BYTES + 1))
        log.end("a", output="head\n" + "y" * MAX_OUTPUT_TAIL_BYTES + "\nlast line")
        entry = log.to_wire()["entries"][0]
        self.assertTrue(entry["command_truncated"])
        self.assertEqual(MAX_COMMAND_BYTES, len(entry["command"]))
        self.assertTrue(entry["output_truncated"])
        self.assertTrue(entry["output_tail"].endswith("\nlast line"))

    def test_recorded_output_survives_an_end_that_asks_to_keep_it(self):
        log = ActionLog()
        log.begin("a", "command", command="c")
        log.output("a", "real stdout")
        log.end("a", output="(rendered for the model)", keep_recorded_output=True)
        self.assertEqual("real stdout", log.to_wire()["entries"][0]["output_tail"])


class CodexActionsTest(unittest.TestCase):
    def item(self, method, **fields):
        return method, {"threadId": "thread-1", "turnId": "turn-1", "item": {
            "type": "commandExecution", "id": "exec-1", "command": "/usr/bin/zsh -lc false",
            "aggregatedOutput": None, "exitCode": None, **fields}}

    def test_shell_wrapper_is_removed_and_other_shapes_are_kept(self):
        self.assertEqual("false", shell_script("/usr/bin/zsh -lc false"))
        self.assertEqual("ls ~/.ssh > /dev/null; echo rc=$?",
                         shell_script("/usr/bin/zsh -lc 'ls ~/.ssh > /dev/null; echo rc=$?'"))
        self.assertEqual("echo hi", shell_script(["bash", "-lc", "echo hi"]))
        self.assertEqual("git status", shell_script("git status"))
        self.assertEqual("echo 'unterminated", shell_script("echo 'unterminated"))
        self.assertIsNone(shell_script(None))

    def test_command_item_carries_script_output_and_exit_code(self):
        log = ActionLog()
        record_item(log, *self.item("item/started"))
        record_item(log, *self.item("item/completed", aggregatedOutput="rc=0\n", exitCode=1))
        self.assertEqual([("command", "false", "rc=0\n", 1, True)], entries(log))

    def test_file_change_is_an_edit_and_conversation_items_are_not_actions(self):
        log = ActionLog()
        for item_type in ("userMessage", "agentMessage", "reasoning"):
            record_item(log, "item/completed", {"item": {"type": item_type, "id": item_type}})
        record_item(log, "item/completed", {"item": {
            "type": "fileChange", "id": "patch-1", "changes": [{"path": "a.txt", "kind": "add"}]}})
        record_item(log, "item/started", {"item": {"type": "mcpToolCall", "id": "mcp-1"}})
        wire = log.to_wire()
        self.assertEqual(["edit", "other"], [entry["kind"] for entry in wire["entries"]])
        self.assertEqual(["a.txt"], wire["entries"][0]["paths"])
        self.assertEqual("mcpToolCall", wire["entries"][1]["name"])

    def test_adapter_hands_the_turn_actions_over_once_and_skips_sealed_turns(self):
        class Rpc:
            def request(self, method, params):
                return {"turn": {"id": "turn-1"}}
        adapter = CodexAppServerAdapter(Rpc(), generation="g")
        binding = TransportBinding("thread-1", "thread-1", "g")
        self.assertIsNone(adapter.take_actions())
        adapter.continue_turn(binding, text="go")
        method, params = self.item("item/completed", exitCode=0)
        self.assertFalse(adapter._sink_notification({"method": method, "params": params}))
        adapter._sealed_turn_ids.append("turn-0")
        adapter._sink_notification({"method": method, "params": {
            **params, "turnId": "turn-0", "item": {**params["item"], "id": "late"}}})
        adapter._sink_notification({"method": method, "params": {**params, "threadId": "thread-2"}})
        self.assertEqual([("command", "false", None, 0, True)], entries(adapter.take_actions()))
        self.assertIsNone(adapter.take_actions())


class AcpActionsTest(unittest.TestCase):
    UPDATES = (
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "status": "pending", "title": "Terminal",
         "kind": "execute", "_meta": {"claudeCode": {"toolName": "Bash"}}},
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "kind": "execute", "title": "run check",
         "rawInput": {"command": "echo rc=$?"}},
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1",
         "_meta": {"claudeCode": {"toolName": "Bash", "toolResponse": {"stdout": "rc=0", "stderr": ""}}}},
        {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed", "rawOutput": "rc=0"},
    )

    def test_command_text_and_stdout_arrive_across_updates(self):
        log = ActionLog()
        for update in self.UPDATES:
            record_tool_update(log, update)
        self.assertEqual([("command", "echo rc=$?", "rc=0", None, True)], entries(log))

    def test_stdout_wins_over_the_text_rendered_for_the_model(self):
        log = ActionLog()
        record_tool_update(log, {"toolCallId": "t1", "kind": "execute", "rawInput": {"command": "false"}})
        record_tool_update(log, {"toolCallId": "t1", "_meta": {"claudeCode": {"toolResponse": {"stdout": ""}}}})
        record_tool_update(log, {"toolCallId": "t1", "status": "completed",
                                 "rawOutput": "(Bash completed with no output)"})
        self.assertEqual([("command", "false", "", None, True)], entries(log))

    def test_edit_keeps_paths_and_a_failed_call_still_ends(self):
        log = ActionLog()
        record_tool_update(log, {"toolCallId": "t2", "kind": "edit", "title": "Write a.txt",
                                 "locations": [{"path": "/work/a.txt"}]})
        record_tool_update(log, {"toolCallId": "t2", "status": "failed", "rawOutput": "Tool use aborted"})
        record_tool_update(log, {"toolCallId": "t3", "kind": "search"})
        record_tool_update(log, {"toolCallId": "t4", "kind": "switch_mode"})
        wire = log.to_wire()
        self.assertEqual(["edit", "read", "other"], [entry["kind"] for entry in wire["entries"]])
        self.assertEqual(["/work/a.txt"], wire["entries"][0]["paths"])
        self.assertEqual((True, "Tool use aborted"), (wire["entries"][0]["ended"], wire["entries"][0]["output_tail"]))

    def test_capture_keeps_actions_per_session_until_taken(self):
        capture = AcpMessageCapture()
        binding = AcpBinding("s1", "claude", "g")
        other = AcpBinding("s2", "claude", "g")
        capture.begin(binding, 1)
        capture.begin(other, 1)
        for update in self.UPDATES:
            capture.observe({"method": "session/update", "params": {"sessionId": "s1", "update": update}})
        capture.finish(completed=True, binding=binding)
        self.assertEqual([], entries(capture.take_actions(other)))
        self.assertEqual([("command", "echo rc=$?", "rc=0", None, True)], entries(capture.take_actions(binding)))
        self.assertIsNone(capture.take_actions(binding))


class PiActionsTest(unittest.TestCase):
    START = {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "bash",
             "args": {"command": "false", "timeout": None}}
    END = {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "bash", "isError": True, "result": {
        "content": [{"type": "text", "text": "(no output)\n\nCommand exited with code 1"}],
        "structuredContent": {"output": "", "exit_code": 1}}}

    def test_bash_carries_structured_output_and_exit_code(self):
        log = ActionLog()
        record_tool_event(log, self.START)
        record_tool_event(log, self.END)
        self.assertEqual([("command", "false", "", 1, True)], entries(log))

    def test_tools_are_classified_and_host_execution_is_never_a_command(self):
        log = ActionLog()
        for index, (name, args) in enumerate((
            ("read", {"path": "a.md"}), ("write", {"path": "b.md"}),
            ("host_exec", {"command": "touch /outside"}), ("custom", {}),
        )):
            record_tool_event(log, {"type": "tool_execution_start", "toolCallId": f"c{index}",
                                    "toolName": name, "args": args})
        record_tool_event(log, {"type": "tool_execution_end", "toolCallId": "c2", "result": {
            "content": [{"type": "text", "text": "denied"}]}})
        wire = log.to_wire()
        self.assertEqual(["read", "edit", "other", "other"], [entry["kind"] for entry in wire["entries"]])
        self.assertEqual(["b.md"], wire["entries"][1]["paths"])
        self.assertEqual((None, "denied", "host_exec"), (
            wire["entries"][2]["command"], wire["entries"][2]["output_tail"], wire["entries"][2]["name"]))

    def test_run_records_tool_events_and_the_turn_end_carries_them(self):
        run = _PiRun("s:op", None)
        run.on_event(self.START)
        run.on_event(self.END)
        end = turn_end(identity="s:op", final={"role": "assistant", "stopReason": "stop", "content": []},
                       aborted=False, transport_lost=False, actions=run.actions)
        self.assertEqual([("command", "false", "", 1, True)], entries(end.detail["actions"]))


if __name__ == "__main__":
    unittest.main()
