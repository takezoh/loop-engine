"""Finding an agent's command on PATH / PATHEXT, from environment values, on any platform."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

from provider_transport.command_search import (
    host_command_search,
    is_command_shim,
    resolve_command,
    search_values,
)
from provider_transport.host_platform import is_windows
from provider_transport.windows_process import WindowsJobCell, WindowsProcessError
from .windows_api_fake import FakeProcessApi


def windows_search(files, path=r"C:\npm;C:\Tools", pathext=".COM;.EXE;.BAT;.CMD"):
    environ = {"Path": path}
    if pathext is not None:
        environ["PATHEXT"] = pathext
    return search_values(environ, windows=True, runnable=lambda candidate: candidate in files)


class WindowsCommandSearchTests(unittest.TestCase):
    def test_an_npm_shim_is_found_through_pathext(self):
        search = windows_search({r"C:\npm\claude.cmd", r"C:\npm\claude"})
        self.assertEqual(r"C:\npm\claude.cmd", resolve_command("claude", search))

    def test_pathext_order_decides_between_an_exe_and_a_shim_in_one_directory(self):
        files = {r"C:\Tools\codex.exe", r"C:\Tools\codex.cmd"}
        self.assertEqual(r"C:\Tools\codex.exe", resolve_command("codex", windows_search(files)))
        self.assertEqual(
            r"C:\Tools\codex.cmd",
            resolve_command("codex", windows_search(files, pathext=".CMD;.EXE")),
        )

    def test_path_order_decides_between_directories(self):
        files = {r"C:\npm\pi.cmd", r"C:\Tools\pi.exe"}
        self.assertEqual(r"C:\npm\pi.cmd", resolve_command("pi", windows_search(files)))

    def test_a_name_with_its_extension_is_tried_as_it_is(self):
        files = {r"C:\Tools\node.exe", r"C:\Tools\node.exe.com"}
        self.assertEqual(r"C:\Tools\node.exe", resolve_command("node.exe", windows_search(files)))

    def test_a_name_with_a_directory_is_not_searched_on_path(self):
        files = {r"C:\npm\claude.cmd", r"D:\own\claude.exe"}
        search = windows_search(files)
        self.assertEqual(r"D:\own\claude.exe", resolve_command(r"D:\own\claude", search))
        self.assertIsNone(resolve_command(r".\claude", search))

    def test_without_pathext_the_windows_default_extensions_apply(self):
        self.assertEqual(
            r"C:\npm\claude.cmd", resolve_command("claude", windows_search({r"C:\npm\claude.cmd"}, pathext=None))
        )

    def test_an_absent_command_is_not_found(self):
        self.assertIsNone(resolve_command("codex", windows_search(set())))

    def test_only_batch_files_are_shims(self):
        self.assertTrue(is_command_shim(r"C:\npm\claude.CMD"))
        self.assertTrue(is_command_shim(r"C:\npm\old.bat"))
        self.assertFalse(is_command_shim(r"C:\Tools\codex.exe"))


class PosixCommandSearchTests(unittest.TestCase):
    def test_the_first_runnable_file_on_path_is_found_without_extensions(self):
        files = {"/opt/b/codex", "/opt/b/codex.exe"}
        search = search_values({"PATH": "/opt/a:/opt/b"}, windows=False, runnable=files.__contains__)
        self.assertEqual("/opt/b/codex", resolve_command("codex", search))
        self.assertIsNone(resolve_command("claude", search))


class HostCommandSearchTests(unittest.TestCase):
    def test_the_host_search_skips_a_file_this_platform_cannot_run(self):
        """POSIX runs a file that has its executable bit; Windows a file whose extension PATHEXT names."""
        with tempfile.TemporaryDirectory() as temporary:
            first, second = Path(temporary, "first"), Path(temporary, "second")
            first.mkdir()
            second.mkdir()
            (first / "agent").write_text("#!/bin/sh\n")
            runnable = second / ("agent.exe" if is_windows() else "agent")
            runnable.write_text("#!/bin/sh\n")
            runnable.chmod(0o755)
            search = host_command_search({"PATH": f"{first}{os.pathsep}{second}", "PATHEXT": ".exe"})
            self.assertEqual(str(runnable), resolve_command("agent", search))


class WindowsCellStartTests(unittest.TestCase):
    """The Windows cell starts the program PATH / PATHEXT names, and refuses a batch shim."""

    def test_the_cell_starts_the_program_found_on_the_childs_path(self):
        api = FakeProcessApi()
        cell = WindowsJobCell(api=api, runnable={r"C:\Tools\codex.exe"}.__contains__)
        self.addCleanup(cell.close)
        process = cell.spawn(("codex", "app-server"), cwd="C:\\work", options={"env": {"PATH": r"C:\npm;C:\Tools"}})
        launches = [row for row in api.events if row[0] == "create-suspended-in-job"]
        self.assertEqual((r"C:\Tools\codex.exe", "app-server"), tuple(launches[0][2]))
        api.active[process.job] = 0
        self.assertTrue(cell.wait_empty(1.0))

    def test_the_cell_refuses_a_program_that_resolves_to_a_batch_shim(self):
        api = FakeProcessApi()
        cell = WindowsJobCell(api=api, runnable={r"C:\npm\claude.cmd"}.__contains__)
        self.addCleanup(cell.close)
        with self.assertRaisesRegex(WindowsProcessError, "windows-command-shim-refused"):
            cell.spawn(("claude",), cwd="C:\\work", options={"env": {"Path": r"C:\npm"}})
        with self.assertRaisesRegex(WindowsProcessError, "windows-command-not-found"):
            cell.spawn(("pi",), cwd="C:\\work", options={"env": {"Path": r"C:\npm"}})
        self.assertEqual([], [row for row in api.events if row[0] == "create-suspended-in-job"])


if __name__ == "__main__":
    unittest.main()
