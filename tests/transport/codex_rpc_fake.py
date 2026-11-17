"""A Codex App Server JSON-RPC fake that keeps threads, turns and their notifications."""
import os
from threading import Condition, Event, Thread
import time


class FakeProcess:
    def __init__(self):
        self.dead = False

    def poll(self):
        return 1 if self.dead else None


class FakeCodexRpc:
    def __init__(self, admission):
        self.process = FakeProcess()
        self.calls = []
        self.notifications = []
        self._condition = Condition()
        self._thread_sequence = 0
        self._turn_sequence = 0
        self._turns = {}
        self._thread_turns = {}
        self._reservation = admission.reservation
        self._closed = False
        self.close_notification_gate = None
        self.unsubscribe_started = Event()
        admission.consume(expected_cell_id="cell")
        self._receipt = admission.spawned(os.getpid())
        self.notification_sink = None

    def request(self, method, params):
        with self._condition:
            self.calls.append((method, dict(params)))
            if method == "thread/start":
                self._thread_sequence += 1
                thread_id = f"thread-{self._thread_sequence}"
                return {"thread": {"id": thread_id, "sessionId": f"session-{thread_id}"}}
            if method == "thread/resume":
                thread_id = params["threadId"]
                return {"thread": {"id": thread_id, "sessionId": f"session-{thread_id}"}}
            if method == "turn/start":
                self._turn_sequence += 1
                turn_id = f"turn-{self._turn_sequence}"
                self._turns[turn_id] = params["threadId"]
                self._thread_turns[params["threadId"]] = turn_id
                return {"turn": {"id": turn_id}}
            if method == "turn/interrupt":
                turn_id = params["turnId"]
                self._publish_locked({
                    "method": "turn/completed",
                    "params": {
                        "threadId": params["threadId"],
                        "turn": {"id": turn_id, "status": "interrupted"},
                    },
                })
                return {}
            if method == "thread/unsubscribe":
                self.unsubscribe_started.set()
                notification = {
                    "method": "thread/closed",
                    "params": {"threadId": params["threadId"]},
                }
                gate = self.close_notification_gate
                if gate is None:
                    self._publish_locked(notification)
                else:
                    def delayed_publish():
                        gate.wait()
                        with self._condition:
                            self._publish_locked(notification)
                    Thread(target=delayed_publish, daemon=True).start()
                return {}
            raise AssertionError(f"unexpected Codex method {method}")

    def wait_notification(self, method, *, predicate, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._condition:
            while True:
                if self.process.dead:
                    raise OSError("fake-host-dead")
                for index, message in enumerate(self.notifications):
                    if message.get("method") == method and predicate(message["params"]):
                        return self.notifications.pop(index)["params"]
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(method)
                    self._condition.wait(remaining)
                else:
                    self._condition.wait(0.05)

    def drain_notifications(self, method, *, predicate=lambda _params: True):
        with self._condition:
            selected = [
                message["params"] for message in self.notifications
                if message.get("method") == method and predicate(message["params"])
            ]
            self.notifications = [
                message for message in self.notifications
                if not (message.get("method") == method and predicate(message["params"]))
            ]
            return selected

    def finish(self, thread_id, status="completed"):
        with self._condition:
            turn_id = self._thread_turns[thread_id]
            self._publish_locked({
                "method": "turn/completed",
                "params": {
                    "threadId": thread_id,
                    "turn": {"id": turn_id, "status": status},
                },
            })

    def die(self):
        with self._condition:
            self.process.dead = True
            self._condition.notify_all()

    def _publish_locked(self, message):
        sink = self.notification_sink
        if sink is not None and sink(message):
            return
        self.notifications.append(message)
        self._condition.notify_all()

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.die()
        self._reservation.release_live(self._receipt, certain=True)
