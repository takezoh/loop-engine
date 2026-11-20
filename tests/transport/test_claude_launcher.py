"""The launcher that starts the Claude ACP adapter the engine installed. PATH and the installed adapter
are values; nothing here starts node, the adapter or claude."""
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from provider_transport import claude_adapter
from provider_transport.command_search import search_values
from provider_transport.launchers import claude_agent_acp as launcher


class LauncherTests(unittest.TestCase):
    def launch(self, environ, *, windows=False, files=(), verify=None):
        """Run the launcher's main in this process; returns (exit status, the argv it started).

        The environment each start received is in `self.environments`, the directories verified in
        `self.verified`."""
        started = []
        self.environments = []
        self.verified = []

        def execve(path, argv, env):
            self.assertEqual(argv[0], path)
            started.append(list(argv))
            self.environments.append(dict(env))
            raise SystemExit(0)

        def run(argv, check, env):
            started.append(list(argv))
            self.environments.append(dict(env))
            return subprocess.CompletedProcess(argv, 0)

        def verified(target):
            self.verified.append(target)
            if verify is not None:
                raise verify

        search = search_values(environ, windows=windows, runnable=set(files).__contains__)
        with patch.dict(os.environ, environ, clear=True), \
                patch.object(launcher, "is_windows", return_value=windows), \
                patch.object(launcher, "host_command_search", return_value=search), \
                patch.object(claude_adapter, "verify", side_effect=verified), \
                patch.object(launcher.os, "execve", side_effect=execve), \
                patch.object(launcher.subprocess, "run", side_effect=run):
            try:
                status = launcher.main(["ok"])
            except SystemExit as exit:
                status = exit.code
        return status, started

    def test_the_installed_adapter_runs_the_claude_on_path(self):
        environ = {"HOME": "/home/test", "PATH": "/opt/node/bin:/home/test/.local/bin", "KEEP": "1"}
        status, started = self.launch(
            environ, files={"/opt/node/bin/node", "/home/test/.local/bin/claude"})
        self.assertEqual(0, status)
        runtime = Path("/home/test/.local/share/loop-engine")
        self.assertEqual([["/opt/node/bin/node", str(runtime / claude_adapter.ENTRY_RELATIVE_PATH), "ok"]],
                         started)
        self.assertEqual([runtime], self.verified)
        self.assertEqual({**environ, "CLAUDE_CODE_EXECUTABLE": "/home/test/.local/bin/claude"},
                         self.environments[0])

    def test_the_install_time_runtime_override_is_used(self):
        environ = {"HOME": "/home/test", "PATH": "/opt/node/bin", "LOOP_ENGINE_RUNTIME_DIR": "/srv/shared runtime"}
        status, started = self.launch(environ, files={"/opt/node/bin/node", "/opt/node/bin/claude"})
        self.assertEqual(0, status)
        self.assertEqual(str(Path("/srv/shared runtime") / claude_adapter.ENTRY_RELATIVE_PATH), started[0][1])
        self.assertEqual([Path("/srv/shared runtime")], self.verified)

    def test_nothing_starts_when_the_installed_adapter_fails_its_check(self):
        status, started = self.launch(
            {"HOME": "/home/test", "PATH": "/opt/node/bin"},
            files={"/opt/node/bin/node", "/opt/node/bin/claude"},
            verify=RuntimeError("claude-adapter-package-mismatch"))
        self.assertEqual(127, status)
        self.assertEqual([], started)

    def test_nothing_starts_without_a_node_or_a_claude_on_path(self):
        for files in ({"/opt/node/bin/claude"}, {"/opt/node/bin/node"}):
            with self.subTest(files=sorted(files)):
                status, started = self.launch({"HOME": "/home/test", "PATH": "/opt/node/bin"}, files=files)
                self.assertEqual(127, status)
                self.assertEqual([], started)

    def test_on_windows_node_exe_starts_the_entry_and_shims_are_refused(self):
        environ = {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local", "Path": r"C:\node;C:\claude;C:\shims"}
        status, started = self.launch(environ, windows=True, files={r"C:\node\node.exe", r"C:\claude\claude.exe"})
        self.assertEqual(0, status)
        self.assertEqual(r"C:\node\node.exe", started[0][0])
        self.assertEqual(r"C:\claude\claude.exe", self.environments[0]["CLAUDE_CODE_EXECUTABLE"])
        for files in ({r"C:\shims\node.cmd", r"C:\claude\claude.exe"}, {r"C:\node\node.exe", r"C:\shims\claude.cmd"}):
            with self.subTest(files=sorted(files)):
                status, started = self.launch(environ, windows=True, files=files)
                self.assertEqual(127, status)
                self.assertEqual([], started)


if __name__ == "__main__":
    unittest.main()
