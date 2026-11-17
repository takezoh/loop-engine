"""Find a command by name on ``PATH``, the way the platform's own process start would.

``resolve_command`` is pure: the search path, the extensions and the test for a
runnable file come in as values. ``host_command_search`` builds those values
for this platform from the environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
import posixpath
from typing import Callable, Mapping

from .host_platform import is_windows



WINDOWS_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"
# Windows command shims run only through cmd.exe, which re-reads every argument as a command line.
WINDOWS_SHIM_EXTENSIONS = frozenset({".bat", ".cmd"})


@dataclass(frozen=True)
class CommandSearch:
    """How one platform finds a command: where to look, which names to try and what can run."""

    directories: tuple[str, ...]
    extensions: tuple[str, ...]
    runnable: Callable[[str], bool]
    windows: bool


def search_values(
    environ: Mapping[str, str], *, windows: bool, runnable: Callable[[str], bool]
) -> CommandSearch:
    """Purely read ``PATH`` and, on Windows, ``PATHEXT`` from environment values."""
    path = _variable(environ, "PATH", windows=windows) or ""
    separator = ";" if windows else ":"
    directories = tuple(part for part in path.split(separator) if part)
    if not windows:
        return CommandSearch(directories, ("",), runnable, windows=False)
    raw = _variable(environ, "PATHEXT", windows=True) or WINDOWS_DEFAULT_PATHEXT
    extensions = tuple(part.lower() for part in raw.split(";") if part.startswith("."))
    return CommandSearch(directories, extensions, runnable, windows=True)


def _variable(environ: Mapping[str, str], name: str, *, windows: bool) -> str | None:
    """Windows environment names are case-insensitive: ``Path`` is ``PATH``."""
    if not windows:
        return environ.get(name)
    return next((value for key, value in environ.items() if key.upper() == name), None)


def resolve_command(name: str, search: CommandSearch) -> str | None:
    """The file ``name`` starts, or None.

    A name with a directory part is not searched for on ``PATH``. On Windows a
    name that already ends in one of ``PATHEXT``'s extensions is tried as it
    is, and otherwise each extension is appended in ``PATHEXT`` order; the
    current directory is not searched.
    """
    paths = ntpath if search.windows else posixpath
    separators = ("\\", "/") if search.windows else ("/",)
    candidates = [name + extension for extension in _names(name, search)]
    if any(separator in name for separator in separators):
        return next((candidate for candidate in candidates if search.runnable(candidate)), None)
    for directory in search.directories:
        for candidate in candidates:
            path = paths.join(directory, candidate)
            if search.runnable(path):
                return path
    return None


def _names(name: str, search: CommandSearch) -> tuple[str, ...]:
    if not search.windows:
        return ("",)
    if ntpath.splitext(name)[1].lower() in search.extensions:
        return ("",)
    return search.extensions


def is_command_shim(path: str) -> bool:
    """Whether a resolved Windows command is a batch file that only cmd.exe can run."""
    return ntpath.splitext(path)[1].lower() in WINDOWS_SHIM_EXTENSIONS


def host_command_search(environ: Mapping[str, str] | None = None) -> CommandSearch:
    """The command search of this platform, from this process's environment by default."""
    env = os.environ if environ is None else environ
    if is_windows():
        return search_values(env, windows=True, runnable=os.path.isfile)
    return search_values(
        env, windows=False,
        runnable=lambda path: os.path.isfile(path) and os.access(path, os.X_OK),
    )
