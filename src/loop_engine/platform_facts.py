"""The facts of the platform the engine runs on. The only module that tells platforms apart.

The engine runs on Linux and on native Windows. `CURRENT` is chosen here once; every other
module receives what it needs from a `Platform`: facts as values, and the two effects that
differ by platform (`place_new`, `lock`) as adapters. Decisions take the values as arguments
and never look at the platform themselves. Each adapter takes the OS calls it makes as an
argument, so the Windows adapters run against a fake of those calls on any machine.
"""
import errno
import os
import shlex
import stat
from dataclasses import dataclass
from typing import Callable, Optional

# Windows process creation flags (winbase.h): a new process group, without the caller's console.
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008


@dataclass(frozen=True)
class Platform:
    path_style: str  # how an absolute path looks: "posix" (/a/b) or "windows" (C:\a\b, \\server\share\a)
    null_device: str  # the file that reads as empty
    home: Optional[str]  # HOME for git when the environment has none; None leaves HOME unset
    quote: Callable[[list], str]  # argv -> one command line, as the agent is told to run it
    detached: dict  # Popen keywords that start a child outside the caller's session or console
    place_new: Callable[[str, str], None]  # (temporary, path): see `link_new`
    lock: Callable  # (open file): see `flock`
    lock_held: frozenset  # the errnos `lock` raises when another process holds the lock


def windows_command_line(argv):
    """argv as one command line by the rules the Microsoft C runtime uses to split it again.

    The same text `subprocess.list2cmdline` gives: an argument with a space or a tab, or an empty
    one, is put in double quotes; a double quote is escaped with a backslash, and backslashes are
    doubled where they precede a double quote."""
    parts = []
    for argument in argv:
        quoted = not argument or " " in argument or "\t" in argument
        text, slashes = [], 0
        for char in argument:
            if char == "\\":
                slashes += 1
                continue
            text.append("\\" * (slashes * 2 + 1) + '"' if char == '"' else "\\" * slashes + char)
            slashes = 0
        text.append("\\" * (slashes * 2 if quoted else slashes))
        parts.append('"' + "".join(text) + '"' if quoted else "".join(text))
    return " ".join(parts)


def link_new(temporary, path, files=os):
    """Give the file `temporary` the name `path` unless `path` exists; `temporary` is gone afterwards.

    An error other than an existing `path` is raised, also with `temporary` gone. POSIX: a hard
    link never replaces a file already there."""
    try:
        files.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        files.unlink(temporary)


def rename_new(temporary, path, files=os):
    """As `link_new`. Windows: a rename never replaces a file already there, and a read-only file
    must be made writable before it can be removed."""
    try:
        files.rename(temporary, path)
    except OSError as error:
        files.chmod(temporary, stat.S_IREAD | stat.S_IWRITE)
        files.unlink(temporary)
        if not isinstance(error, FileExistsError):
            raise


def flock(handle, fcntl=None):
    """Lock the open file without waiting, for as long as it stays open, or raise OSError."""
    if fcntl is None:
        import fcntl
    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def byte_lock(handle, msvcrt=None):
    """As `flock`, with the Windows C runtime's lock on the file's first byte."""
    if msvcrt is None:
        import msvcrt
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)


POSIX = Platform(path_style="posix", null_device="/dev/null", home="/", quote=shlex.join,
                 detached={"start_new_session": True}, place_new=link_new, lock=flock,
                 lock_held=frozenset({errno.EAGAIN, errno.EWOULDBLOCK}))
WINDOWS = Platform(path_style="windows", null_device="nul", home=None, quote=windows_command_line,
                   detached={"creationflags": CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS},
                   place_new=rename_new, lock=byte_lock,
                   lock_held=frozenset({errno.EACCES}))  # msvcrt.locking with LK_NBLCK on a locked byte
CURRENT = WINDOWS if os.name == "nt" else POSIX
