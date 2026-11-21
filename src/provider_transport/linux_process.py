"""Linux process cell: this process is the child subreaper of every provider it starts.

Only this module performs Linux process effects for the subsession runtime:
``prctl``, ``/proc``, process groups, pidfds and signals.
"""

from __future__ import annotations

import ctypes
import errno
import functools
import os
from pathlib import Path
import platform
import signal
import subprocess
from threading import RLock
import time
from typing import Any, Iterable, Mapping, Sequence


PR_SET_CHILD_SUBREAPER = 36
PR_GET_CHILD_SUBREAPER = 37
# The kernel's numbers for pidfd_send_signal and pidfd_open. The cell makes these calls itself, so it
# needs a kernel with them (Linux 5.3), not a Python built with their wrappers. Calls added since
# Linux 5.1 have these numbers on every architecture but those whose call tables are offset.
SYS_PIDFD_SEND_SIGNAL = 424
SYS_PIDFD_OPEN = 434
OFFSET_CALL_TABLES = ("alpha", "ia64", "mips")
# pidfd_send_signal flag (Linux 6.9): signal the process group whose ID is the pidfd's process.
PIDFD_SIGNAL_PROCESS_GROUP = 4
# Rescans of a group whose members are signalled one by one, for members forked during a scan.
GROUP_SCAN_LIMIT = 8


class ProcessObservationUncertain(RuntimeError):
    """The process cell cannot be proven empty from the current /proc scan."""


@functools.cache
def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    return libc


def enable_subreaper() -> None:
    """Make this process the adoption point for orphaned descendants."""
    libc = _libc()
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        error = ctypes.get_errno()
        raise RuntimeError("subsession-subreaper-unavailable") from OSError(
            error, os.strerror(error)
        )
    observed = ctypes.c_int(0)
    if libc.prctl(
        PR_GET_CHILD_SUBREAPER, ctypes.byref(observed), 0, 0, 0
    ) != 0 or observed.value != 1:
        raise RuntimeError("subsession-subreaper-unavailable")


class _StartedRoot:
    """A root ``spawn`` started: its identity and a pidfd naming exactly that process."""

    __slots__ = ("identity", "pidfd")

    def __init__(self, identity: str, pidfd: int) -> None:
        self.identity = identity
        self.pidfd = pidfd


class ProcSubreaperCell:
    """Membership derived from this process's kernel process ancestry.

    This process is a child subreaper. Provider roots remain adapter-owned
    direct children, each the leader of its own session and process group. If
    one exits before a ``setsid`` descendant, the kernel reparents that
    descendant to this process, so it remains a member until reaped.

    Every signal goes through a pidfd, never a bare PID. ``spawn`` opens the
    root's pidfd while the root is an unreaped child of this process, so the
    pidfd names that process even after its PID is reused. A root is started
    with ``setsid``: it leads its own session and process group, and a session
    leader cannot move to another group, so the group whose ID is the root's
    pidfd is the group this cell started for it.
    """

    def __init__(self, *, root_pid: int | None = None) -> None:
        if not Path("/proc/self/stat").is_file():
            raise RuntimeError("subsession-proc-cell-unavailable")
        if not _pidfd_available():
            raise RuntimeError("subsession-pidfd-unavailable")
        enable_subreaper()
        self.root_pid = os.getpid() if root_pid is None else root_pid
        self._roots: dict[int, str] = {}
        self._started: dict[int, _StartedRoot] = {}
        self._group_signal = True
        # Identities of processes seen descending from, or in the session of, a root this cell started.
        self._lineage: set[str] = set()
        self._closed = False
        self._lock = RLock()

    def _members(self, table: dict[int, "ProcessRecord"]) -> set[int]:
        """Descendants of this process, without the ended children it does not collect."""
        own_session = os.getsid(0)
        return {
            pid for pid in descendants(self.root_pid, table)
            if not self._uncollected(pid, table[pid], table, own_session)
        }

    def _uncollected(
        self, pid: int, record: "ProcessRecord", table: dict[int, "ProcessRecord"], own_session: int
    ) -> bool:
        """An ended child of this process that this cell may not collect.

        A child this process forked is in this process's session, or leads a
        session of its own; it cannot be in a session it does not lead. So a
        child in another session that it does not lead was adopted from a
        root, and a child that leads its own session is taken as adopted only
        when it was seen in a root's lineage. The rest belong to the code that
        started them, which collects them itself; an ended one runs nothing,
        so it is not a member.
        """
        if record.ppid != self.root_pid or record.state != "Z":
            return False
        if record.session == own_session:
            return True
        return record.session == pid and process_identity(pid, table) not in self._lineage

    def spawn(
        self, command: Sequence[str], *, cwd: str, options: Mapping[str, object]
    ) -> Any:
        """Start a root in its own session; fork inheritance makes it a member before exec.

        The root is recorded under the lock, before any reaping here can collect
        it, so its pidfd and identity name the process ``Popen`` started.
        """
        launch = dict(options)
        launch.update(cwd=cwd, start_new_session=True)
        with self._lock:
            if self._closed:
                raise RuntimeError("subsession-process-cell-closed")
            process = subprocess.Popen(command, **launch)
            pidfd = None
            try:
                pidfd = _pidfd_open(process.pid)
                identity = process_identity(process.pid)
                if identity is None:
                    raise RuntimeError("subsession-root-identity-unavailable")
            except BaseException:
                if pidfd is not None:
                    _send(pidfd, signal.SIGKILL)
                    os.close(pidfd)
                else:
                    process.kill()
                process.wait()
                raise
            self._close_started(self._started.pop(process.pid, None))
            self._started[process.pid] = _StartedRoot(identity, pidfd)
            return process

    def confirm(self, pid: int) -> bool:
        with self._lock:
            try:
                table = process_table()
            except ProcessObservationUncertain:
                return False
            if pid not in descendants(self.root_pid, table):
                return False
            identity = process_identity(pid, table)
            if identity is None:
                return False
            self._roots[pid] = identity
            self._observe_lineage(table)
            return True

    def process_identity(self, pid: int) -> str | None:
        return process_identity(pid)

    def process_exists(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def release_certain(
        self, pid: int | None, identity: str | None
    ) -> bool:
        """Prove no cell member can belong to a completed provider root."""
        with self._lock:
            try:
                self._reap_adopted_zombies()
                table = process_table()
            except ProcessObservationUncertain:
                return False
            if pid is not None and identity is not None:
                if process_identity(pid, table) == identity:
                    return False
            if pid is not None:
                self._roots.pop(pid, None)
            self._discard_stale_roots(table)
            self._prune_started()
            members = self._members(table)
            owned: set[int] = set()
            for root_pid, root_identity in self._roots.items():
                if process_identity(root_pid, table) != root_identity:
                    continue
                owned.add(root_pid)
                owned.update(descendants(root_pid, table))
            return not (members - owned)

    def _discard_stale_roots(self, table: dict[int, "ProcessRecord"]) -> None:
        for pid, identity in tuple(self._roots.items()):
            if process_identity(pid, table) != identity:
                self._roots.pop(pid, None)

    def _reap_adopted_zombies(self) -> None:
        """Collect the ended processes this process adopted from the roots it confirmed or started.

        Other code in this process starts its own children (``git`` and the
        like) and collects them itself; collecting one here would leave its
        owner with ECHILD, which ``subprocess`` reports as exit status 0. An
        orphan never seen in a root's lineage that leads its own session, such
        as a daemon that left the root's session before any scan, cannot be
        told from such a child, so it is not collected (see ``_uncollected``).
        """
        table = process_table()
        self._observe_lineage(table)
        own_session = os.getsid(0)
        for pid, record in table.items():
            if (
                record.ppid != self.root_pid
                or record.state != "Z"
                or pid in self._roots
                or pid in self._started
                or self._uncollected(pid, record, table, own_session)
            ):
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _observe_lineage(self, table: dict[int, "ProcessRecord"]) -> None:
        """Record the processes descending from a started root or in its session.

        A root that is still unreaped holds its PID, so a process in the
        session with that number is in the session the root leads. An identity
        holds the process's start time, so one that left the table never
        names another process and is forgotten.
        """
        recorded = {pid: root.identity for pid, root in self._started.items()}
        recorded.update(self._roots)
        unreaped = {
            pid for pid, identity in recorded.items()
            if process_identity(pid, table) == identity
        }
        kept = {
            int(identity.rsplit(":", 2)[1]) for identity in self._lineage
            if process_identity(int(identity.rsplit(":", 2)[1]), table) == identity
        }
        sources = unreaped | kept | {
            pid for pid, record in table.items() if record.session in unreaped
        }
        members = set(sources - unreaped)
        for source in sources:
            members |= descendants(source, table)
        self._lineage = {
            identity for identity in (process_identity(pid, table) for pid in members)
            if identity is not None
        }

    @property
    def empty(self) -> bool:
        with self._lock:
            try:
                self._reap_adopted_zombies()
                table = process_table()
            except ProcessObservationUncertain:
                return False
            self._discard_stale_roots(table)
            self._prune_started()
            return not self._members(table)

    def terminate_roots(self, roots: Iterable[tuple[int, str | None]]) -> None:
        """Kill each named root that ``spawn`` started under that identity, with its process group.

        A root named with a missing identity, or with one other than the
        identity recorded when it started, is not signalled.
        """
        with self._lock:
            for pid, identity in roots:
                root = self._started.get(pid)
                if root is not None and identity is not None and root.identity == identity:
                    self._kill_root(pid, root)

    def terminate_root(self, pid: int) -> None:
        with self._lock:
            root = self._started.get(pid)
            if root is None:
                raise RuntimeError("subsession-owned-root-unavailable")
            self._kill_root(pid, root)

    def _kill_root(self, pid: int, root: _StartedRoot) -> None:
        """Kill the root's process group through pidfds only.

        With the kernel's group flag, one signal through the root's pidfd ends
        the group. Without it, each member of the group is signalled through
        its own pidfd, opened before its group is read; the group is the one
        this cell started only while the root is unreaped, so the root's pidfd
        is checked after the member's group is read, and the root is signalled
        last. A member whose group was read after the root was reaped is not
        signalled; ``terminate_all`` still ends it as a cell member.
        """
        try:
            if self._group_signal:
                try:
                    _send(root.pidfd, signal.SIGKILL, PIDFD_SIGNAL_PROCESS_GROUP)
                    return
                except OSError as exc:
                    if exc.errno != errno.EINVAL:
                        raise
                    self._group_signal = False
            self._kill_group_members(pid, root)
            _send(root.pidfd, signal.SIGKILL)
        except PermissionError:
            pass  # a member it may not signal stays alive, so its release is not certain

    def _kill_group_members(self, group: int, root: _StartedRoot) -> None:
        """Signal every other member of the root's group, rescanning until none is left."""
        signalled: set[str] = set()
        for _ in range(GROUP_SCAN_LIMIT):
            try:
                table = process_table()
            except ProcessObservationUncertain:
                return
            found = False
            for member, record in table.items():
                if member == group or record.pgrp != group:
                    continue
                opened = _open_in_group(member, group)
                if opened is None:
                    continue
                identity, pidfd = opened
                try:
                    if identity in signalled:
                        continue
                    # The root unreaped now means it was unreaped when the
                    # member's group was read, so that group was this cell's.
                    if not _send(root.pidfd, 0):
                        return
                    _send(pidfd, signal.SIGKILL)
                    signalled.add(identity)
                    found = True
                finally:
                    os.close(pidfd)
            if not found:
                return

    def _prune_started(self) -> None:
        """Forget the roots that nothing can be signalled through any more."""
        for pid, root in tuple(self._started.items()):
            try:
                alive = self._signalable(root)
            except OSError:
                continue
            if not alive:
                self._close_started(self._started.pop(pid))

    def _signalable(self, root: _StartedRoot) -> bool:
        """Whether the root, or with the kernel's group flag any member of its group, still exists."""
        if self._group_signal:
            try:
                return _send(root.pidfd, 0, PIDFD_SIGNAL_PROCESS_GROUP)
            except OSError as exc:
                if exc.errno != errno.EINVAL:
                    raise
                self._group_signal = False
        return _send(root.pidfd, 0)

    @staticmethod
    def _close_started(root: _StartedRoot | None) -> None:
        if root is not None:
            os.close(root.pidfd)

    def terminate_all(self) -> None:
        with self._lock:
            table = process_table()
            members = self._members(table)
            for pid in sorted(
                members, key=lambda item: process_depth(item, table), reverse=True
            ):
                pidfd = _open_verified(pid, process_identity(pid, table))
                if pidfd is None:
                    continue
                try:
                    _send(pidfd, signal.SIGKILL)
                except PermissionError as exc:
                    raise RuntimeError("subsession-process-cell-kill-failed") from exc
                finally:
                    os.close(pidfd)

    def resolve_unconfirmed_empty(self, timeout: float) -> bool:
        """A failed ``Popen`` has already reaped any child it forked, so no root is left unconfirmed."""
        return True

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while not self.empty:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._prune_started()


def _syscall(number: int, *arguments: int) -> int:
    """Make a kernel call; a failed call raises the OSError of its errno."""
    result = _libc().syscall(ctypes.c_long(number), *(ctypes.c_long(argument) for argument in arguments))
    if result == -1:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return result


def _pidfd_open(pid: int) -> int:
    """A pidfd naming the process ``pid`` names now, closed on exec."""
    return _syscall(SYS_PIDFD_OPEN, pid, 0)


def _pidfd_available() -> bool:
    """Whether the kernel opens a pidfd of this process and signals through it."""
    if platform.machine().startswith(OFFSET_CALL_TABLES):
        return False
    try:
        pidfd = _pidfd_open(os.getpid())
    except OSError:
        return False
    try:
        return _send(pidfd, 0)
    except OSError:
        return False
    finally:
        os.close(pidfd)


def _send(pidfd: int, signal_number: int, flags: int = 0) -> bool:
    """Signal the process (or, with the group flag, the group) a pidfd names; False when it has none."""
    try:
        _syscall(SYS_PIDFD_SEND_SIGNAL, pidfd, signal_number, 0, flags)
    except ProcessLookupError:
        return False
    return True


def _open_in_group(pid: int, group: int) -> tuple[str, int] | None:
    """The identity and a pidfd of ``pid`` when the process the pidfd names is in ``group``.

    The pidfd is opened before the process's record is read, so the identity and
    the group read belong to the process the pidfd names.
    """
    try:
        pidfd = _pidfd_open(pid)
    except ProcessLookupError:
        return None
    try:
        record = _stat_record(Path(f"/proc/{pid}/stat").read_text(encoding="utf-8"))
    except (OSError, ValueError, IndexError):
        record = None
    identity = None if record is None else process_identity(pid, {pid: record})
    if record is None or record.pgrp != group or identity is None:
        os.close(pidfd)
        return None
    return identity, pidfd


def _open_verified(pid: int, identity: str | None) -> int | None:
    """A pidfd for ``pid`` when the process it names has ``identity``; None otherwise.

    The pidfd is opened before the identity is read, so a match proves the pidfd
    names the recorded process: a process that took the PID later started later.
    """
    if identity is None:
        return None
    try:
        pidfd = _pidfd_open(pid)
    except ProcessLookupError:
        return None
    if process_identity(pid) != identity:
        os.close(pidfd)
        return None
    return pidfd


class ProcessRecord:
    __slots__ = ("ppid", "state", "pgrp", "session", "start_time")

    def __init__(
        self, ppid: int, state: str, start_time: str, *, pgrp: int = 0, session: int = 0
    ) -> None:
        self.ppid = ppid
        self.state = state
        self.pgrp = pgrp
        self.session = session
        self.start_time = start_time


def _stat_record(value: str) -> ProcessRecord:
    close = value.rfind(")")
    if close < 0:
        raise ValueError("process-stat-command-unclosed")
    fields = value[close + 2 :].split()
    if len(fields) < 20:
        raise ValueError("process-stat-fields-missing")
    return ProcessRecord(
        int(fields[1]), fields[0], fields[19], pgrp=int(fields[2]), session=int(fields[3])
    )


def process_table() -> dict[int, ProcessRecord]:
    table: dict[int, ProcessRecord] = {}
    try:
        entries = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise ProcessObservationUncertain(
            "subsession-proc-observation-uncertain"
        ) from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            table[int(entry.name)] = _stat_record(
                (entry / "stat").read_text(encoding="utf-8")
            )
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ESRCH}:
                # The numeric /proc entry disappeared between enumeration and
                # stat read, which proves that exact process no longer exists.
                continue
            raise ProcessObservationUncertain(
                f"subsession-proc-observation-uncertain:{entry.name}"
            ) from exc
        except (ValueError, IndexError) as exc:
            raise ProcessObservationUncertain(
                f"subsession-proc-observation-uncertain:{entry.name}"
            ) from exc
    return table


def descendants(root_pid: int, table: dict[int, ProcessRecord]) -> set[int]:
    result: set[int] = set()
    frontier = [root_pid]
    while frontier:
        parent = frontier.pop()
        children = [
            pid for pid, record in table.items()
            if record.ppid == parent and pid not in result
        ]
        result.update(children)
        frontier.extend(children)
    result.discard(root_pid)
    return result


def process_identity(
    pid: int, table: dict[int, ProcessRecord] | None = None
) -> str | None:
    """Boot, PID and kernel start time of the process; None when it cannot be observed."""
    if table is None:
        try:
            record: ProcessRecord | None = _stat_record(
                Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            )
        except (OSError, ValueError, IndexError):
            return None
    else:
        record = table.get(pid)
    if record is None:
        return None
    try:
        boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        return None
    return f"{boot_id}:{pid}:{record.start_time}"


def process_depth(pid: int, table: dict[int, ProcessRecord]) -> int:
    depth = 0
    seen: set[int] = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        pid = table[pid].ppid
        depth += 1
    return depth
