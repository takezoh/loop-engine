"""The process cell every provider process starts in, and the choice of its adapter."""

from __future__ import annotations

import os
from typing import Any, Iterable, Mapping, Protocol, Sequence


class CellMembership(Protocol):
    """Starting, owning and ending the provider processes of one cell.

    A root is a process ``spawn`` started. Its identity names that exact
    process, so a reused PID never matches it.
    """

    def spawn(
        self, command: Sequence[str], *, cwd: str, options: Mapping[str, object]
    ) -> Any:
        """Start ``command`` in ``cwd`` as a root of this cell; returns a Popen-like process.

        When it raises, any process it may have created stays unconfirmed until
        ``resolve_unconfirmed_empty`` proves it gone.
        """
        ...

    def confirm(self, pid: int) -> bool:
        """Whether ``pid`` is a root this cell started and still owns."""
        ...

    def process_identity(self, pid: int) -> str | None:
        """The identity of the process ``pid`` names now; None when it cannot be observed."""
        ...

    def process_exists(self, pid: int) -> bool:
        """Whether a process this cell must still account for runs under ``pid``."""
        ...

    def release_certain(self, pid: int | None, identity: str | None) -> bool:
        """Whether no member of the cell can belong to the ended root ``pid``/``identity``."""
        ...

    def terminate_roots(self, roots: Iterable[tuple[int, str | None]]) -> None:
        """End each named ``(pid, identity)`` root with its descendants, and no other process."""
        ...

    def terminate_root(self, pid: int) -> None:
        """End one root this cell started, with its descendants."""
        ...

    def terminate_all(self) -> None:
        """End every member of the cell."""
        ...

    def resolve_unconfirmed_empty(self, timeout: float) -> bool:
        """End the processes failed starts left unconfirmed; True once none is left."""
        ...

    def wait_empty(self, timeout: float) -> bool:
        """Wait until the cell has no member; False when ``timeout`` passes first."""
        ...

    def close(self) -> None:
        """Refuse further starts."""
        ...


def create_cell_membership() -> CellMembership:
    """The process cell of this platform: a Windows Job Object cell, otherwise the Linux subreaper cell."""
    if os.name == "nt":
        from .windows_process import WindowsJobCell
        return WindowsJobCell.from_environment()
    from .linux_process import ProcSubreaperCell
    return ProcSubreaperCell()
