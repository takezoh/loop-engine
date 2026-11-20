"""Every process cell keeps the cell port contract; the Linux cell's own ancestry rules."""
import os
import errno
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from provider_transport.cwd_authority import (
    AdmissionDenied,
    ChildLedger,
    SpawnAdmission,
)
from provider_transport.directory_authority import create_cwd_authority
from provider_transport.linux_process import (
    ProcessObservationUncertain,
    ProcSubreaperCell,
    process_identity,
    process_table,
)
from provider_transport.windows_process import WindowsJobCell
from provider_transport.windows_process import process_exists as windows_process_exists
from provider_transport.testing.cell_fake import FakeCellMembership
from .platform_selection import runs_on
from .windows_api_fake import FakeProcessApi, any_program


def ended_without_being_collected(process, timeout=5):
    """Wait until a child of this process has ended, leaving its exit status for its owner to collect.

    POSIX waits for the exit without reaping it. Windows keeps an exit status while a handle to the
    process is open, which its Popen holds, so there it waits until the process is gone."""
    if hasattr(os, "waitid"):
        os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOWAIT)
        return
    deadline = time.monotonic() + timeout
    while windows_process_exists(process.pid) and time.monotonic() < deadline:
        time.sleep(0.01)


class CellMembershipContract:
    """What `SpawnAdmission`, `ChildLedger` and the provider clients rely on from every process cell."""

    def make_cell(self):
        raise NotImplementedError

    def running(self):
        """A command whose process runs until it is ended."""
        raise NotImplementedError

    def end(self, cell, process):
        """Make a running root end by itself, and reap it."""
        raise NotImplementedError

    def reap(self, process):
        """Collect a root the cell ended."""
        raise NotImplementedError

    def fail_next_start(self):
        """Make the next start raise; returns the command to start."""
        raise NotImplementedError

    def started_in(self, process):
        """The working directory the process was started in."""
        raise NotImplementedError

    def working_directory(self):
        raise NotImplementedError

    def still_running(self, cell, process):
        """Whether a root no one ended is still running."""
        return cell.process_exists(process.pid)

    def open_host(self):
        """The implementation under contract, for the port registry."""
        return self.make_cell()

    def close_host(self, cell):
        cell.close()

    def cell(self):
        cell = self.make_cell()
        self.addCleanup(self.close_host, cell)
        return cell

    def start(self, cell, command=None, options=None):
        process = cell.spawn(command or self.running(), cwd=self.working_directory(), options=options or {})
        self.addCleanup(self._discard, cell, process)
        return process

    def _discard(self, cell, process):
        if cell.process_exists(process.pid):
            cell.terminate_root(process.pid)
            self.reap(process)

    def test_a_started_root_is_confirmed_with_a_stable_identity_in_its_directory(self):
        cell = self.cell()
        process = self.start(cell)
        self.assertTrue(cell.confirm(process.pid))
        identity = cell.process_identity(process.pid)
        self.assertIsInstance(identity, str)
        self.assertEqual(identity, cell.process_identity(process.pid))
        self.assertTrue(cell.process_exists(process.pid))
        self.assertEqual(self.working_directory(), self.started_in(process))

    def test_release_is_certain_only_once_the_root_has_ended(self):
        cell = self.cell()
        process = self.start(cell)
        self.assertTrue(cell.confirm(process.pid))
        identity = cell.process_identity(process.pid)
        self.assertFalse(cell.release_certain(process.pid, identity))
        self.end(cell, process)
        self.assertTrue(cell.wait_empty(2.0))
        self.assertFalse(cell.process_exists(process.pid))
        self.assertTrue(cell.release_certain(process.pid, identity))

    def test_terminating_named_roots_ends_those_roots_only(self):
        cell = self.cell()
        named, other = self.start(cell), self.start(cell)
        self.assertTrue(cell.confirm(named.pid) and cell.confirm(other.pid))
        cell.terminate_roots(((named.pid, cell.process_identity(named.pid)),))
        self.reap(named)
        self.assertFalse(cell.process_exists(named.pid))
        self.assertTrue(cell.process_exists(other.pid))

    def test_a_root_named_with_an_identity_it_does_not_have_is_not_terminated(self):
        cell = self.cell()
        process = self.start(cell)
        self.assertTrue(cell.confirm(process.pid))
        identity = cell.process_identity(process.pid)
        cell.terminate_roots(((process.pid, f"other-{identity}"), (process.pid, None)))
        self.assertTrue(self.still_running(cell, process))
        self.assertEqual(identity, cell.process_identity(process.pid))

    def test_terminating_one_root_ends_it(self):
        cell = self.cell()
        process = self.start(cell)
        cell.terminate_root(process.pid)
        self.reap(process)
        self.assertFalse(cell.process_exists(process.pid))

    def test_terminating_all_empties_the_cell(self):
        cell = self.cell()
        first, second = self.start(cell), self.start(cell)
        cell.terminate_all()
        self.reap(first)
        self.reap(second)
        self.assertTrue(cell.wait_empty(2.0))

    def test_a_failed_start_leaves_nothing_unconfirmed(self):
        cell = self.cell()
        command = self.fail_next_start()
        with self.assertRaises(Exception):
            cell.spawn(command, cwd=self.working_directory(), options={})
        self.assertTrue(cell.resolve_unconfirmed_empty(1.0))
        self.assertTrue(cell.wait_empty(1.0))

    def test_a_process_the_cell_did_not_start_is_not_confirmed(self):
        cell = self.cell()
        self.assertFalse(cell.confirm(os.getpid()))

    def test_a_child_the_cell_did_not_start_keeps_its_exit_status_for_its_owner(self):
        """Other code in the process (git, checks) starts and collects its own children."""
        cell = self.cell()
        process = self.start(cell)
        other = subprocess.Popen((sys.executable, "-c", "raise SystemExit(7)"))
        self.addCleanup(other.wait)
        ended_without_being_collected(other)
        cell.release_certain(process.pid, cell.process_identity(process.pid))
        cell.wait_empty(0.1)
        self.assertEqual(7, other.wait(timeout=5))

    def test_a_closed_cell_refuses_to_start(self):
        cell = self.cell()
        cell.close()
        with self.assertRaises(RuntimeError):
            cell.spawn(self.running(), cwd=self.working_directory(), options={})


class RealProcessCellContract(CellMembershipContract):
    """The contract fixtures for a cell that starts processes on this machine."""

    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = str(Path(directory.name).resolve())

    def running(self):
        return (sys.executable, "-c", "import os,sys,time; print(os.getcwd(), flush=True); time.sleep(60)")

    def start(self, cell, command=None, options=None):
        return super().start(cell, command, options or {"stdout": subprocess.PIPE, "text": True})

    def end(self, cell, process):
        process.kill()
        self.reap(process)

    def still_running(self, cell, process):
        time.sleep(0.2)  # a signalled process is a zombie until reaped; give the signal time to land
        return process.poll() is None

    def reap(self, process):
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

    def fail_next_start(self):
        return (str(Path(self.directory) / "no-such-program"),)

    def started_in(self, process):
        return process.stdout.readline().strip()

    def working_directory(self):
        return self.directory


@runs_on("posix")
class LinuxCellContractTests(RealProcessCellContract, unittest.TestCase):
    def make_cell(self):
        return ProcSubreaperCell()


class FakeCellContractTests(RealProcessCellContract, unittest.TestCase):
    def make_cell(self):
        return FakeCellMembership()


class WindowsJobCellContractTests(CellMembershipContract, unittest.TestCase):
    """The Windows cell against the fake Win32 process API: a root runs until its Job Object empties."""

    def setUp(self):
        self.api = FakeProcessApi()

    def make_cell(self):
        return WindowsJobCell(api=self.api, runnable=any_program)

    def running(self):
        return ("provider",)

    def end(self, cell, process):
        self.api.active[process.job] = 0

    def reap(self, process):
        pass

    def fail_next_start(self):
        self.api.fail_launch = True
        return ("provider",)

    def started_in(self, process):
        launches = [row for row in self.api.events if row[0] == "create-suspended-in-job"]
        return next(row[3]["cwd"] for row in launches if row[1] == process.job)

    def working_directory(self):
        return "C:\\work"


@runs_on("posix")
class LinuxCellTests(unittest.TestCase):
    """The Linux cell's ancestry rules beyond the port contract."""

    def bystander(self):
        """A process outside the cell that leads its own process group, as a provider root does."""
        process = subprocess.Popen(
            (sys.executable, "-c", "import time; time.sleep(60)"), start_new_session=True
        )

        def end():
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
        self.addCleanup(end)
        return process

    def test_a_reused_pid_is_not_signalled_through_the_record_of_the_root_that_had_it(self):
        """A root ends and its PID now names an unrelated group leader: no path signals it."""
        for group_signal in (True, False):
            with self.subTest(group_signal=group_signal):
                with TemporaryDirectory() as directory:
                    cell = ProcSubreaperCell()
                    self.addCleanup(cell.close)
                    cell._group_signal = group_signal
                    root = cell.spawn(
                        (sys.executable, "-c", "import time; time.sleep(60)"),
                        cwd=directory, options={},
                    )
                    self.assertTrue(cell.confirm(root.pid))
                    boot, _pid, started = cell.process_identity(root.pid).split(":")
                    root.kill()
                    root.wait(timeout=5)
                    bystander = self.bystander()
                    # The kernel gave the ended root's PID to the bystander: the
                    # cell's record of the root now sits under the bystander's PID.
                    record = cell._started.pop(root.pid)
                    record.identity = f"{boot}:{bystander.pid}:{started}"
                    cell._started[bystander.pid] = record
                    cell.terminate_roots(((bystander.pid, record.identity),))
                    cell.terminate_root(bystander.pid)
                    cell.terminate_roots(((bystander.pid, None),))
                    time.sleep(0.1)
                    self.assertIsNone(bystander.poll())
                    self.assertEqual(os.getpgid(bystander.pid), bystander.pid)

    def test_a_live_process_under_a_recorded_identity_it_does_not_have_is_not_signalled(self):
        """The ledger names a PID whose live process is not the one it recorded."""
        cell = ProcSubreaperCell()
        self.addCleanup(cell.close)
        bystander = self.bystander()
        stale = f"{process_identity(bystander.pid).rsplit(':', 1)[0]}:0"
        cell.terminate_roots(((bystander.pid, stale), (bystander.pid, None)))
        cell.terminate_roots(((bystander.pid, process_identity(bystander.pid)),))
        with self.assertRaises(RuntimeError):
            cell.terminate_root(bystander.pid)
        time.sleep(0.1)
        self.assertIsNone(bystander.poll())

    def test_terminating_a_root_ends_its_process_group(self):
        for group_signal in (True, False):
            with self.subTest(group_signal=group_signal):
                with TemporaryDirectory() as directory:
                    child_path = Path(directory) / "child.pid"
                    cell = ProcSubreaperCell()
                    self.addCleanup(cell.close)
                    cell._group_signal = group_signal
                    root = cell.spawn(
                        (
                            sys.executable, "-c",
                            "import pathlib,subprocess,sys,time;"
                            "p=subprocess.Popen((sys.executable,'-c','import time;time.sleep(60)'));"
                            f"pathlib.Path({str(child_path)!r}).write_text(str(p.pid));"
                            "time.sleep(60)",
                        ),
                        cwd=directory, options={},
                    )
                    deadline = time.monotonic() + 5
                    while not child_path.exists() and time.monotonic() < deadline:
                        time.sleep(0.01)
                    child = int(child_path.read_text())
                    self.assertTrue(cell.confirm(root.pid))
                    cell.terminate_roots(((root.pid, cell.process_identity(root.pid)),))
                    root.wait(timeout=5)
                    self.assertTrue(cell.wait_empty(5.0))
                    self.assertIsNone(process_identity(child))

    def group_root(self, cell, directory):
        """A root started by the cell with a member of its group; returns the root and the member's PID."""
        child_path = Path(directory) / "child.pid"
        root = cell.spawn(
            (
                sys.executable, "-c",
                "import pathlib,subprocess,sys,time;"
                "p=subprocess.Popen((sys.executable,'-c','import time;time.sleep(60)'));"
                f"pathlib.Path({str(child_path)!r}).write_text(str(p.pid));"
                "time.sleep(60)",
            ),
            cwd=directory, options={},
        )
        deadline = time.monotonic() + 5
        while not child_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return root, int(child_path.read_text())

    def test_without_the_group_flag_no_group_is_signalled_by_its_number(self):
        """The group number may name another group once the root is reaped; members are signalled by pidfd."""
        with TemporaryDirectory() as directory:
            cell = ProcSubreaperCell()
            self.addCleanup(cell.close)
            cell._group_signal = False
            root, child = self.group_root(cell, directory)
            with patch("os.killpg", side_effect=AssertionError("signalled a group by its number")):
                cell.terminate_root(root.pid)
            root.wait(timeout=5)
            self.assertTrue(cell.wait_empty(5.0))
            self.assertIsNone(process_identity(child))

    def test_without_the_group_flag_a_reaped_root_lets_no_member_be_signalled_through_it(self):
        """Once someone else reaps the root, its group number proves nothing; terminate_all still ends the member."""
        with TemporaryDirectory() as directory:
            cell = ProcSubreaperCell()
            self.addCleanup(cell.close)
            cell._group_signal = False
            root, child = self.group_root(cell, directory)
            identity = process_identity(child)
            os.kill(root.pid, 9)
            root.wait(timeout=5)  # the owner of the root reaps it, not the cell
            with patch("os.killpg", side_effect=AssertionError("signalled a group by its number")):
                cell.terminate_roots(((root.pid, cell._started[root.pid].identity),))
            time.sleep(0.1)
            self.assertEqual(identity, process_identity(child))
            cell.terminate_all()
            self.assertTrue(cell.wait_empty(5.0))
            self.assertIsNone(process_identity(child))

    def test_current_environment_supports_proc_subreaper_cell(self):
        cell = ProcSubreaperCell()
        try:
            self.assertTrue(cell.empty)
        finally:
            cell.close()

    def test_proc_stat_uncertainty_never_becomes_empty_or_releasable(self):
        cell = ProcSubreaperCell()
        target = Path(f"/proc/{os.getpid()}/stat")
        original_read_text = Path.read_text
        cases = (
            OSError("stat-denied"),
            ValueError("stat-decode-failed"),
            "malformed-stat",
        )
        try:
            for failure in cases:
                with self.subTest(failure=repr(failure)):
                    def read_text(path, *args, **kwargs):
                        if path == target:
                            if isinstance(failure, BaseException):
                                raise failure
                            return failure
                        return original_read_text(path, *args, **kwargs)

                    with patch.object(Path, "read_text", read_text):
                        with self.assertRaises(ProcessObservationUncertain):
                            process_table()
                        self.assertFalse(cell.release_certain(None, None))
                        self.assertFalse(cell.empty)
        finally:
            cell.close()

    def test_proc_stat_disappearance_race_is_not_observation_uncertainty(self):
        target = Path(f"/proc/{os.getpid()}/stat")
        original_read_text = Path.read_text
        for error_number in (errno.ENOENT, errno.ESRCH):
            with self.subTest(error_number=error_number):
                def read_text(path, *args, **kwargs):
                    if path == target:
                        raise OSError(error_number, "process-disappeared")
                    return original_read_text(path, *args, **kwargs)

                with patch.object(Path, "read_text", read_text):
                    self.assertNotIn(os.getpid(), process_table())

    def test_setsid_descendant_remains_member_after_provider_root_exits(self):
        with TemporaryDirectory() as temporary:
            grandchild_path = Path(temporary) / "grandchild.pid"
            cell = ProcSubreaperCell()
            root = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,subprocess,sys,time;"
                        "p=subprocess.Popen((sys.executable,'-c','import time;time.sleep(60)'),start_new_session=True);"
                        f"pathlib.Path({str(grandchild_path)!r}).write_text(str(p.pid));"
                        "time.sleep(60)"
                    ),
                ),
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 2
                while not grandchild_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(grandchild_path.exists())
                identity = process_identity(root.pid)
                self.assertIsNotNone(identity)
                self.assertTrue(cell.confirm(root.pid))
                root.terminate()
                root.wait(timeout=2)
                self.assertFalse(cell.release_certain(root.pid, identity))
                self.assertFalse(cell.empty)
                cell.terminate_all()
                self.assertTrue(cell.wait_empty(2))
            finally:
                if root.poll() is None:
                    root.kill()
                    root.wait(timeout=2)
                if not cell.empty:
                    cell.terminate_all()
                    cell.wait_empty(2)
                cell.close()

    def test_root_exit_before_mark_live_quarantines_until_descendant_reaped(self):
        with TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            grandchild_path = root_path / "grandchild.pid"
            cell = ProcSubreaperCell()
            authority = create_cwd_authority(root_path)
            ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
            admission = SpawnAdmission(
                reservation=ledger.reserve(), cwd_lease=authority.admit("."),
                cell_id="cell", epoch=0, fence_check=lambda _: True,
                cell_membership=cell,
            )
            admission.consume(expected_cell_id="cell")
            provider = subprocess.Popen(
                (
                    sys.executable,
                    "-c",
                    (
                        "import pathlib,subprocess,sys,time;"
                        "p=subprocess.Popen((sys.executable,'-c','import time;time.sleep(0.5)'),start_new_session=True);"
                        f"pathlib.Path({str(grandchild_path)!r}).write_text(str(p.pid));"
                    ),
                ),
                start_new_session=True,
            )
            try:
                provider.wait(timeout=2)
                deadline = time.monotonic() + 2
                while not grandchild_path.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(grandchild_path.exists())
                with self.assertRaisesRegex(AdmissionDenied, "not-in-host-cell"):
                    admission.spawned(provider.pid)
                admission.abort()
                self.assertEqual(1, ledger.used)
                self.assertEqual(0, ledger.reconcile())
                with self.assertRaisesRegex(
                    AdmissionDenied, "provider-capacity-exhausted"
                ):
                    ledger.reserve()
                deadline = time.monotonic() + 2
                while ledger.used and time.monotonic() < deadline:
                    ledger.reconcile()
                    time.sleep(0.01)
                self.assertEqual(0, ledger.used)
                replacement = ledger.reserve()
                replacement.release(certain=True)
            finally:
                if provider.poll() is None:
                    provider.kill()
                    provider.wait(timeout=2)
                if not cell.empty:
                    cell.terminate_all()
                    cell.wait_empty(2)
                authority.close()
                cell.close()



if __name__ == "__main__":
    unittest.main()
