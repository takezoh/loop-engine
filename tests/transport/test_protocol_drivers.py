"""Every protocol driver keeps the driver port contract the provider lifecycle relies on."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import time
from types import SimpleNamespace
import unittest

from provider_transport.acp_driver import AcpDriver
from provider_transport.codex_driver import CodexDriver
from provider_transport.provider_request import ActionRequest
from provider_transport.pi_driver import PiDriver, binding_mismatch, turn_end
from provider_transport.pi_rpc import PiRpcClient, PiRpcClosed, TRANSPORT_CLOSED
from provider_transport.provider_port import HostSlot, TurnSpec
from .codex_rpc_fake import FakeCodexRpc, FakeProcess
from .pi_rpc_fake import FakePi
from .protocol_driver_fake import FakeAdmission, FakeDriver


def request(operation="op-1", model="model-a", effort="high"):
    return ActionRequest("start", operation, provider="p", model=model, effort=effort,
                         cwd="/work", prompt="do it")


class WireAdmission:
    """What a wire fake reads from a spawn admission: consume, spawned and the reservation."""

    def __init__(self):
        self.spawned_count = 0
        self.reservation = SimpleNamespace(release_live=lambda receipt, certain: None)
        self.cwd_lease = SimpleNamespace(admitted_path=lambda: "/work")

    def consume(self, *, expected_cell_id):
        return "/work", ()

    def spawned(self, pid):
        self.spawned_count += 1
        return SimpleNamespace(pid=pid)


def wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        time.sleep(0.01)


class ProtocolDriverContract:
    """What `ProviderSessions` relies on from every `ProtocolDriver`."""

    def make_driver(self):
        raise NotImplementedError

    def open_host(self):
        """The implementation under contract, for the port registry."""
        return self.make_driver()

    def close_host(self, driver):
        pass

    def admission(self):
        return WireAdmission()

    def hold_next_turn(self):
        """Keep the next turn running until it is cancelled."""
        raise NotImplementedError

    def fail_next_bind(self):
        """Make the next turn fail before its session binds."""
        raise NotImplementedError

    def slot(self, host=None):
        adopted = []
        slot = HostSlot(host, self.admission() if host is None else None, "cell", adopted.append)
        return slot, adopted

    def turn(self, driver, slot, *, durable=None, operation="op-1"):
        sessions = []
        end = driver.run_turn(TurnSpec(request(operation), "/work", durable), slot, sessions.append)
        return end, sessions

    def test_a_turn_without_a_host_launches_one_binds_once_and_completes(self):
        driver = self.make_driver()
        slot, adopted = self.slot()
        end, sessions = self.turn(driver, slot)
        self.assertEqual([slot.host], adopted)
        self.assertEqual(1, len(sessions))
        self.assertTrue(sessions[0].session_id and sessions[0].durable_id)
        self.assertTrue(end.completed, end)
        driver.release(sessions[0], 2.0)

    def test_a_durable_session_binds_again_by_its_identity(self):
        driver = self.make_driver()
        slot, _ = self.slot()
        _, first = self.turn(driver, slot)
        driver.release(first[0], 2.0)
        shared = driver.traits.shared_host
        again_slot, adopted = self.slot(slot.host if shared else None)
        _, again = self.turn(driver, again_slot, durable=first[0].durable_id, operation="op-2")
        self.assertEqual(first[0].session_id, again[0].session_id)
        self.assertEqual(0 if shared else 1, len(adopted))
        driver.release(again[0], 2.0)

    def test_a_running_turn_reports_progress_and_ends_unfinished_when_cancelled(self):
        driver = self.make_driver()
        slot, _ = self.slot()
        self.hold_next_turn()
        sessions, result = [], {}

        def work():
            result["end"] = driver.run_turn(TurnSpec(request(), "/work", None), slot, sessions.append)

        worker = Thread(target=work, daemon=True)
        worker.start()
        wait_for(lambda: sessions)
        progress = driver.progress(sessions[0])
        self.assertIsInstance(progress.output, str)
        self.assertTrue(progress.activity_at is None or isinstance(progress.activity_at, float))
        driver.cancel(sessions[0])
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(result["end"].completed)
        driver.release(sessions[0], 2.0)

    def test_a_failure_before_binding_raises_without_a_session(self):
        driver = self.make_driver()
        slot, _ = self.slot()
        self.fail_next_bind()
        sessions = []
        with self.assertRaises(Exception):
            driver.run_turn(TurnSpec(request(), "/work", None), slot, sessions.append)
        self.assertEqual([], sessions)


class FakeDriverContractTests(ProtocolDriverContract, unittest.TestCase):
    def setUp(self):
        self.driver = FakeDriver()

    def make_driver(self):
        return self.driver

    def admission(self):
        return FakeAdmission()

    def hold_next_turn(self):
        self.driver.script(hold=True)

    def fail_next_bind(self):
        self.driver.script(bind_error=OSError("bind failed"))


class AutoFinishingCodexRpc(FakeCodexRpc):
    """The App Server fake, finishing each turn unless held."""

    def __init__(self, admission, owner):
        super().__init__(admission)
        self.owner = owner

    def request(self, method, params):
        if method == "thread/start" and self.owner.fail_bind:
            self.owner.fail_bind = False
            raise OSError("thread-start-failed")
        result = super().request(method, params)
        if method == "turn/start":
            if self.owner.hold:
                self.owner.hold = False
            else:
                self.finish(params["threadId"])
        return result


class CodexDriverContractTests(ProtocolDriverContract, unittest.TestCase):
    def setUp(self):
        self.hold = False
        self.fail_bind = False

    def make_driver(self):
        return CodexDriver(timeout=5.0, launch=lambda admission, cell_id: AutoFinishingCodexRpc(admission, self))

    def hold_next_turn(self):
        self.hold = True

    def fail_next_bind(self):
        self.fail_bind = True


class AcpDriverContractTests(ProtocolDriverContract, unittest.TestCase):
    """The ACP driver over a router that binds and prompts like the real one."""

    def setUp(self):
        self.hold = False
        self.fail_bind = False
        self.sessions = 0
        self.routed = []

    def router(self, payload, *, spawn_admission, host_cell_id, existing_connection=None,
               spawn_observer=None, started_observer=None, message_capture=None, **kwargs):
        from provider_transport.acp_router import AcpStartedTurn
        from provider_transport.turn_result import TurnCollector
        self.routed.append(kwargs)
        connection = existing_connection
        if connection is None:
            spawn_admission.spawned(os.getpid())
            connection = SimpleNamespace(rpc=SimpleNamespace(
                process=FakeProcess(), close=lambda: None))
        spawn_observer(connection)
        if self.fail_bind:
            self.fail_bind = False
            raise OSError("session-new-failed")
        if "session_id" in payload:
            session_id = payload["session_id"]
        else:
            self.sessions += 1
            session_id = f"acp-session-{self.sessions}"
        cancelled = Event()
        held, self.hold = self.hold, False
        binding = SimpleNamespace(session_id=session_id, provider="claude", generation="g")
        client = SimpleNamespace(cancel=lambda _binding: cancelled.set(), close=lambda _binding, timeout=None: None)
        started_observer(AcpStartedTurn(
            provider="claude", binding=binding, connection=connection, client=client,
            message_capture=message_capture, binding_proof=None,
        ))
        if held:
            cancelled.wait(5)
        return SimpleNamespace(
            binding=binding, stop_reason=None if held else "end_turn",
            turn_result=TurnCollector(f"{session_id}:turn").seal_completed(),
            cleanup_diagnostics=None, binding_proof=None, tool_failures=(),
        )

    def make_driver(self):
        return AcpDriver(provider="claude", generation="g", authority_root=Path("/work"), router=self.router)

    def hold_next_turn(self):
        self.hold = True

    def fail_next_bind(self):
        self.fail_bind = True

    def test_a_turn_hands_the_callers_exclusion_to_the_router(self):
        named = {"plugin": "tools", "names": ["design", "review"]}
        for exclude in (named, None):
            with self.subTest(exclude=exclude):
                driver = AcpDriver(provider="claude", generation="g", authority_root=Path("/work"),
                                   router=self.router, exclude=exclude)
                slot, _ = self.slot()
                self.turn(driver, slot)
                self.assertEqual(exclude, self.routed[-1]["exclude"])


class PiDriverContractTests(ProtocolDriverContract, unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.session_dir = Path(directory.name)
        self.peers = []
        self.hold = False
        self.reject_state = False

    def launch(self, command, admission, cell_id):
        admission.spawned(os.getpid())
        peer = FakePi(command, hold=self.hold, reject_state=self.reject_state)
        self.hold = self.reject_state = False
        self.peers.append(peer)
        return peer.client

    def make_driver(self):
        return PiDriver(session_dir=self.session_dir, timeout=5.0, launch=self.launch)

    def hold_next_turn(self):
        self.hold = True

    def fail_next_bind(self):
        self.reject_state = True

    def test_the_launch_command_carries_the_callers_exclusion_and_the_session_binding(self):
        driver = PiDriver(session_dir=self.session_dir, timeout=5.0, launch=self.launch,
                          exclude={"plugin": "tools", "names": ["design", "review"]})
        slot, _ = self.slot()
        _, sessions = self.turn(driver, slot)
        command = self.peers[0].command
        self.assertEqual(("pi", "--mode", "rpc"), command[:3])
        exclusion = command.index("--exclude-skills")
        self.assertEqual(["design", "review"], command[exclusion + 1].split(","))
        self.assertEqual(
            ("--session-dir", str(self.session_dir), "--session-id", sessions[0].session_id,
             "--model", "model-a", "--thinking", "high"),
            command[-8:],
        )
        self.assertEqual(["do it"], self.peers[0].prompts)
        driver.release(sessions[0], 2.0)
        self.assertTrue(self.peers[0].closed.wait(2))


class PiDecisionTests(unittest.TestCase):
    def test_a_session_binds_only_with_the_requested_identity_model_and_thinking(self):
        state = {"sessionId": "s", "model": {"id": "m", "provider": "p"}, "thinkingLevel": "high"}
        self.assertIsNone(binding_mismatch(state, session_id="s", model="m", effort="high"))
        self.assertIsNone(binding_mismatch(state, session_id="s", model="p/m", effort=None))
        self.assertEqual("pi-session-identity-mismatch", binding_mismatch(state, session_id="t", model=None, effort=None))
        self.assertEqual("pi-model-binding-mismatch", binding_mismatch(state, session_id="s", model="x", effort=None))
        self.assertEqual("pi-thinking-binding-mismatch", binding_mismatch(state, session_id="s", model=None, effort="low"))

    def test_a_run_completes_only_when_it_settled_with_a_final_message(self):
        final = {"role": "assistant", "content": [{"type": "text", "text": "done"}], "stopReason": "stop"}
        cases = (
            (dict(final=final, aborted=False, transport_lost=False), True, "stop"),
            (dict(final={**final, "stopReason": "error"}, aborted=False, transport_lost=False), False, "error"),
            (dict(final=final, aborted=True, transport_lost=False), False, "aborted"),
            (dict(final=None, aborted=False, transport_lost=False), False, "no_message"),
            (dict(final=final, aborted=False, transport_lost=True), False, "transport_lost"),
        )
        for values, completed, status in cases:
            with self.subTest(status=status):
                end = turn_end(identity="s:op", **values)
                self.assertEqual((completed, status), (end.completed, end.provider_status))
                self.assertEqual("s:op", end.turn_result["turn_identity"])


class PiRpcClientTests(unittest.TestCase):
    def test_records_split_only_on_line_feed(self):
        read_fd, write_fd = os.pipe()
        peer_read, peer_write = os.pipe()
        client = PiRpcClient(stdin=os.fdopen(peer_write, "wb"), stdout=os.fdopen(read_fd, "rb"))
        self.addCleanup(client.close)
        events = []
        client.subscribe(events.append)
        with os.fdopen(write_fd, "wb") as out:
            out.write('{"type":"message_update","text":"a b"}\n'.encode("utf-8"))
        wait_for(lambda: len(events) == 2)
        self.assertEqual("a b", events[0]["text"])
        self.assertEqual(TRANSPORT_CLOSED, events[1]["type"])
        os.close(peer_read)

    def test_a_closed_transport_fails_the_waiting_command(self):
        read_fd, write_fd = os.pipe()
        peer_read, peer_write = os.pipe()
        client = PiRpcClient(stdin=os.fdopen(peer_write, "wb"), stdout=os.fdopen(read_fd, "rb"))
        self.addCleanup(client.close)
        Thread(target=lambda: (time.sleep(0.05), os.close(write_fd)), daemon=True).start()
        with self.assertRaises(PiRpcClosed):
            client.command("get_state", timeout=5)
        self.assertFalse(client.alive())
        os.close(peer_read)


if __name__ == "__main__":
    unittest.main()


class PiLifecycleTests(unittest.TestCase):
    """The provider lifecycle over the Pi driver: one process per turn, one durable session."""

    def setUp(self):
        from provider_transport.provider_sessions import ProviderSessions
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.peers = []
        self.sessions = ProviderSessions(
            PiDriver(session_dir=Path(directory.name), timeout=5.0, launch=self.launch), cell_id="cell"
        )
        self.addCleanup(self.sessions.shutdown)

    def launch(self, command, admission, cell_id):
        admission.spawned(os.getpid())
        peer = FakePi(command)
        self.peers.append(peer)
        return peer.client

    def test_each_turn_runs_in_its_own_process_and_continues_the_same_session(self):
        first_admission = WireAdmission()
        self.sessions.bind_pending_execution("exec-1", "op-1")
        started = self.sessions.start(request("op-1"), first_admission)
        session_id = started["session_id"]
        self.sessions.bind_execution("exec-1", session_id)
        self.assertTrue(self.sessions.wait_turn("exec-1", 5))
        self.assertTrue(self.peers[0].closed.wait(5))
        self.assertFalse(self.sessions.uses_shared_host(session_id))
        self.assertTrue(self.sessions.continuation_needs_spawn("exec-1"))

        second_admission = WireAdmission()
        continued = self.sessions.action(
            ActionRequest("continue", "op-2", provider="pi", model="model-a", effort="high", cwd="/work",
                          prompt="again"),
            SimpleNamespace(execution_id="exec-1"), second_admission,
        )
        self.assertEqual(session_id, continued["session_id"])
        self.assertEqual((1, 1), (first_admission.spawned_count, second_admission.spawned_count))
        self.assertEqual(session_id, self.peers[1].command[self.peers[1].command.index("--session-id") + 1])
        self.assertTrue(self.sessions.wait_turn("exec-1", 5))
        observed = self.sessions.action(
            ActionRequest("observe", "op-3"),
            SimpleNamespace(execution_id="exec-1"),
        )
        self.assertEqual(("completed", "stop"), (observed["outcome"], observed["provider_status"]))


class ProviderCompositionTests(unittest.TestCase):
    """The catalog transport selects the driver."""

    def test_each_transport_composes_its_driver(self):
        from provider_transport.provider_catalog import load_provider
        from provider_transport.provider_drivers import driver_for
        with TemporaryDirectory() as directory:
            state = Path(directory)
            kinds = {
                name: type(driver_for(load_provider(name), origin=state, state_dir=state))
                for name in ("codex", "claude", "pi")
            }
            pi = driver_for(load_provider("pi"), origin=state, state_dir=state)
        self.assertEqual({"codex": CodexDriver, "claude": AcpDriver, "pi": PiDriver}, kinds)
        self.assertEqual(state / "pi-sessions", pi.session_dir)
