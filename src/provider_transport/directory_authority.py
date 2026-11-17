"""The working-directory authority a provider process starts under, and the choice of its adapter.

An authority holds its origin directory, and the temporary root when allowed,
and admits only directories reached from them without following a link. A
lease holds one admitted directory until the process that starts in it has
started.
"""

from __future__ import annotations

import os
from threading import RLock
from typing import Protocol


class CwdDenied(PermissionError):
    """A requested working directory is not admitted, or a lease no longer owns it."""


class HeldDirectory(Protocol):
    """An adapter's open handle on one admitted directory."""

    identity: tuple[int, int]

    @property
    def spawn_path(self) -> str:
        """The path a child process starts in while this handle is held."""
        ...

    def admitted_path(self) -> str:
        """The absolute path that names this directory now; raises ``CwdDenied`` when none does."""
        ...

    def close(self) -> None: ...


class CwdLease:
    """Move-only owner of one admitted directory, the same for every adapter."""

    def __init__(self, held: HeldDirectory) -> None:
        self._held = held
        self.identity = held.identity
        self._consumed = False
        self._closed = False
        self._lock = RLock()

    def admitted_path(self) -> str:
        """The absolute path of the admitted directory, rechecked, for a host that takes a path."""
        with self._lock:
            if self._closed or self._consumed:
                raise CwdDenied("cwd-lease-not-owned")
            return self._held.admitted_path()

    def consume_for_spawn(self) -> str:
        """The path to start one process in; the directory stays held until ``close``."""
        with self._lock:
            if self._closed or self._consumed:
                raise CwdDenied("cwd-lease-already-consumed")
            path = self._held.spawn_path
            self._consumed = True
            return path

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._held.close()

    def __enter__(self) -> "CwdLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class CwdAuthority(Protocol):
    """Admits working directories under a captured origin and optional temporary root."""

    def admit(self, requested: str | os.PathLike[str]) -> CwdLease:
        """A lease on ``requested`` (relative to the origin, or absolute under a held root); raises ``CwdDenied``."""
        ...

    def close(self) -> None: ...


def create_cwd_authority(
    cwd: str | os.PathLike[str] | None = None, *, allow_tmp: bool = False
) -> CwdAuthority:
    """The working-directory authority of this platform, holding ``cwd`` (this process's cwd when None)."""
    if os.name == "nt":
        from .windows_filesystem import WindowsDirectoryAuthority
        return WindowsDirectoryAuthority.capture(cwd, allow_tmp=allow_tmp)
    from .posix_filesystem import PosixDirectoryAuthority
    return PosixDirectoryAuthority.capture(cwd, allow_tmp=allow_tmp)
