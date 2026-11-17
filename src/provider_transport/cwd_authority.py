"""One-shot provider spawn admission and the cell-local ledger of the children it admits.

A provider process starts only by consuming a ``SpawnAdmission``: a ledger
reservation, a working-directory lease and the process cell. Which processes
the ledger owns, what a failed start leaves to clean up and whether a release
is certain are decided by the pure functions here from facts the cell reports.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from threading import Condition, RLock
import time
from typing import Any, Callable, Iterable, Mapping, Sequence
from uuid import uuid4

from .cell_membership import CellMembership
from .directory_authority import CwdLease


class AdmissionDenied(RuntimeError):
    pass


RESERVED = "reserved"
LIVE = "live"
QUARANTINED = "quarantined"


@dataclass(frozen=True)
class LedgerEntry:
    state: str
    pid: int | None = None
    identity: str | None = None


def owned_roots(entries: Iterable[LedgerEntry]) -> tuple[tuple[int, str | None], ...]:
    """The roots a ledger owns: every live or quarantined entry with a recorded process."""
    return tuple(
        (entry.pid, entry.identity) for entry in entries
        if entry.state in {LIVE, QUARANTINED} and entry.pid is not None
    )


def process_gone(recorded: str | None, observed: str | None, exists: bool) -> bool:
    """Whether the recorded process has ended: no process runs under its PID, or another one does."""
    if not exists:
        return True
    return recorded is not None and observed is not None and observed != recorded


def after_gone(entry: LedgerEntry, *, release_certain: bool) -> LedgerEntry | None:
    """The entry once its process has gone: removed when release is certain, otherwise quarantined."""
    if release_certain:
        return None
    return replace(entry, state=QUARANTINED)


def failed_start_release_certain(*, launched: bool, unconfirmed_resolved: bool) -> bool:
    """Release after a failed start is certain only when it launched no root and left none unconfirmed."""
    return not launched and unconfirmed_resolved


@dataclass(frozen=True)
class SpawnReceipt:
    reservation_id: str
    pid: int
    process_start_identity: str


class ChildReservation:
    def __init__(self, ledger: "ChildLedger", reservation_id: str) -> None:
        self._ledger = ledger
        self.reservation_id = reservation_id
        self._finished = False

    def mark_live(self, pid: int) -> SpawnReceipt:
        if self._finished:
            raise AdmissionDenied("child-reservation-consumed")
        try:
            receipt = self._ledger._mark_live(self.reservation_id, pid)
        except AdmissionDenied:
            self._finished = True
            raise
        self._finished = True
        return receipt

    def release(self, *, certain: bool) -> None:
        if self._finished:
            return
        self._ledger._release(self.reservation_id, certain=certain)
        self._finished = True

    def quarantine(self, pid: int) -> None:
        if self._finished:
            return
        self._ledger._quarantine(self.reservation_id, pid)
        self._finished = True

    def release_live(self, receipt: SpawnReceipt, *, certain: bool) -> None:
        self._ledger.release_live(receipt, certain=certain)


class ChildLedger:
    """Cell-local bounded child owner; quarantine continues to consume capacity."""

    def __init__(
        self,
        *,
        cell_id: str,
        cell: CellMembership,
        max_children: int = 4,
    ) -> None:
        if max_children <= 0:
            raise ValueError("max-children-must-be-positive")
        self.cell_id = cell_id
        self.max_children = max_children
        self._cell = cell
        self._entries: dict[str, LedgerEntry] = {}
        self._lock = RLock()
        self._condition = Condition(self._lock)

    def reserve(self) -> ChildReservation:
        with self._lock:
            self._reconcile_locked()
            if len(self._entries) >= self.max_children:
                raise AdmissionDenied("provider-capacity-exhausted")
            reservation_id = uuid4().hex
            self._entries[reservation_id] = LedgerEntry(RESERVED)
            return ChildReservation(self, reservation_id)

    def reconcile(self) -> int:
        """Release only entries whose recorded process is proven absent."""
        with self._lock:
            return self._reconcile_locked()

    def _reconcile_locked(self) -> int:
        removed = 0
        for reservation_id, entry in tuple(self._entries.items()):
            if entry.state not in {LIVE, QUARANTINED} or entry.pid is None:
                continue
            if not process_gone(
                entry.identity,
                self._cell.process_identity(entry.pid),
                self._cell.process_exists(entry.pid),
            ):
                continue
            after = after_gone(
                entry, release_certain=self._cell.release_certain(entry.pid, entry.identity)
            )
            if after is None:
                del self._entries[reservation_id]
                removed += 1
            else:
                self._entries[reservation_id] = after
        if removed:
            self._condition.notify_all()
        return removed

    def _mark_live(self, reservation_id: str, pid: int) -> SpawnReceipt:
        with self._lock:
            entry = self._entries.get(reservation_id)
            if entry is None or entry.state != RESERVED:
                raise AdmissionDenied("child-reservation-not-current")
            identity = self._cell.process_identity(pid)
            if identity is None:
                self._entries[reservation_id] = LedgerEntry(QUARANTINED, pid, None)
                raise AdmissionDenied("child-process-identity-unavailable")
            self._entries[reservation_id] = LedgerEntry(LIVE, pid, identity)
            return SpawnReceipt(reservation_id, pid, identity)

    def _quarantine(self, reservation_id: str, pid: int) -> None:
        with self._lock:
            entry = self._entries.get(reservation_id)
            if entry is None or entry.state != RESERVED:
                raise AdmissionDenied("child-reservation-not-current")
            self._entries[reservation_id] = LedgerEntry(
                QUARANTINED, pid, self._cell.process_identity(pid)
            )

    def _release(self, reservation_id: str, *, certain: bool) -> None:
        with self._lock:
            entry = self._entries.get(reservation_id)
            if entry is None:
                return
            if certain:
                del self._entries[reservation_id]
                self._condition.notify_all()
            else:
                self._entries[reservation_id] = replace(entry, state=QUARANTINED)

    def release_live(self, receipt: SpawnReceipt, *, certain: bool) -> None:
        with self._lock:
            entry = self._entries.get(receipt.reservation_id)
            if entry != LedgerEntry(LIVE, receipt.pid, receipt.process_start_identity):
                raise AdmissionDenied("child-identity-conflict")
            if certain and self._cell.release_certain(
                receipt.pid, receipt.process_start_identity
            ):
                del self._entries[receipt.reservation_id]
                self._condition.notify_all()
            else:
                self._entries[receipt.reservation_id] = replace(entry, state=QUARANTINED)

    @property
    def used(self) -> int:
        with self._lock:
            return len(self._entries)

    def terminate_owned(self) -> None:
        """End this ledger's own provider roots, with their descendants.

        Only entries this ledger actually admitted are named to the cell, so an
        unrelated member of the cell is never touched. Quarantined entries are
        included because their recorded process may still be alive.
        """
        with self._lock:
            roots = owned_roots(self._entries.values())
        self._cell.terminate_roots(roots)
        self.reconcile()

    def wait_empty(self, timeout: float) -> bool:
        """Prove this lifetime cell has no reserved/live/quarantined child.

        Quarantined entries converge as soon as their recorded process and any
        adopted descendants are reaped, so the wait re-reconciles instead of
        blocking forever on a stale claim.
        """
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self._reconcile_locked()
                if not self._entries:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(min(remaining, 0.05))


class SpawnAdmission:
    """Move-only bundle required by every provider adapter before a process starts.

    An admission no start consumed returns its reservation and lease through
    ``release_unused``; used as a context manager it does so on any exit, so a
    start that fails before it launches leaves nothing held.
    """

    def __init__(
        self,
        *,
        reservation: ChildReservation,
        cwd_lease: CwdLease,
        cell_id: str,
        epoch: int,
        fence_check: Callable[[int], bool],
        cell_membership: CellMembership,
    ) -> None:
        self.reservation = reservation
        self.cwd_lease = cwd_lease
        self.cell_id = cell_id
        self.epoch = epoch
        self._fence_check = fence_check
        self._cell_membership = cell_membership
        self._consumed = False
        self._released = False
        self._lock = RLock()

    def __enter__(self) -> "SpawnAdmission":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release_unused()

    def release_unused(self) -> None:
        """Return the reservation and lease unless a start consumed the admission; it cannot be consumed after."""
        with self._lock:
            if self._consumed or self._released:
                return
            self._released = True
        self.abort()

    def consume(self, *, expected_cell_id: str) -> str:
        """The working directory to start the one admitted process in."""
        with self._lock:
            if self._released:
                raise AdmissionDenied("spawn-admission-released")
            if self._consumed:
                raise AdmissionDenied("spawn-admission-already-consumed")
            if expected_cell_id != self.cell_id:
                raise AdmissionDenied("spawn-admission-cell-mismatch")
            if not self._fence_check(self.epoch):
                raise AdmissionDenied("spawn-admission-stale-fence")
            cwd = self.cwd_lease.consume_for_spawn()
            self._consumed = True
            return cwd

    def spawn_process(
        self,
        command: Sequence[str],
        *,
        expected_cell_id: str,
        options: Mapping[str, object],
    ) -> tuple[Any, SpawnReceipt]:
        """Synchronize the fence, held cwd, cell start, and ownership receipt."""
        process = None
        try:
            cwd = self.consume(expected_cell_id=expected_cell_id)
            process = self._cell_membership.spawn(command, cwd=cwd, options=dict(options))
            receipt = self.spawned(int(process.pid))
            return process, receipt
        except BaseException:
            resolved = False
            if process is not None:
                try:
                    self._cell_membership.terminate_root(int(process.pid))
                    process.wait(timeout=2.0)
                except BaseException:
                    pass
            else:
                try:
                    resolved = bool(self._cell_membership.resolve_unconfirmed_empty(2.0))
                except BaseException:
                    resolved = False
            self.abort(certain=failed_start_release_certain(
                launched=process is not None, unconfirmed_resolved=resolved
            ))
            raise

    def spawned(self, pid: int) -> SpawnReceipt:
        try:
            if not self._cell_membership.confirm(pid):
                self.reservation.quarantine(pid)
                raise AdmissionDenied("child-not-in-host-cell")
            return self.reservation.mark_live(pid)
        finally:
            self.cwd_lease.close()

    def abort(self, *, certain: bool = True) -> None:
        self.cwd_lease.close()
        self.reservation.release(certain=certain)
