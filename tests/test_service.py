"""Requests the engine accepts and refuses, the request files, the client and the CLI."""
import errno
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout

from engine_support import RUNNER, SRC, EngineCase, approve, changes, produced, published_runner, git, git_out, write

from loop_engine import cli, client, service, store
from loop_engine.agents import TurnOutcome
from loop_engine.platform_facts import CURRENT, POSIX, WINDOWS
from loop_engine.service import Engine


def finishing(request, commands, cancelled):
    return produced(request, commands) if request.role == "producer" else approve(request)


class AdmissionTest(EngineCase):
    def refused(self, engine, contract=None, **contract_changes):
        response = engine.handle({"request": "start", "contract": contract or self.contract(**contract_changes)})
        self.assertFalse(response["ok"], response)
        self.assertEqual([], store.list_units(self.root))
        self.assertEqual([], self.agents.requests)
        return response["error"]

    def test_start_is_refused_before_anything_is_created(self):
        engine = self.engine(finishing)
        self.assertIn("installed catalog", self.refused(engine, self.with_agent(self.contract(), producer="gamma")))
        self.assertIn("top directory", self.refused(engine, repository=os.path.join(self.repo, ".loop-engine")))
        self.assertIn("cannot be read", self.refused(engine, repository=os.path.join(self.repo, "missing")))
        self.assertIn("checks.json", self.refused(engine, self.with_check(self.contract(), "lint")))
        self.assertIn("keys must be exactly", self.refused(engine, command=["sh", "-c", "touch /tmp/x"]))

    def test_checks_come_from_the_committed_file_not_the_working_copy(self):
        write(self.repo, ".loop-engine/checks.json", json.dumps(
            {"checks": {"unit": {"argv": ["sh", "-c", "exit 0"]}, "lint": {"argv": ["x"]}}}))
        engine = self.engine(finishing)
        self.assertIn("lint", self.refused(engine, self.with_check(self.contract(), "lint")))
        engine.handle({"request": "start", "contract": self.contract()})
        self.run_to_stop(engine)
        with open(os.path.join(store.unit_dir(self.published, "u1"), "checks.json"), encoding="utf-8") as handle:
            self.assertEqual(self.checks, json.load(handle)["checks"])

    def test_engine_refuses_the_repository_its_own_code_runs_from(self):
        engine = self.engine(finishing, own_code=(os.path.join(self.repo, "packages"),))
        self.assertIn("own code", self.refused(engine))

    def test_same_contract_is_idempotent_and_a_different_one_conflicts(self):
        engine = self.engine(finishing)
        engine.handle({"request": "start", "contract": self.contract()})
        self.run_to_stop(engine)
        again = engine.handle({"request": "start", "contract": self.contract()})
        self.assertEqual("done", again["result"]["stop_reason"])
        self.assertEqual(2, len(self.agents.requests))
        different = self.contract()
        different["unit"]["goal"] = "something else"
        other = engine.handle({"request": "start", "contract": different})
        self.assertIn("different unit", other["error"])

    def test_an_error_in_a_request_is_answered_and_the_engine_keeps_answering(self):
        engine = self.engine(finishing)
        def broken(_raw):
            raise ValueError("Paths don't have the same drive")
        engine.start = broken
        with redirect_stderr(io.StringIO()) as errors:
            response = engine.handle({"request": "start", "contract": self.contract()})
        self.assertEqual({"ok": False, "error": "engine error: ValueError: Paths don't have the same drive"}, response)
        self.assertIn("Traceback", errors.getvalue())
        self.assertEqual({"ok": True, "result": {"engine": "running"}}, engine.handle({"request": "ping"}))

    def test_a_start_that_failed_after_creating_the_unit_still_drives_it(self):
        engine = self.engine(finishing)
        launch, calls = engine._launch, []
        def failing_once(unit_id):
            calls.append(unit_id)
            if len(calls) == 1:
                raise RuntimeError("cannot start a thread")
            launch(unit_id)
        engine._launch = failing_once
        with redirect_stderr(io.StringIO()):
            response = engine.handle({"request": "start", "contract": self.contract()})
        self.assertIn("engine error: RuntimeError", response["error"])
        self.assertEqual("done", self.run_to_stop(engine)["stop_reason"])
        self.assertTrue(engine.handle({"request": "start", "contract": self.contract()})["ok"])

    def test_a_start_that_failed_before_recording_the_unit_is_completed_when_sent_again(self):
        engine = self.engine(finishing)
        unit_store = engine._store("u1")
        write_status, failed = unit_store.write_status, []
        def failing_once(status):
            if not failed:
                failed.append(status)
                raise OSError("disk full")
            write_status(status)
        unit_store.write_status = failing_once
        start = {"request": "start", "contract": self.contract()}
        with redirect_stderr(io.StringIO()):
            self.assertIn("engine error: OSError: disk full", engine.handle(start)["error"])
        self.assertTrue(engine.handle(start)["ok"])
        self.assertEqual("done", self.run_to_stop(engine)["stop_reason"])

    def test_an_answer_that_failed_before_its_state_was_saved_can_be_sent_again(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            if self.agents.nth["producer"] == 1:
                return TurnOutcome(True, '{"question": "tabs or spaces?", "id": "q-style"}', None)
            return produced(request, commands)
        engine = self.engine(script)
        engine.handle({"request": "start", "contract": self.contract()})
        self.assertEqual("waiting_for_answer", self.run_to_stop(engine)["stop_reason"])
        unit_store = engine._store("u1")
        write_json, failed = unit_store.write_json, []
        def failing_once(name, value):
            if name == "state.json" and not failed:
                failed.append(name)
                raise OSError("disk full")
            write_json(name, value)
        unit_store.write_json = failing_once
        answer = {"request": "answer", "unit_id": "u1", "question_id": "q-style", "answer": "spaces"}
        with redirect_stderr(io.StringIO()):
            self.assertIn("engine error: OSError: disk full", engine.handle(answer)["error"])
        self.assertEqual("waiting_for_answer", self.run_to_stop(engine)["stop_reason"])
        self.assertTrue(engine.handle(answer)["ok"])
        self.assertEqual("done", self.run_to_stop(engine)["stop_reason"])

    def test_an_answer_that_failed_after_its_state_was_saved_is_still_carried_out(self):
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            if self.agents.nth["producer"] == 1:
                return TurnOutcome(True, '{"question": "tabs or spaces?", "id": "q-style"}', None)
            return produced(request, commands)
        engine = self.engine(script)
        engine.handle({"request": "start", "contract": self.contract()})
        self.assertEqual("waiting_for_answer", self.run_to_stop(engine)["stop_reason"])
        unit_store = engine._store("u1")
        write_json, write_status, saved, failed = unit_store.write_json, unit_store.write_status, [], []
        def saving(name, value):
            write_json(name, value)
            saved.append(name)
        def failing_after_the_state(status):  # the first status written once the answer's state is saved
            if "state.json" in saved and not failed:
                failed.append(status)
                raise OSError("disk full")
            write_status(status)
        unit_store.write_json, unit_store.write_status = saving, failing_after_the_state
        answer = {"request": "answer", "unit_id": "u1", "question_id": "q-style", "answer": "spaces"}
        with redirect_stderr(io.StringIO()):
            self.assertIn("engine error: OSError: disk full", engine.handle(answer)["error"])
        self.assertEqual("done", self.run_to_stop(engine)["stop_reason"])
        self.assertIn("Answer: spaces", self.agents.requests[-2].prompt)

    def test_unknown_and_malformed_requests_are_refused(self):
        engine = self.engine(finishing)
        for payload in (None, [], {"request": "exec", "argv": ["sh"]}, {"request": "start"},
                        {"request": "start", "contract": self.contract(), "env": {"X": "1"}},
                        {"request": "cancel", "unit_id": "../u1"}, {"request": "cancel", "unit_id": "nope"},
                        {"request": "answer", "unit_id": "u1", "question_id": "q", "answer": "a", "path": "/x"}):
            self.assertFalse(engine.handle(payload)["ok"], payload)
        self.assertEqual([], self.agents.requests)


class ServeTest(EngineCase):
    def serving(self, script, idle_seconds=60.0):
        engine = self.engine(script, idle_seconds=idle_seconds)
        served = []
        thread = threading.Thread(target=lambda: served.append(engine.serve(client.lock_path(self.root))), daemon=True)
        thread.start()
        self.addCleanup(thread.join, 30)
        self.addCleanup(engine._halting.set)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:  # an entry starts a real engine while the state home is free
            probe = store.ProcessLock(client.lock_path(self.root))
            if not probe.acquire():
                break
            probe.release()
            time.sleep(0.01)
        return engine, thread, served

    def environ(self):
        return {**os.environ, "LOOP_ENGINE_HOME": self.home, "LOOP_ENGINE_WORKTREES": os.path.join(self.home, "worktrees")}

    def test_request_files_are_answered_and_removed_and_a_second_engine_yields(self):
        engine, _thread, _served = self.serving(finishing)
        response = client.request(self.root, {"request": "start", "contract": self.contract()}, environ=self.environ())
        self.assertTrue(response["ok"], response)
        status = client.wait(self.root, "u1", timeout=30)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual([], os.listdir(os.path.join(self.root, "inbox")))
        self.assertIn(b"+made by the producer", client.result(self.root, "u1", "patch.diff"))
        with self.assertRaises(ValueError):
            client.result(self.root, "u1", "../../contract.json")
        second = self.engine(finishing)
        self.assertFalse(second.serve(client.lock_path(self.root)))

    def test_garbage_in_the_inbox_is_refused_not_interpreted(self):
        self.serving(finishing)
        os.makedirs(os.path.join(self.root, "inbox"), exist_ok=True)
        for name, text in (("0" * 32 + ".json", "{not json"), ("../escape.json", "{}"), ("short.json", "{}")):
            try:
                with open(os.path.join(self.root, "inbox", name), "w") as handle:
                    handle.write(text)
            except OSError:
                continue
        self.assertEqual({"ok": True, "result": {"engine": "running"}},
                         client.request(self.root, {"request": "ping"}, environ=self.environ()))
        self.assertEqual({"ok": False, "error": "malformed request"},
                         store.read_json(store.response_path(self.root, "0" * 32)))
        self.assertEqual([], store.list_units(self.root))

    def test_engine_exits_when_idle_and_releases_the_state_home(self):
        engine, thread, served = self.serving(finishing, idle_seconds=0.3)
        thread.join(30)
        self.assertEqual([True], served)
        probe = store.ProcessLock(client.lock_path(self.root))
        self.assertTrue(probe.acquire())
        probe.release()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(argv), environ=self.environ())
        return code, out.getvalue(), err.getvalue()

    def contract_file(self, **changes_):
        path = os.path.join(self.home, f"contract-{len(changes_)}.json")
        os.makedirs(self.home, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(self.contract(**changes_), handle)
        return path

    def test_cli_run_exits_with_a_code_per_stop_reason(self):
        def script(request, commands, cancelled):
            if request.unit_id == "ask" and request.round == 1:
                return TurnOutcome(True, '{"question": "which?", "id": "q-1"}', None)
            if request.unit_id == "lost":
                return TurnOutcome(True, "no record", None)
            if request.unit_id == "vague" and request.role == "reviewer":
                return TurnOutcome(True, "LGTM", {"version": 1, "dropped": 0, "entries": []})
            if request.unit_id == "refused" and request.role == "reviewer":
                return changes(request, "the same problem every time")
            return finishing(request, commands, cancelled)
        self.serving(script)
        code, out, _err = self.cli("run", self.contract_file())
        self.assertEqual((0, "done"), (code, json.loads(out)["stop_reason"]))
        code, out, _err = self.cli("run", self.contract_file(unit_id="lost"))
        self.assertEqual((13, "no-execution-record"), (code, json.loads(out)["detail"]))
        self.assertEqual(17, self.cli("run", self.contract_file(unit_id="vague"))[0])
        self.assertEqual(12, self.cli("run", self.contract_file(unit_id="refused"))[0])
        self.assertEqual(10, self.cli("run", self.contract_file(unit_id="ask"))[0])
        answer = os.path.join(self.home, "answer.txt")
        with open(answer, "w") as handle:
            handle.write("the first")
        self.assertEqual(0, self.cli("answer", "ask", "q-1", answer)[0])
        self.assertEqual(0, self.cli("wait", "ask")[0])
        bad = os.path.join(self.home, "bad.json")
        with open(bad, "w") as handle:
            json.dump(self.with_check(self.contract(unit_id="bad"), "nope"), handle)
        code, _out, err = self.cli("run", bad)
        self.assertEqual(2, code)
        self.assertIn("rejected", err)
        self.assertEqual(2, self.cli("status", "never-started")[0])
        self.assertEqual(2, self.cli("run", os.path.join(self.home, "missing.json"))[0])

    def test_worktrees_live_where_agents_can_write_not_under_the_state_home(self):
        self.assertEqual("/cache/loop-engine/worktrees", store.worktrees_root({"XDG_CACHE_HOME": "/cache"}))
        self.assertEqual("/elsewhere/wt", store.worktrees_root({"LOOP_ENGINE_WORKTREES": "/elsewhere/wt",
                                                                "LOOP_ENGINE_HOME": "/state"}))
        default = store.worktrees_root({"LOOP_ENGINE_HOME": "/state"})
        self.assertFalse(default.startswith("/state"))
        self.assertTrue(default.endswith(os.path.join(".cache", "loop-engine", "worktrees")))

    def test_the_state_home_and_its_roots_carry_the_engines_own_names_and_no_callers(self):
        environ = {"LOOP_ENGINE_HOME": "/state", "DEV_SKILLS_HOME": "/caller", "DEV_SKILLS_WORKTREES": "/caller/wt"}
        self.assertEqual("/state/records", store.home_root(environ))
        self.assertEqual("/state/published", store.published_root(environ))
        self.assertFalse(store.worktrees_root(environ).startswith("/caller"))
        default = store.home_root({"DEV_SKILLS_HOME": "/caller"})
        self.assertTrue(default.endswith(os.path.join(".loop-engine", "records")))
        self.assertEqual(".loop-engine/checks.json", service.CHECKS_FILE)

    def test_engine_command_is_fixed_and_isolated(self):
        command = client.engine_command()
        self.assertEqual([sys.executable, "-I", os.path.join(SRC, "loop_engine", "__main__.py"), "serve"], command)


class ProcessLockTest(unittest.TestCase):
    """Taking the engine's lock has three outcomes: acquired, held by another, failed."""

    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, True)

    def test_acquired_then_held_by_another_then_free_again(self):
        path = os.path.join(self.directory, "home", "engine.lock")
        first, second = store.ProcessLock(path), store.ProcessLock(path)
        self.assertTrue(first.acquire())
        self.addCleanup(first.release)
        self.assertFalse(second.acquire())
        first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_a_lock_that_cannot_be_taken_names_its_cause(self):
        blocker = os.path.join(self.directory, "not-a-directory")
        with open(blocker, "w") as handle:
            handle.write("x")
        directory_lock = os.path.join(self.directory, "a-directory")
        os.mkdir(directory_lock)
        for path in (os.path.join(blocker, "engine.lock"), directory_lock):
            with self.subTest(path=path):
                with self.assertRaises(store.LockFailed) as raised:
                    store.ProcessLock(path).acquire()
                self.assertIn(path, str(raised.exception))
                self.assertIsInstance(raised.exception.__cause__, OSError)

    def test_only_contention_means_another_engine(self):
        cases = (
            (False, errno.EWOULDBLOCK, True), (False, errno.EAGAIN, True),
            (False, errno.ENOLCK, False), (False, errno.EACCES, False), (False, errno.EOPNOTSUPP, False),
            (True, errno.EACCES, True), (True, errno.EBADF, False), (True, errno.EINVAL, False),
        )
        for windows, code, contended in cases:
            with self.subTest(windows=windows, errno=errno.errorcode[code]):
                error = OSError(code, os.strerror(code))
                self.assertIs(contended, store.lock_contended(error, (WINDOWS if windows else POSIX).lock_held))

    def test_an_entry_that_cannot_lock_reports_the_cause_instead_of_assuming_an_engine(self):
        blocker = os.path.join(self.directory, "home")
        with open(blocker, "w") as handle:
            handle.write("x")
        with self.assertRaisesRegex(client.EngineUnavailable, "cannot be locked: cannot open the lock file"):
            client.ensure_engine(blocker, dict(os.environ))
        engine = Engine(os.path.join(self.directory, "state"), published=os.path.join(self.directory, "published"),
                        git=None, agents=None, templates={},
                        runner=(sys.executable, RUNNER), worktrees=self.directory, platform=CURRENT)
        with self.assertRaises(store.LockFailed):
            engine.serve(client.lock_path(blocker))


class EngineProcessTest(EngineCase):
    """The real engine process, started the way an entry starts it."""

    def test_entry_starts_the_engine_which_answers_and_refuses_from_its_own_installation(self):
        environ = {**os.environ, "LOOP_ENGINE_HOME": self.home, "PYTHONPATH": self.repo,
                   "LOOP_ENGINE_WORKTREES": os.path.join(self.home, "worktrees")}
        write(self.repo, "loop_engine/__init__.py", "raise SystemExit('imported from the working directory')\n")
        before = list(client._started)

        def stop_engines():
            # An engine this test stopped is not one that failed: forget it, so a later request
            # in this process does not report it as stopped before answering.
            for process in [process for process in client._started if process not in before]:
                process.terminate()
                process.wait(30)
                client._started.remove(process)
                client._stderr_paths.pop(process, None)
        self.addCleanup(stop_engines)
        self.assertEqual({"ok": True, "result": {"engine": "running"}},
                         client.request(self.root, {"request": "ping"}, environ=environ))
        self.assertEqual(1, len(client._started) - len(before))
        self.assertIsNone(client.ensure_engine(self.root, environ))
        # the engine this entry started publishes the check program beside the state home it was given
        with open(published_runner(self.published), "rb") as published, open(RUNNER, "rb") as source:
            self.assertEqual(source.read(), published.read())
        unknown = client.request(self.root, {"request": "start", "contract": self.with_agent(
            self.contract(), producer="gamma")}, environ=environ)
        self.assertIn("installed catalog", unknown["error"])
        known = client.request(self.root, {"request": "start", "contract": self.with_agent(self.contract(
            repository=git_out(SRC, "rev-parse", "--show-toplevel")), producer="codex", reviewer="claude")},
            environ=environ)
        self.assertIn("own code", known["error"])
        self.assertEqual([], store.list_units(self.root))

    def test_an_engine_that_stops_before_answering_is_reported_with_its_error_output(self):
        # An engine that cannot start says why, instead of the entry waiting until its time runs out.
        no_git = os.path.join(os.path.dirname(self.home), "no git")
        os.makedirs(no_git)
        environ = {**os.environ, "LOOP_ENGINE_HOME": self.home, "PATH": no_git,
                   "LOOP_ENGINE_WORKTREES": os.path.join(self.home, "worktrees")}
        started = time.monotonic()
        with self.assertRaisesRegex(client.EngineUnavailable, r"(?s)stopped with status [1-9]\d* before it answered: .*git not found on PATH"):
            client.request(self.root, {"request": "ping"}, environ=environ, timeout=60)
        self.assertLess(time.monotonic() - started, 30)


if __name__ == "__main__":
    unittest.main()
