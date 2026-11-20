"""The launcher that starts the Claude ACP adapter of the user's environment. PATH is a value; nothing
here starts node, the adapter or claude."""
import os
import subprocess
import unittest
from unittest.mock import patch

from provider_transport.command_search import search_values
from provider_transport.launchers import claude_agent_acp as launcher

WINDOWS_ENTRY = r"C:\npm\node_modules\@agentclientprotocol\claude-agent-acp\dist\index.js"


class LauncherTests(unittest.TestCase):
    def launch(self, environ, *, windows=False, files=()):
        """Run the launcher's main in this process; returns (exit status, the argv it started).

        The environment each start received is in `self.environments`."""
        started = []
        self.environments = []

        def execve(path, argv, env):
            self.assertEqual(argv[0], path)
            started.append(list(argv))
            self.environments.append(dict(env))
            raise SystemExit(0)

        def run(argv, check, env):
            started.append(list(argv))
            self.environments.append(dict(env))
            return subprocess.CompletedProcess(argv, 0)

        search = search_values(environ, windows=windows, runnable=set(files).__contains__)
        with patch.dict(os.environ, environ, clear=True), \
                patch.object(launcher, "is_windows", return_value=windows), \
                patch.object(launcher, "host_command_search", return_value=search), \
                patch.object(launcher.os, "execve", side_effect=execve), \
                patch.object(launcher.subprocess, "run", side_effect=run):
            try:
                status = launcher.main(["ok"])
            except SystemExit as exit:
                status = exit.code
        return status, started

    def test_the_adapter_on_path_runs_the_claude_on_path(self):
        environ = {"HOME": "/home/test", "PATH": "/opt/npm/bin:/home/test/.local/bin", "KEEP": "1"}
        status, started = self.launch(
            environ, files={"/opt/npm/bin/claude-agent-acp", "/home/test/.local/bin/claude"})
        self.assertEqual(0, status)
        self.assertEqual([["/opt/npm/bin/claude-agent-acp", "ok"]], started)
        self.assertEqual({**environ, "CLAUDE_CODE_EXECUTABLE": "/home/test/.local/bin/claude"},
                         self.environments[0])

    def test_nothing_starts_without_a_claude_on_path(self):
        status, started = self.launch({"PATH": "/opt/npm/bin"}, files={"/opt/npm/bin/claude-agent-acp"})
        self.assertEqual(127, status)
        self.assertEqual([], started)

    def test_nothing_starts_without_the_adapter_on_path(self):
        status, started = self.launch({"PATH": "/usr/bin"}, files={"/usr/bin/claude"})
        self.assertEqual(127, status)
        self.assertEqual([], started)

    def test_on_windows_the_shims_entry_starts_with_node_exe(self):
        environ = {"Path": r"C:\npm;C:\node;C:\claude"}
        status, started = self.launch(environ, windows=True, files={
            r"C:\npm\claude-agent-acp.cmd", WINDOWS_ENTRY, r"C:\node\node.exe", r"C:\claude\claude.exe"})
        self.assertEqual(0, status)
        self.assertEqual([[r"C:\node\node.exe", WINDOWS_ENTRY, "ok"]], started)
        self.assertEqual(r"C:\claude\claude.exe", self.environments[0]["CLAUDE_CODE_EXECUTABLE"])

    def test_on_windows_an_adapter_that_is_not_a_shim_starts_as_it_is(self):
        status, started = self.launch({"Path": r"C:\tools"}, windows=True, files={
            r"C:\tools\claude-agent-acp.exe", r"C:\tools\claude.exe"})
        self.assertEqual(0, status)
        self.assertEqual([[r"C:\tools\claude-agent-acp.exe", "ok"]], started)

    def test_on_windows_a_shim_without_its_entry_or_a_node_exe_starts_nothing(self):
        environ = {"Path": r"C:\npm;C:\node;C:\claude"}
        for files in ({r"C:\npm\claude-agent-acp.cmd", r"C:\node\node.exe", r"C:\claude\claude.exe"},
                      {r"C:\npm\claude-agent-acp.cmd", WINDOWS_ENTRY, r"C:\claude\claude.exe"},
                      {r"C:\npm\claude-agent-acp.cmd", WINDOWS_ENTRY, r"C:\node\node.cmd", r"C:\claude\claude.exe"}):
            with self.subTest(files=sorted(files)):
                status, started = self.launch(environ, windows=True, files=files)
                self.assertEqual(127, status)
                self.assertEqual([], started)

    def test_on_windows_a_claude_shim_is_refused(self):
        status, started = self.launch({"Path": r"C:\tools;C:\shims"}, windows=True, files={
            r"C:\tools\claude-agent-acp.exe", r"C:\shims\claude.cmd"})
        self.assertEqual(127, status)
        self.assertEqual([], started)


if __name__ == "__main__":
    unittest.main()
