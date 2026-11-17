"""A fake Pi RPC peer: answers `pi --mode rpc` commands over in-memory pipes.

It reads the session id, model and thinking level from the launch argv,
answers `get_state`, `prompt` and `abort`, and streams the events a run emits.
A held run waits until the test releases it or the client aborts it. A run
given `dialogs` sends each extension UI request and, for a dialog, waits for
the client's `extension_ui_response` before it goes on, as Pi blocks on one.
"""
from __future__ import annotations

import json
import os
from threading import Event, Thread

from provider_transport.pi_rpc import PiRpcClient


class FakePi:
    def __init__(self, command, *, hold=False, reject_state=False, text="hello world", stop="stop", dialogs=()):
        self.command = tuple(command)
        self.hold = hold
        self.reject_state = reject_state
        self.text = text
        self.stop = stop
        self.release = Event()
        self.aborted = Event()
        self.prompts: list[str] = []
        self.dialogs = list(dialogs)
        self.ui_responses: list[dict] = []
        self._answered = Event()
        self.closed = Event()
        to_peer_read, to_peer_write = os.pipe()
        from_peer_read, from_peer_write = os.pipe()
        self._peer_in = os.fdopen(to_peer_read, "rb")
        self._peer_out = os.fdopen(from_peer_write, "wb")
        self.client = PiRpcClient(
            stdin=os.fdopen(to_peer_write, "wb"), stdout=os.fdopen(from_peer_read, "rb"), request_timeout=5.0
        )
        Thread(target=self._serve, daemon=True).start()

    def _arg(self, name):
        if name in self.command:
            return self.command[self.command.index(name) + 1]
        return None

    def _write(self, record):
        self._peer_out.write(json.dumps(record).encode("utf-8") + b"\n")
        self._peer_out.flush()

    def _respond(self, request, data=None, *, success=True, error=None):
        record = {"id": request.get("id"), "type": "response", "command": request["type"], "success": success}
        if data is not None:
            record["data"] = data
        if error is not None:
            record["error"] = error
        self._write(record)

    def _run(self):
        self._write({"type": "agent_start"})
        self._write({"type": "message_update", "assistantMessageEvent": {
            "type": "text_delta", "contentIndex": 0, "delta": self.text[:6]}})
        for dialog in self.dialogs:
            self._answered.clear()
            self._write({"type": "extension_ui_request", **dialog})
            if dialog.get("method") in ("select", "confirm", "input", "editor") and not self._answered.wait(5):
                return  # Pi would wait on the dialog forever: the run never settles
        if self.hold:
            while not self.release.wait(0.01):
                if self.aborted.is_set():
                    self._write({"type": "agent_settled", "aborted": True})
                    return
        self._write({"type": "message_end", "message": {
            "role": "assistant", "content": [{"type": "text", "text": self.text}], "stopReason": self.stop}})
        self._write({"type": "agent_end", "messages": [], "willRetry": False})
        self._write({"type": "agent_settled", "aborted": False})

    def _serve(self):
        try:
            for raw in iter(self._peer_in.readline, b""):
                request = json.loads(raw)
                kind = request["type"]
                if kind == "get_state":
                    if self.reject_state:
                        self._respond(request, success=False, error="no model")
                        continue
                    model = self._arg("--model")
                    self._respond(request, {
                        "sessionId": self._arg("--session-id"),
                        "model": {"id": model, "provider": "fake"} if model else None,
                        "thinkingLevel": self._arg("--thinking") or "medium",
                    })
                elif kind == "prompt":
                    self.prompts.append(request["message"])
                    self._respond(request, {"disposition": "started"})
                    Thread(target=self._run, daemon=True).start()
                elif kind == "extension_ui_response":
                    self.ui_responses.append(request)
                    self._answered.set()
                elif kind == "abort":
                    self.aborted.set()
                    self._respond(request)
                else:
                    self._respond(request, success=False, error=f"unexpected {kind}")
        finally:
            self.closed.set()
            self._peer_out.close()
