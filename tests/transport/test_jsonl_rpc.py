import json
from queue import Empty, Queue
from threading import Barrier, Event, Lock, Thread
from time import monotonic
import unittest
from unittest.mock import patch

from provider_transport.cwd_authority import ChildLedger, SpawnAdmission
from provider_transport.jsonl_rpc import JsonlRpcClient, NO_TIMEOUT
from provider_transport.testing.cell_fake import FakeCellMembership
from provider_transport.testing.directory_fake import FakeCwdAuthority


class QueueReader:
    def __init__(self) -> None:
        self.lines: Queue[str | None] = Queue()

    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line


class RecordingWriter:
    def __init__(self) -> None:
        self.messages: Queue[dict[str, object]] = Queue()

    def write(self, value: str) -> int:
        self.messages.put(json.loads(value))
        return len(value)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


class BarrierWriter(RecordingWriter):
    def __init__(self, write_started: Barrier, allow_write: Event) -> None:
        super().__init__()
        self.write_started = write_started
        self.allow_write = allow_write

    def write(self, value: str) -> int:
        self.write_started.wait(timeout=1)
        if not self.allow_write.wait(timeout=1):
            raise TimeoutError("test did not release the scripted stdin write")
        return super().write(value)


def admission() -> SpawnAdmission:
    cell = FakeCellMembership()
    return SpawnAdmission(
        reservation=ChildLedger(cell_id="cell", cell=cell).reserve(),
        cwd_lease=FakeCwdAuthority().admit("."), cell_id="cell", epoch=0,
        fence_check=lambda _epoch: True, cell_membership=cell,
    )


class FakeProcess:
    pid = 4242

    def __init__(self, *, stdin: RecordingWriter, stdout: QueueReader) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls: list[float | None] = []
        self.reaped = False

    def poll(self) -> int | None:
        return 0 if self.reaped else None

    def terminate(self) -> None:
        self.terminate_calls += 1

    def kill(self) -> None:
        self.kill_calls += 1

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls.append(timeout)
        self.reaped = True
        return 0


class CloseRecordingWriter(RecordingWriter):
    def __init__(self) -> None:
        super().__init__()
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class ScriptedTransportClosedError(OSError):
    pass


class InitializeSentinel(BaseException):
    pass


class PreSendRejectModel(JsonlRpcClient):
    """Test-side model of a durable terminal admission check."""

    def __init__(self, **kwargs) -> None:
        self._model_terminal = Event()
        super().__init__(**kwargs)

    def request(self, method, params):
        if self._model_terminal.is_set():
            raise ScriptedTransportClosedError("scripted stdout reached EOF")
        return super().request(method, params)

    def _read_loop(self) -> None:
        super()._read_loop()
        self._model_terminal.set()


class PendingDrainOnlyModel(JsonlRpcClient):
    """Test-side countermodel that drains only requests present at EOF."""

    def _transition_terminal(self, error: OSError) -> None:
        for request_id, pending in list(self._pending.items()):
            self._pending.pop(request_id, None)
            pending.put(error)


class BlockingSetItemDict(dict):
    """Dict-compatible hook immediately before a pending registration commits."""

    def __init__(self) -> None:
        super().__init__()
        self.registration_started = Event()
        self.resume_registration = Event()

    def __setitem__(self, key, value) -> None:
        self.registration_started.set()
        if not self.resume_registration.wait(timeout=1):
            raise TimeoutError("test did not resume pending registration")
        super().__setitem__(key, value)


class NonAtomicCheckThenRegisterModel(JsonlRpcClient):
    """Countermodel whose terminal pre-check is not atomic with registration."""

    recheck_under_terminal_owner = False

    def __init__(self, **kwargs) -> None:
        self.eof_processed = Event()
        self.drained_at_eof = -1
        self._model_terminal = Event()
        self._terminal_owner = Lock()
        super().__init__(**kwargs)
        self._pending = BlockingSetItemDict()

    def request(self, method, params):
        if self._model_terminal.is_set():
            raise ScriptedTransportClosedError("scripted stdout reached EOF")
        response_queue = self._register_and_send(method, params)
        try:
            response = response_queue.get(timeout=self.request_timeout)
        except Empty as exc:
            for request_id, pending in list(self._pending.items()):
                if pending is response_queue:
                    self._pending.pop(request_id, None)
            raise TimeoutError(f"scripted request timed out: {method}") from exc
        if isinstance(response, BaseException):
            raise response
        return response

    def _register_and_send(self, method, params):
        with self._write_lock:
            request_id = self._next_id
            self._next_id += 1
            response_queue = Queue(maxsize=1)
            self._pending[request_id] = response_queue
            if self.recheck_under_terminal_owner:
                with self._terminal_owner:
                    if self._model_terminal.is_set():
                        self._pending.pop(request_id, None)
                        raise ScriptedTransportClosedError(
                            "scripted stdout reached EOF"
                        )
                    self._send(
                        {"method": method, "id": request_id, "params": dict(params)}
                    )
            else:
                self._send(
                    {"method": method, "id": request_id, "params": dict(params)}
                )
        return response_queue

    def _read_loop(self) -> None:
        super()._read_loop()
        with self._terminal_owner:
            self._model_terminal.set()
            pending_at_eof = list(self._pending.items())
            self.drained_at_eof = len(pending_at_eof)
            for request_id, pending in pending_at_eof:
                self._pending.pop(request_id, None)
                pending.put(ScriptedTransportClosedError("scripted stdout reached EOF"))
        self.eof_processed.set()


class CommonTerminalOwnerModel(NonAtomicCheckThenRegisterModel):
    """Model that rechecks terminal state under the transition owner."""

    recheck_under_terminal_owner = True


class JsonlRpcClientTests(unittest.TestCase):
    def test_request_uses_per_call_timeout_without_changing_default(self) -> None:
        client = JsonlRpcClient(
            stdin=RecordingWriter(), stdout=QueueReader(), request_timeout=1.0
        )

        started = monotonic()
        with self.assertRaisesRegex(TimeoutError, "session/close"):
            client.request("session/close", {"sessionId": "s"}, timeout=0.01)

        self.assertLess(monotonic() - started, 0.2)
        self.assertEqual(1.0, client.request_timeout)

    def test_no_timeout_request_waits_past_the_control_deadline(self) -> None:
        client = JsonlRpcClient(
            stdin=RecordingWriter(), stdout=QueueReader(), request_timeout=0.05
        )
        writer = client.stdin
        result: list[dict[str, object]] = []

        def call() -> None:
            result.append(
                dict(client.request("session/prompt", {"sessionId": "s"}, timeout=NO_TIMEOUT))
            )

        thread = Thread(target=call)
        thread.start()
        request = writer.messages.get(timeout=1)
        # Deliberately outlive the bounded control timeout before answering.
        thread.join(timeout=0.2)
        self.assertTrue(thread.is_alive())
        client.stdout.lines.put(
            json.dumps({"id": request["id"], "result": {"stopReason": "end_turn"}})
        )
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual([{"stopReason": "end_turn"}], result)

    def test_default_request_stays_bounded_by_control_timeout(self) -> None:
        client = JsonlRpcClient(
            stdin=RecordingWriter(), stdout=QueueReader(), request_timeout=0.05
        )
        with self.assertRaises(TimeoutError):
            client.request("session/new", {"cwd": "."})

    def test_initialize_opts_into_experimental_thread_settings_api(self) -> None:
        client = object.__new__(JsonlRpcClient)
        calls: list[tuple[str, dict[str, object]]] = []
        notifications: list[tuple[str, dict[str, object]]] = []
        client.request = lambda method, params: (
            calls.append((method, dict(params))) or {"userAgent": "test"}
        )
        client.notify = lambda method, params: notifications.append(
            (method, dict(params))
        )

        result = client.initialize()

        self.assertEqual({"userAgent": "test"}, result)
        self.assertTrue(calls[0][1]["capabilities"]["experimentalApi"])
        self.assertEqual([("initialized", {})], notifications)

    def setUp(self) -> None:
        self.reader = QueueReader()
        self.writer = RecordingWriter()
        self.client = JsonlRpcClient(
            stdin=self.writer,
            stdout=self.reader,
            request_timeout=1,
        )

    def tearDown(self) -> None:
        self.reader.lines.put(None)
        self.client.close()

    def test_request_correlates_response_and_notification(self) -> None:
        result: list[dict[str, object]] = []

        def call() -> None:
            result.append(dict(self.client.request("thread/read", {"threadId": "t"})))

        thread = Thread(target=call)
        thread.start()
        request = self.writer.messages.get(timeout=1)
        self.reader.lines.put(
            json.dumps(
                {
                    "method": "turn/completed",
                    "params": {"threadId": "t", "turn": {"id": "turn-1"}},
                }
            )
            + "\n"
        )
        self.reader.lines.put(
            json.dumps({"id": request["id"], "result": {"thread": {"id": "t"}}})
            + "\n"
        )
        thread.join(timeout=1)

        self.assertEqual("t", result[0]["thread"]["id"])
        notification = self.client.wait_notification("turn/completed")
        self.assertEqual("t", notification["threadId"])

    def test_server_requests_are_rejected_without_entering_transcript(self) -> None:
        self.reader.lines.put(
            json.dumps(
                {
                    "id": 88,
                    "method": "item/commandExecution/requestApproval",
                    "params": {},
                }
            )
            + "\n"
        )
        response = self.writer.messages.get(timeout=1)
        self.assertEqual(88, response["id"])
        self.assertEqual(-32601, response["error"]["code"])

    def test_drain_preserves_notification_read_order(self) -> None:
        for delta in ("one", "two"):
            self.reader.lines.put(json.dumps({
                "method": "item/agentMessage/delta",
                "params": {"threadId": "t", "turnId": "turn-1", "delta": delta},
            }) + "\n")
        import time
        deadline = time.monotonic() + 1
        drained = []
        while len(drained) < 2 and time.monotonic() < deadline:
            drained.extend(
                self.client.drain_notifications("item/agentMessage/delta")
            )
        self.assertEqual(["one", "two"], [item["delta"] for item in drained])

    def test_malformed_frames_emit_only_bounded_loss_facts(self) -> None:
        losses = []
        self.client.active_turn_loss_handler = losses.append
        self.reader.lines.put("not-json\n")
        self.reader.lines.put("[]\n")
        import time
        deadline = time.monotonic() + 1
        while len(losses) < 2 and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(["malformed_frame", "malformed_frame"], losses)

    def test_notification_sink_consumes_frames_without_queueing(self) -> None:
        consumed = []
        self.client.notification_sink = lambda message: (
            consumed.append(message), True
        )[1]
        self.reader.lines.put(json.dumps({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "t", "turnId": "turn-1", "delta": "streamed"},
        }) + "\n")
        import time
        deadline = time.monotonic() + 1
        while not consumed and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual("streamed", consumed[0]["params"]["delta"])
        self.assertEqual(
            [], self.client.drain_notifications("item/agentMessage/delta")
        )

    def test_notification_sink_crash_drops_frame_and_notes_loss(self) -> None:
        losses = []
        self.client.active_turn_loss_handler = losses.append

        def broken_sink(message):
            raise RuntimeError("sink bug")

        self.client.notification_sink = broken_sink
        self.reader.lines.put(json.dumps({
            "method": "item/agentMessage/delta",
            "params": {"threadId": "t", "turnId": "turn-1", "delta": "lost"},
        }) + "\n")
        import time
        deadline = time.monotonic() + 1
        while not losses and time.monotonic() < deadline:
            time.sleep(0.001)
        self.assertEqual(["internal_contract_violation"], losses)
        self.assertEqual(
            [], self.client.drain_notifications("item/agentMessage/delta")
        )

    def test_stdout_eof_fails_outstanding_request_without_waiting_for_timeout(self) -> None:
        failures: list[BaseException] = []

        def call() -> None:
            try:
                self.client.request("session/prompt", {"sessionId": "session-1"})
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=call)
        thread.start()
        self.writer.messages.get(timeout=1)

        # Scripted peer closes stdout while session/prompt is outstanding.
        self.reader.lines.put(None)
        thread.join(timeout=0.1)

        self.assertFalse(
            thread.is_alive(),
            "stdout EOF must complete outstanding requests before request_timeout",
        )
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], OSError)
        self.assertNotIsInstance(failures[0], TimeoutError)

    def test_request_after_processed_stdout_eof_is_rejected_without_pending_leak(
        self,
    ) -> None:
        observation = self._observe_request_after_processed_eof(
            self.client, self.reader, self.writer
        )

        self.assertEqual(
            {
                "completed_before_timeout": True,
                "is_transport_error": True,
                "is_timeout_error": False,
                "message_sent": False,
                "pending_count": 0,
            },
            observation,
        )

    def test_post_eof_request_contract_accepts_pre_send_reject_and_rejects_drain_only(
        self,
    ) -> None:
        expected = {
            "completed_before_timeout": True,
            "is_transport_error": True,
            "is_timeout_error": False,
            "message_sent": False,
            "pending_count": 0,
        }

        reject_observation = self._observe_post_eof_model(PreSendRejectModel)
        drain_only_observation = self._observe_post_eof_model(PendingDrainOnlyModel)

        self.assertEqual(expected, reject_observation)
        self.assertNotEqual(expected, drain_only_observation)
        self.assertEqual(
            {
                "completed_before_timeout": False,
                "is_transport_error": True,
                "is_timeout_error": True,
                "message_sent": True,
                "pending_count": 0,
            },
            drain_only_observation,
        )

    def test_notify_after_processed_stdout_eof_is_rejected_without_write(self) -> None:
        self.reader.lines.put(None)
        self.client._reader.join(timeout=0.1)
        self.assertFalse(self.client._reader.is_alive())

        caught: BaseException | None = None
        try:
            self.client.notify("session/cancel", {"sessionId": "after-eof"})
        except BaseException as exc:
            caught = exc

        self.assertIsInstance(caught, OSError)
        self.assertNotIsInstance(caught, TimeoutError)
        self.assertTrue(self.writer.messages.empty())

    def test_wait_notification_after_processed_stdout_eof_fails_immediately(
        self,
    ) -> None:
        self.reader.lines.put(None)
        self.client._reader.join(timeout=0.1)
        self.assertFalse(self.client._reader.is_alive())

        caught: BaseException | None = None
        started_at = monotonic()
        try:
            self.client.wait_notification("turn/completed", timeout=0.25)
        except BaseException as exc:
            caught = exc
        completed_before_timeout = monotonic() - started_at < 0.1

        self.assertIsInstance(caught, OSError)
        self.assertNotIsInstance(caught, TimeoutError)
        self.assertTrue(completed_before_timeout)

    def test_close_after_processed_stdout_eof_cleans_transport_once(self) -> None:
        reader = QueueReader()
        writer = CloseRecordingWriter()
        process = FakeProcess(stdin=writer, stdout=reader)
        client = JsonlRpcClient(
            stdin=writer,
            stdout=reader,
            process=process,
            request_timeout=0.25,
        )
        reader.lines.put(None)
        client._reader.join(timeout=0.1)
        self.assertFalse(client._reader.is_alive())

        client.close()
        client.close()

        self.assertEqual(
            {
                "stdin_close_calls": 1,
                "terminate_calls": 1,
                "kill_calls": 0,
                "wait_calls": [2.0],
                "child_reaped": True,
            },
            {
                "stdin_close_calls": writer.close_calls,
                "terminate_calls": process.terminate_calls,
                "kill_calls": process.kill_calls,
                "wait_calls": process.wait_calls,
                "child_reaped": process.reaped,
            },
        )

    def test_stdout_eof_racing_with_pending_registration_completes_once(self) -> None:
        reader = QueueReader()
        write_started = Barrier(2)
        allow_write = Event()
        writer = BarrierWriter(write_started, allow_write)
        client = JsonlRpcClient(stdin=writer, stdout=reader, request_timeout=0.5)
        failures: list[BaseException] = []

        def call() -> None:
            try:
                client.request("session/prompt", {"sessionId": "racing-eof"})
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=call)
        try:
            thread.start()
            # request() has registered _pending before entering writer.write().
            write_started.wait(timeout=1)
            reader.lines.put(None)
            allow_write.set()
            writer.messages.get(timeout=1)
            thread.join(timeout=0.1)
            completed_before_timeout = not thread.is_alive()
            thread.join(timeout=1)

            self.assertTrue(
                completed_before_timeout,
                "EOF/register interleaving must complete before request_timeout",
            )
            self.assertEqual(1, len(failures))
            self.assertIsInstance(failures[0], OSError)
            self.assertNotIsInstance(failures[0], TimeoutError)
            self.assertEqual({}, client._pending)
        finally:
            allow_write.set()
            reader.lines.put(None)
            client.close()
            thread.join(timeout=1)

    def test_stdout_eof_between_terminal_precheck_and_pending_registration_rejects_without_send(
        self,
    ) -> None:
        reader = QueueReader()
        writer = RecordingWriter()
        client = JsonlRpcClient(
            stdin=writer,
            stdout=reader,
            request_timeout=0.2,
        )
        pending = BlockingSetItemDict()
        client._pending = pending
        failures: list[BaseException] = []

        def call() -> None:
            try:
                client.request("session/prompt", {"sessionId": "lost-registration"})
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=call)
        try:
            thread.start()
            self.assertTrue(pending.registration_started.wait(timeout=1))
            self.assertEqual({}, client._pending)

            # EOF completes while admission is stopped and therefore drains nothing.
            reader.lines.put(None)
            client._reader.join(timeout=0.1)
            self.assertFalse(client._reader.is_alive())
            self.assertEqual({}, client._pending)

            pending.resume_registration.set()
            thread.join(timeout=0.05)
            completed_before_timeout = not thread.is_alive()
            thread.join(timeout=client.request_timeout + 0.2)
            self.assertFalse(thread.is_alive())

            self.assertEqual(
                {
                    "completed_before_timeout": True,
                    "is_transport_error": True,
                    "is_timeout_error": False,
                    "message_sent": False,
                    "pending_count": 0,
                    "completion_count": 1,
                },
                {
                    "completed_before_timeout": completed_before_timeout,
                    "is_transport_error": len(failures) == 1
                    and isinstance(failures[0], OSError),
                    "is_timeout_error": len(failures) == 1
                    and isinstance(failures[0], TimeoutError),
                    "message_sent": not writer.messages.empty(),
                    "pending_count": len(client._pending),
                    "completion_count": len(failures),
                },
            )
        finally:
            pending.resume_registration.set()
            reader.lines.put(None)
            client.close()
            thread.join(timeout=1)

    def test_lost_registration_contract_rejects_non_atomic_check_then_register_model(
        self,
    ) -> None:
        expected = {
            "eof_drained_count": 0,
            "completed_before_timeout": True,
            "is_transport_error": True,
            "is_timeout_error": False,
            "message_sent": False,
            "pending_count": 0,
            "completion_count": 1,
        }

        countermodel = self._observe_lost_registration_model(
            NonAtomicCheckThenRegisterModel
        )
        common_owner_model = self._observe_lost_registration_model(
            CommonTerminalOwnerModel
        )

        self.assertNotEqual(expected, countermodel)
        self.assertEqual(
            {
                "eof_drained_count": 0,
                "completed_before_timeout": False,
                "is_transport_error": True,
                "is_timeout_error": True,
                "message_sent": True,
                "pending_count": 0,
                "completion_count": 1,
            },
            countermodel,
        )
        self.assertEqual(expected, common_owner_model)

    def test_launch_reaps_child_when_initialize_observes_stdout_eof(self) -> None:
        reader = QueueReader()
        reader.lines.put(None)
        writer = CloseRecordingWriter()
        process = FakeProcess(stdin=writer, stdout=reader)

        caught: BaseException | None = None
        started_at = monotonic()
        with patch(
            "provider_transport.jsonl_rpc.subprocess.Popen",
            return_value=process,
        ):
            try:
                JsonlRpcClient.launch(command=("app-server",), request_timeout=0.5,
                                      spawn_admission=admission(), host_cell_id="cell")
            except BaseException as exc:
                caught = exc
        completed_before_timeout = monotonic() - started_at < 0.25

        self.assertEqual(
            {
                "is_transport_error": True,
                "is_timeout_error": False,
                "completed_before_timeout": True,
                "stdin_close_calls": 1,
                "terminate_calls": 1,
                "kill_calls": 0,
                "wait_calls": [2.0],
                "child_reaped": True,
            },
            {
                "is_transport_error": isinstance(caught, OSError),
                "is_timeout_error": isinstance(caught, TimeoutError),
                "completed_before_timeout": completed_before_timeout,
                "stdin_close_calls": writer.close_calls,
                "terminate_calls": process.terminate_calls,
                "kill_calls": process.kill_calls,
                "wait_calls": process.wait_calls,
                "child_reaped": process.reaped,
            },
        )

    def test_launch_reaps_child_and_reraises_baseexception_initialize_failure(
        self,
    ) -> None:
        reader = QueueReader()
        writer = CloseRecordingWriter()
        process = FakeProcess(stdin=writer, stdout=reader)
        initialize_failure = InitializeSentinel("initialize was interrupted")

        caught: BaseException | None = None
        try:
            with patch(
                "provider_transport.jsonl_rpc.subprocess.Popen",
                return_value=process,
            ), patch.object(
                JsonlRpcClient,
                "initialize",
                side_effect=initialize_failure,
            ):
                try:
                    JsonlRpcClient.launch(command=("app-server",), request_timeout=0.25,
                                          spawn_admission=admission(), host_cell_id="cell")
                except BaseException as exc:
                    caught = exc
        finally:
            reader.lines.put(None)

        self.assertIs(caught, initialize_failure)
        self.assertEqual(
            {
                "stdin_close_calls": 1,
                "terminate_calls": 1,
                "kill_calls": 0,
                "wait_calls": [2.0],
                "child_reaped": True,
            },
            {
                "stdin_close_calls": writer.close_calls,
                "terminate_calls": process.terminate_calls,
                "kill_calls": process.kill_calls,
                "wait_calls": process.wait_calls,
                "child_reaped": process.reaped,
            },
        )

    def test_launch_cleanup_contract_requires_baseexception_boundary(self) -> None:
        expected = {
            "same_exception": True,
            "stdin_close_calls": 1,
            "terminate_calls": 1,
            "kill_calls": 0,
            "wait_calls": [2.0],
            "child_reaped": True,
        }

        exception_countermodel = self._observe_modeled_launch_cleanup(Exception)
        baseexception_model = self._observe_modeled_launch_cleanup(BaseException)

        self.assertNotEqual(expected, exception_countermodel)
        self.assertEqual(expected, baseexception_model)

    def _observe_post_eof_model(self, client_type):
        reader = QueueReader()
        writer = RecordingWriter()
        client = client_type(stdin=writer, stdout=reader, request_timeout=0.2)
        try:
            return self._observe_request_after_processed_eof(client, reader, writer)
        finally:
            reader.lines.put(None)
            client.close()

    def _observe_request_after_processed_eof(self, client, reader, writer):
        failures: list[BaseException] = []

        # The peer closes stdout but deliberately continues accepting stdin writes.
        reader.lines.put(None)
        client._reader.join(timeout=0.1)
        self.assertFalse(client._reader.is_alive())

        def call() -> None:
            try:
                client.request("session/prompt", {"sessionId": "after-eof"})
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=call)
        thread.start()
        thread.join(timeout=0.05)
        completed_before_timeout = not thread.is_alive()
        thread.join(timeout=client.request_timeout + 0.2)
        self.assertFalse(thread.is_alive())

        return {
            "completed_before_timeout": completed_before_timeout,
            "is_transport_error": len(failures) == 1
            and isinstance(failures[0], OSError),
            "is_timeout_error": len(failures) == 1
            and isinstance(failures[0], TimeoutError),
            "message_sent": not writer.messages.empty(),
            "pending_count": len(client._pending),
        }

    def _observe_modeled_launch_cleanup(self, catch_type):
        reader = QueueReader()
        writer = CloseRecordingWriter()
        process = FakeProcess(stdin=writer, stdout=reader)
        client = JsonlRpcClient(
            stdin=writer,
            stdout=reader,
            process=process,
            request_timeout=0.25,
        )
        initialize_failure = InitializeSentinel("initialize was interrupted")

        caught: BaseException | None = None
        try:
            try:
                raise initialize_failure
            except catch_type:
                client.close()
                raise
        except BaseException as exc:
            caught = exc
        finally:
            reader.lines.put(None)
            client._reader.join(timeout=1)

        return {
            "same_exception": caught is initialize_failure,
            "stdin_close_calls": writer.close_calls,
            "terminate_calls": process.terminate_calls,
            "kill_calls": process.kill_calls,
            "wait_calls": process.wait_calls,
            "child_reaped": process.reaped,
        }

    def _observe_lost_registration_model(self, client_type):
        reader = QueueReader()
        writer = RecordingWriter()
        client = client_type(stdin=writer, stdout=reader, request_timeout=0.2)
        failures: list[BaseException] = []

        def call() -> None:
            try:
                client.request("session/prompt", {"sessionId": "modeled-gap"})
            except BaseException as exc:
                failures.append(exc)

        thread = Thread(target=call)
        try:
            thread.start()
            self.assertTrue(client._pending.registration_started.wait(timeout=1))
            self.assertEqual({}, client._pending)
            reader.lines.put(None)
            self.assertTrue(client.eof_processed.wait(timeout=1))
            self.assertEqual(0, client.drained_at_eof)
            client._pending.resume_registration.set()
            thread.join(timeout=0.05)
            completed_before_timeout = not thread.is_alive()
            thread.join(timeout=client.request_timeout + 0.2)
            self.assertFalse(thread.is_alive())
            return {
                "eof_drained_count": client.drained_at_eof,
                "completed_before_timeout": completed_before_timeout,
                "is_transport_error": len(failures) == 1
                and isinstance(failures[0], OSError),
                "is_timeout_error": len(failures) == 1
                and isinstance(failures[0], TimeoutError),
                "message_sent": not writer.messages.empty(),
                "pending_count": len(client._pending),
                "completion_count": len(failures),
            }
        finally:
            client._pending.resume_registration.set()
            reader.lines.put(None)
            client.close()
            thread.join(timeout=1)


if __name__ == "__main__":
    unittest.main()
