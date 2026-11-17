"""Descriptor-held POSIX working-directory authority.

Only this module performs POSIX filesystem effects for the working-directory
authority. Traversal is relative to an open directory descriptor and never
follows a symlink; a child starts in ``/proc/self/fd/<fd>`` while the
descriptor is held, so the directory cannot be swapped between admission and
start.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePath
from threading import RLock

from .directory_authority import CwdDenied, CwdLease


TEMPORARY_ROOT = "/tmp"


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def _identity(fd: int) -> tuple[int, int]:
    stat = os.fstat(fd)
    return stat.st_dev, stat.st_ino


class HeldDescriptor:
    """One admitted directory held open by its descriptor."""

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self.identity = _identity(fd)

    @property
    def spawn_path(self) -> str:
        if _identity(self._fd) != self.identity:
            raise CwdDenied("cwd-lease-identity-changed")
        return f"/proc/self/fd/{self._fd}"

    def admitted_path(self) -> str:
        """The path of the held directory, rechecked against its identity.

        A shared provider host receives cwd as a path, so the path must still
        name the admitted directory at the moment it is handed over.
        """
        path = os.readlink(f"/proc/self/fd/{self._fd}")
        if path.endswith(" (deleted)") or not os.path.isabs(path):
            raise CwdDenied("cwd-denied:directory-removed")
        try:
            stat = os.stat(path)
        except OSError as exc:
            raise CwdDenied("cwd-denied:directory-unavailable") from exc
        if (stat.st_dev, stat.st_ino) != self.identity:
            raise CwdDenied("cwd-denied:identity-changed")
        return path

    def close(self) -> None:
        os.close(self._fd)


class PosixDirectoryAuthority:
    """Origin authority, and the temporary root when allowed, held by descriptor."""

    def __init__(
        self, origin_fd: int, *, display_path: Path, tmp_fd: int | None = None
    ) -> None:
        self._origin_fd = origin_fd
        self._tmp_fd = tmp_fd
        self.display_path = display_path
        self.identity = _identity(origin_fd)
        self._closed = False
        self._lock = RLock()

    @classmethod
    def capture(
        cls, cwd: str | os.PathLike[str] | None = None, *, allow_tmp: bool = False
    ) -> "PosixDirectoryAuthority":
        display = Path.cwd() if cwd is None else Path(cwd)
        fd = os.open(display, _directory_flags())
        try:
            tmp_fd = os.open(TEMPORARY_ROOT, _directory_flags()) if allow_tmp else None
        except OSError:
            os.close(fd)
            raise
        return cls(fd, display_path=display.absolute(), tmp_fd=tmp_fd)

    def admit(self, requested: str | os.PathLike[str]) -> CwdLease:
        raw = os.fspath(requested)
        if not raw or "\x00" in raw:
            raise CwdDenied("cwd-denied:invalid")
        with self._lock:
            if self._closed:
                raise CwdDenied("cwd-denied:authority-closed")
            root_fd, relative = self._relative_components(raw)
            current = os.dup(root_fd)
        try:
            for part in relative:
                next_fd = os.open(part, _directory_flags(), dir_fd=current)
                os.close(current)
                current = next_fd
            return CwdLease(HeldDescriptor(current))
        except OSError as exc:
            try:
                os.close(current)
            except OSError:
                pass
            raise CwdDenied("cwd-denied:not-descendant-or-symlink") from exc

    def _relative_components(self, raw: str) -> tuple[int, tuple[str, ...]]:
        candidate = PurePath(raw)
        parts: tuple[str, ...]
        root_fd = self._origin_fd
        if candidate.is_absolute():
            try:
                parts = Path(raw).relative_to(self.display_path).parts
            except ValueError as exc:
                if self._tmp_fd is None:
                    raise CwdDenied("cwd-denied:outside-origin") from exc
                try:
                    parts = Path(raw).relative_to(TEMPORARY_ROOT).parts
                except ValueError as tmp_exc:
                    raise CwdDenied("cwd-denied:outside-origin") from tmp_exc
                root_fd = self._tmp_fd
        else:
            parts = candidate.parts
        if any(part in {"..", ""} for part in parts):
            raise CwdDenied("cwd-denied:traversal")
        return root_fd, tuple(part for part in parts if part != ".")

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.close(self._origin_fd)
            if self._tmp_fd is not None:
                os.close(self._tmp_fd)
            self._closed = True
