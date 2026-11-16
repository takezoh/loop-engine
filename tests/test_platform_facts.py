"""The platform's facts reach the engine as values: Windows facts are checked here on any machine."""
import ast
import errno
import functools
import os
import stat
import subprocess
import tempfile
import unittest

from engine_support import RUNNER, SRC

from loop_engine import facts, platform_facts, store
from loop_engine.platform_facts import POSIX, WINDOWS
from loop_engine.unit import Unit
from loop_engine.worktree import unexpected_entries

PACKAGE = os.path.join(SRC, "loop_engine")
CHECKS = {"id": "checks", "required": True, "by": "checks", "subject": "candidate",
          "checks": [{"name": "unit", "expect": "pass"}, {"name": "unit", "expect": "fails_on_base"}]}
IMPLEMENT = {"id": "implement", "establishes": ["checks"]}


class CheckCommandTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = os.path.join(os.path.realpath(temporary.name), "state home")
        self.root = os.path.join(home, "records", "loop-engine")
        self.published = os.path.join(home, "loop-engine")
        self.program = store.publish_check_program(self.published, RUNNER)

    def commands(self, platform, python):
        unit = Unit(store.UnitStore(self.root, self.published, "u1"), {"facts": [CHECKS]}, {"ignore": []}, git=None, agents=None,
                    templates=None, runner=(python, self.program), worktrees=None, quote=platform.quote,
                    clock=None, interrupt=None, halting=None)
        return unit._check_commands(IMPLEMENT)

    def test_the_check_command_is_quoted_for_the_platform_the_facts_name(self):
        python = "C:\\Program Files\\Python312\\python.exe"
        checks = os.path.join(self.published, "units", "u1", "checks.json")
        line = subprocess.list2cmdline([python, "-I", self.program, checks])
        self.assertEqual([line + " unit", line + " unit --base"], self.commands(WINDOWS, python))
        self.assertTrue(line.startswith('"C:\\Program Files\\Python312\\python.exe" -I "'), line)
        self.assertNotIn("'", line)
        posix = self.commands(POSIX, "/usr/bin/python3")[0]
        self.assertTrue(posix.startswith(f"/usr/bin/python3 -I '{self.program}' "), posix)

    def test_the_windows_command_line_is_the_one_the_microsoft_c_runtime_splits_back(self):
        for argv in (["a"], [""], ["a b", "c"], ["a\\b"], ['a"b'], ["a\\\\\"b"], ["trailing\\"], ["tail \\"],
                     ["tab\there", "\\\\server\\share\\x y\\"], ['"quoted"', "back\\\\", "C:\\x\\"]):
            with self.subTest(argv):
                self.assertEqual(subprocess.list2cmdline(argv), platform_facts.windows_command_line(argv))


class PlatformValuesTest(unittest.TestCase):
    def test_each_platform_states_its_facts(self):
        self.assertEqual(("posix", "/dev/null", "/"), (POSIX.path_style, POSIX.null_device, POSIX.home))
        self.assertEqual(("windows", "nul", None), (WINDOWS.path_style, WINDOWS.null_device, WINDOWS.home))
        self.assertEqual({"start_new_session": True}, POSIX.detached)
        self.assertEqual({"creationflags": 0x00000200 | 0x00000008}, WINDOWS.detached)
        # A Windows program on PATH is its name plus .exe; a batch file would be parsed again by cmd.exe.
        self.assertEqual((("",), (".exe",)), (POSIX.program_suffixes, WINDOWS.program_suffixes))
        self.assertIn(platform_facts.CURRENT, (POSIX, WINDOWS))
        for platform in (POSIX, WINDOWS):
            self.assertIn(platform.path_style, facts.PATH_STYLES)

    def test_only_the_platform_facts_tell_platforms_apart(self):
        for name in sorted(os.listdir(PACKAGE)):
            if not name.endswith(".py") or name == "platform_facts.py":
                continue
            with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            named = {ast.unparse(node) for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            self.assertFalse(named & {"os.name", "os.uname", "sys.platform", "sys.getwindowsversion", "os.devnull",
                                      "platform.system"}, name)
            imported = {alias.name for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
                        for alias in node.names} | {node.module for node in ast.walk(tree)
                                                    if isinstance(node, ast.ImportFrom) and node.level == 0}
            self.assertFalse(imported & {"fcntl", "msvcrt", "winreg", "_winapi", "ctypes", "ctypes.wintypes", "termios",
                                         "pwd", "grp", "resource", "posix", "nt", "shlex", "platform"}, name)
            text = ast.unparse(tree)
            for assumption in ("/dev/null", "start_new_session", "os.link"):
                self.assertNotIn(assumption, text, name)


class FakeWindowsFiles:
    """The Windows file calls `rename_new` makes, over real files: a rename never replaces a file, and
    a read-only file cannot be removed."""

    def rename(self, source, target):
        if os.path.lexists(target):
            raise FileExistsError(errno.EEXIST, "Cannot create a file when that file already exists", target)
        os.rename(source, target)

    def chmod(self, path, mode):
        os.chmod(path, mode)

    def unlink(self, path):
        if not os.stat(path).st_mode & stat.S_IWRITE:
            raise PermissionError(errno.EACCES, "Access is denied", path)
        os.unlink(path)


class FakeMsvcrt:
    """`msvcrt.locking` over real files: the first byte of a file is held by one open file at a time."""
    LK_NBLCK = 2

    def __init__(self):
        self.holders = {}

    @staticmethod
    def _file(descriptor):
        info = os.fstat(descriptor)
        return info.st_dev, info.st_ino

    def locking(self, descriptor, mode, size):
        assert (mode, size) == (self.LK_NBLCK, 1)
        key, holder = self._file(descriptor), None
        holder = self.holders.get(key)
        if holder is not None and holder != descriptor:
            try:
                still = self._file(holder) == key
            except OSError:
                still = False
            if still:
                raise OSError(errno.EACCES, "Permission denied")
        self.holders[key] = descriptor


def adapters():
    """(name, platform, place_new, lock): each platform's adapters on this machine's OS calls, and the
    Windows adapters on a fake of the Windows calls."""
    real = platform_facts.CURRENT
    yield "this machine", real, real.place_new, real.lock
    windows = functools.partial(platform_facts.rename_new, files=FakeWindowsFiles())
    yield "windows on fake calls", WINDOWS, windows, functools.partial(platform_facts.byte_lock, msvcrt=FakeMsvcrt())


class AdapterContractTest(unittest.TestCase):
    """One contract for the effects that differ by platform, applied to every adapter."""

    def test_place_new_never_replaces_a_file_and_never_leaves_the_temporary(self):
        for name, _platform, place_new, _lock in adapters():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                target, first, second = (os.path.join(directory, n) for n in ("target", "first", "second"))
                for path, data in ((first, b"one"), (second, b"two")):
                    with open(path, "wb") as handle:
                        handle.write(data)
                    os.chmod(path, 0o444)
                place_new(first, target)
                place_new(second, target)  # the name is taken: the file there stays as it is
                with open(target, "rb") as handle:
                    self.assertEqual(b"one", handle.read())
                self.assertEqual(["target"], os.listdir(directory))
                third = os.path.join(directory, "third")
                with open(third, "wb") as handle:
                    handle.write(b"three")
                with self.assertRaises(FileNotFoundError):  # any other error is raised
                    place_new(third, os.path.join(directory, "missing", "target"))
                self.assertEqual(["target"], os.listdir(directory))

    def test_lock_is_held_by_one_open_file_and_names_the_holder_by_the_platforms_errno(self):
        for name, platform, _place_new, lock in adapters():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                path = os.path.join(directory, "engine.lock")
                first, second = open(path, "a+b"), open(path, "a+b")
                self.addCleanup(second.close)
                lock(first)
                with self.assertRaises(OSError) as refused:
                    lock(second)
                self.assertTrue(store.lock_contended(refused.exception, platform.lock_held))
                first.close()
                lock(second)


class TrailingNewlineTest(unittest.TestCase):
    def test_store_unit_ids_and_unit_directories_refuse_a_trailing_newline(self):
        with tempfile.TemporaryDirectory() as root:
            store.unit_dir(root, "u1")
            with self.assertRaises(ValueError):
                store.unit_dir(root, "u1\n")
            os.makedirs(os.path.join(root, "units", "u1"))
            os.makedirs(os.path.join(root, "units", "u2\n"))
            self.assertEqual(["u1"], store.list_units(root))
            namespace = os.path.join(root, "worktrees", "0" * 64)
            worktree = os.path.join(namespace, "u1-" + "0" * 16)
            os.makedirs(worktree)
            self.assertEqual([], unexpected_entries(os.path.join(root, "worktrees"), worktree))
            os.makedirs(worktree + "\n")
            self.assertEqual([worktree + "\n"], unexpected_entries(os.path.join(root, "worktrees"), worktree))


if __name__ == "__main__":
    unittest.main()
