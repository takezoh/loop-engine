"""Shared conformance vectors: equivalent ACP and Codex event histories must
yield the same TurnResult version 1 vocabulary and values (identity excluded)."""

import unittest

from provider_transport.acp_client import (
    AcpBinding,
    AcpMessageCapture,
    AcpPromptError,
    AcpSessionClient,
)
from provider_transport.codex_app_server import (
    CodexAppServerAdapter,
    CodexTurnError,
    TransportBinding,
)


def _without_identity(result):
    wire = result.to_wire()
    wire.pop("turn_identity")
    return wire


class _AcpRpc:
    """Scripted ACP rpc emitting the given chunks during session/prompt."""

    def __init__(self, capture, chunks=(), fail=None):
        self.capture = capture
        self.chunks = chunks
        self.fail = fail

    def request(self, method, params, *, timeout=None):
        if method == "session/prompt":
            for chunk in self.chunks:
                self.capture.observe({
                    "method": "session/update",
                    "params": {
                        "sessionId": "s-1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": chunk},
                        },
                    },
                })
            if self.fail is not None:
                raise self.fail
            return {"stopReason": "end_turn"}
        raise AssertionError(method)

    def notify(self, method, params):
        pass


class _CodexRpc:
    """Scripted Codex rpc emitting the given deltas for the started turn."""

    def __init__(self, deltas=(), terminal_text=None, fail_wait=None):
        self.deltas = deltas
        self.terminal_text = terminal_text
        self.fail_wait = fail_wait
        self.agent_deltas = []

    def request(self, method, params, *, timeout=None):
        if method == "turn/start":
            self.agent_deltas = [
                {"threadId": "thr-1", "turnId": "turn-1", "delta": delta}
                for delta in self.deltas
            ]
            return {"turn": {"id": "turn-1"}}
        raise AssertionError(method)

    def wait_notification(self, method, *, predicate, timeout=None):
        if self.fail_wait is not None:
            raise self.fail_wait
        turn = {"id": "turn-1", "status": "completed"}
        if self.terminal_text is not None:
            turn["outputText"] = self.terminal_text
        return {"threadId": "thr-1", "turn": turn}

    def drain_notifications(self, method, *, predicate=lambda _: True):
        drained = [value for value in self.agent_deltas if predicate(value)]
        self.agent_deltas = [
            value for value in self.agent_deltas if value not in drained
        ]
        return drained


def _acp_turn(chunks=(), fail=None):
    capture = AcpMessageCapture()
    client = AcpSessionClient(
        _AcpRpc(capture, chunks, fail),
        provider="claude",
        generation="gen",
        capabilities={},
        message_capture=capture,
    )
    binding = AcpBinding("s-1", "claude", "gen")
    if fail is None:
        return client.prompt(binding, "go").turn_result
    try:
        client.prompt(binding, "go")
    except AcpPromptError as exc:
        return exc.turn_result
    raise AssertionError("prompt did not fail")


def _codex_turn(deltas=(), terminal_text=None, fail_wait=None):
    rpc = _CodexRpc(deltas, terminal_text, fail_wait)
    adapter = CodexAppServerAdapter(rpc, generation="gen")
    binding = TransportBinding("thr-1", "session-1", "gen")
    turn_id = adapter.continue_turn(binding, text="go")
    if fail_wait is None:
        return adapter.wait(binding, turn_id=turn_id).turn_result
    try:
        adapter.wait(binding, turn_id=turn_id)
    except CodexTurnError as exc:
        return exc.turn_result
    raise AssertionError("wait did not fail")


class TurnParityTests(unittest.TestCase):
    def test_present_completed_history_is_field_for_field_equal(self):
        acp = _acp_turn(chunks=("hello ", "world"))
        codex = _codex_turn(
            deltas=("hello ", "world"), terminal_text="hello world"
        )
        self.assertEqual(_without_identity(acp), _without_identity(codex))
        self.assertEqual("present", acp.communication.presence)

    def test_absent_completed_history_is_field_for_field_equal(self):
        acp = _acp_turn()
        codex = _codex_turn()
        self.assertEqual(_without_identity(acp), _without_identity(codex))
        self.assertEqual("absent", acp.communication.presence)

    def test_truncated_completed_history_is_field_for_field_equal(self):
        text = "頭" * 6_000 + "尾" * 6_000
        acp = _acp_turn(chunks=(text,))
        codex = _codex_turn(deltas=(text,), terminal_text=text)
        self.assertEqual(_without_identity(acp), _without_identity(codex))
        self.assertTrue(acp.communication.truncated)
        self.assertEqual("completed", acp.transport_evidence.completion)

    def test_started_timeout_history_yields_identical_loss_vocabulary(self):
        acp = _acp_turn(chunks=("partial",), fail=TimeoutError("lost"))
        codex = _codex_turn(deltas=("partial",), fail_wait=TimeoutError("lost"))
        self.assertEqual(_without_identity(acp), _without_identity(codex))
        self.assertEqual(("turn_timeout",), acp.transport_evidence.loss_reasons)
        self.assertEqual("incomplete", acp.transport_evidence.completion)
        self.assertEqual("partial", acp.communication.text)


if __name__ == "__main__":
    unittest.main()
