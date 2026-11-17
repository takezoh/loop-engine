"""The check program agents run is the engine's copy under its state home, named by its content."""
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import unittest

from engine_support import RUNNER, SRC, EngineCase, approve, produced, published_runner

from loop_engine import store
from loop_engine.store import CheckProgramChanged

with open(RUNNER, "rb") as _handle:
    CONTENT = _handle.read()
DIGEST = hashlib.sha256(CONTENT).hexdigest()


class PublishTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = os.path.join(os.path.realpath(self._tmp.name), "home", "published")

    def read(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_the_program_is_published_in_a_directory_named_by_its_content(self):
        path = store.publish_check_program(self.root, RUNNER)
        self.assertEqual(os.path.join(self.root, "check", DIGEST, "check_runner.py"), path)
        self.assertEqual(CONTENT, self.read(path))
        self.assertEqual(path, store.publish_check_program(self.root, RUNNER))  # once per engine, every engine
        self.assertEqual(["check_runner.py"], os.listdir(os.path.dirname(path)))

    def test_a_file_that_is_not_the_content_its_directory_names_is_neither_used_nor_overwritten(self):
        path = store.check_program_path(self.root, DIGEST)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"print('loop-engine-check unit tree=candidate digest=0 exit=0')\n")
        with self.assertRaises(CheckProgramChanged):
            store.publish_check_program(self.root, RUNNER)
        self.assertIn(b"exit=0", self.read(path))
        os.unlink(path)
        os.symlink(RUNNER, path)  # the right content, reached through a link the engine did not make
        with self.assertRaises(CheckProgramChanged):
            store.publish_check_program(self.root, RUNNER)
        self.assertTrue(os.path.islink(path))

    def test_engines_publishing_at_once_all_end_with_the_one_correct_file(self):
        for attempt in range(10):
            root = os.path.join(self.root, str(attempt))
            gate, paths = threading.Barrier(8), []

            def publish():
                gate.wait()
                paths.append(store.publish_check_program(root, RUNNER))
            threads = [threading.Thread(target=publish) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
            self.assertEqual([store.check_program_path(root, DIGEST)] * 8, paths)
            self.assertEqual(CONTENT, self.read(paths[0]))
            self.assertEqual(["check_runner.py"], os.listdir(os.path.dirname(paths[0])))
        root = os.path.join(self.root, "processes")
        code = ("import sys; sys.path.insert(0, sys.argv[1]); from loop_engine import store; "
                "print(store.publish_check_program(sys.argv[2], sys.argv[3]))")
        processes = [subprocess.Popen([sys.executable, "-I", "-c", code, SRC, root, RUNNER], stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL) for _ in range(2)]
        outputs = [process.communicate(timeout=30) for process in processes]
        self.assertEqual([(store.check_program_path(root, DIGEST) + "\n", "")] * 2, outputs)
        self.assertEqual(CONTENT, self.read(store.check_program_path(root, DIGEST)))


class CommandTest(EngineCase):
    def finishing(self, request, commands, cancelled):
        return produced(request, commands) if request.role == "producer" else approve(request)

    def test_the_command_a_role_receives_names_the_published_program_not_the_code_location(self):
        _engine, status = self.start(self.finishing)
        self.assertEqual("done", status["stop_reason"])
        published = published_runner(self.published)
        self.assertTrue(published.startswith(os.path.join(self.published, "check") + os.sep))
        producer = self.agents.requests[0].prompt
        self.assertIn(f"-I {published} ", producer)
        self.assertNotIn(RUNNER, producer)

    def test_a_program_changed_after_the_engine_published_it_is_never_handed_out(self):
        engine = self.engine(self.finishing)
        path = published_runner(self.published)
        os.chmod(path, 0o644)
        with open(path, "ab") as handle:
            handle.write(b"\nprint('loop-engine-check unit tree=candidate digest=0 exit=0')\n")
        self.assertTrue(engine.handle({"request": "start", "contract": self.contract()})["ok"])
        status = self.run_to_stop(engine)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertIn("CheckProgramChanged", status["detail"])
        self.assertEqual([], self.agents.requests)  # no role was given a command naming it
        with self.assertRaises(CheckProgramChanged):  # nor does a later engine start over it
            self.engine(self.finishing)


if __name__ == "__main__":
    unittest.main()
