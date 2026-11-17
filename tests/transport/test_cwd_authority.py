import os
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from provider_transport.cwd_authority import (
    LIVE,
    AdmissionDenied,
    QUARANTINED,
    ChildLedger,
    LedgerEntry,
    SpawnAdmission,
    after_gone,
    failed_start_release_certain,
    owned_roots,
    process_gone,
)
from provider_transport.directory_authority import CwdDenied, create_cwd_authority
from provider_transport.posix_filesystem import PosixDirectoryAuthority
from provider_transport.windows_filesystem import WindowsDirectoryAuthority
from provider_transport.testing.cell_fake import FakeCellMembership
from provider_transport.testing.directory_fake import FakeCwdAuthority
from .windows_api_fake import FakeFilesystemApi


class CwdAuthorityContract:
    """What `SpawnAdmission` and the provider lifecycle rely on from every working-directory authority.

    Fixture: the origin holds a directory `child` and a link `link_name` to
    `outside()`, a directory beside the origin; `temporary()` is a directory
    under the temporary root; `elsewhere()` is under neither root.
    """

    link_name = "escape"

    def capture(self, *, allow_tmp=False):
        raise NotImplementedError

    def child_path(self):
        raise NotImplementedError

    def outside(self):
        raise NotImplementedError

    def temporary(self):
        raise NotImplementedError

    def elsewhere(self):
        raise NotImplementedError

    def names(self, spawn_path, path):
        """Whether a process started in `spawn_path` starts in the directory `path` names."""
        return spawn_path == path

    def open_host(self):
        """The implementation under contract, for the port registry."""
        return self.capture()

    def close_host(self, authority):
        authority.close()

    def authority(self, **options):
        authority = self.capture(**options)
        self.addCleanup(authority.close)
        return authority

    def lease(self, authority, requested):
        lease = authority.admit(requested)
        self.addCleanup(lease.close)
        return lease

    def test_the_origin_and_a_descendant_are_admitted_with_their_paths(self):
        authority = self.authority()
        origin = self.lease(authority, ".")
        child = self.lease(authority, "child")
        self.assertNotEqual(origin.identity, child.identity)
        self.assertEqual(self.child_path(), child.admitted_path())

    def test_traversal_paths_outside_the_origin_and_links_are_denied(self):
        authority = self.authority()
        for requested in ("..", "child/../..", self.outside(), self.link_name, self.temporary(), self.elsewhere(), ""):
            with self.subTest(requested=requested), self.assertRaises(CwdDenied):
                authority.admit(requested)

    def test_the_temporary_root_is_admitted_only_when_allowed(self):
        authority = self.authority(allow_tmp=True)
        self.assertEqual(self.temporary(), self.lease(authority, self.temporary()).admitted_path())
        with self.assertRaises(CwdDenied):
            authority.admit(self.elsewhere())

    def test_a_lease_gives_its_spawn_path_once(self):
        lease = self.lease(self.authority(), "child")
        path = lease.consume_for_spawn()
        self.assertIsInstance(path, str)
        self.assertTrue(self.names(path, self.child_path()))
        with self.assertRaises(CwdDenied):
            lease.consume_for_spawn()
        with self.assertRaises(CwdDenied):
            lease.admitted_path()

    def test_a_closed_lease_is_not_owned(self):
        lease = self.authority().admit("child")
        lease.close()
        with self.assertRaises(CwdDenied):
            lease.admitted_path()
        with self.assertRaises(CwdDenied):
            lease.consume_for_spawn()

    def test_a_closed_authority_admits_nothing(self):
        authority = self.capture()
        authority.close()
        with self.assertRaises(CwdDenied):
            authority.admit(".")


class PosixDirectoryAuthorityContractTests(CwdAuthorityContract, unittest.TestCase):
    """The POSIX authority over real directories."""

    def setUp(self):
        parent = TemporaryDirectory()
        self.addCleanup(parent.cleanup)
        self.parent = Path(parent.name).resolve()
        (self.parent / "origin" / "child").mkdir(parents=True)
        (self.parent / "sibling").mkdir()
        (self.parent / "origin" / self.link_name).symlink_to(self.parent / "sibling", target_is_directory=True)
        scratch = TemporaryDirectory(dir="/tmp")
        self.addCleanup(scratch.cleanup)
        self.scratch = Path(scratch.name).resolve()

    def capture(self, *, allow_tmp=False):
        return PosixDirectoryAuthority.capture(self.parent / "origin", allow_tmp=allow_tmp)

    def child_path(self):
        return str(self.parent / "origin" / "child")

    def outside(self):
        return str(self.parent / "sibling")

    def temporary(self):
        return str(self.scratch)

    def elsewhere(self):
        return "/etc"

    def names(self, spawn_path, path):
        observed, expected = os.stat(spawn_path), os.stat(path)
        return (observed.st_dev, observed.st_ino) == (expected.st_dev, expected.st_ino)


class WindowsDirectoryAuthorityContractTests(CwdAuthorityContract, unittest.TestCase):
    """The Windows authority against the fake Win32 filesystem API."""

    link_name = "junction"

    def setUp(self):
        self.api = FakeFilesystemApi()

    def capture(self, *, allow_tmp=False):
        with patch("provider_transport.windows_filesystem.tempfile.gettempdir",
                   return_value="C:\\Temp"):
            return WindowsDirectoryAuthority.capture("C:\\work", allow_tmp=allow_tmp, api=self.api)

    def child_path(self):
        return "C:\\work\\child"

    def outside(self):
        return "C:\\sibling"

    def temporary(self):
        return "C:\\Temp\\scratch"

    def elsewhere(self):
        return "D:\\elsewhere"


class FakeCwdAuthorityContractTests(CwdAuthorityContract, unittest.TestCase):
    def capture(self, *, allow_tmp=False):
        return FakeCwdAuthority(
            "/root/origin", directories=("/root/origin/child", "/root/sibling", "/tmp/scratch"),
            links=("/root/origin/escape",), temporary_root="/tmp" if allow_tmp else None,
        )

    def child_path(self):
        return "/root/origin/child"

    def outside(self):
        return "/root/sibling"

    def temporary(self):
        return "/tmp/scratch"

    def elsewhere(self):
        return "/etc"


class PosixDirectoryAuthorityTests(unittest.TestCase):
    """The POSIX authority's descriptor rules beyond the port contract."""

    def test_the_platform_authority_is_the_posix_one(self):
        with TemporaryDirectory() as temporary:
            authority = create_cwd_authority(temporary)
            self.assertIsInstance(authority, PosixDirectoryAuthority)
            authority.close()

    def test_equal_and_descendant_are_admitted_by_identity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            authority = PosixDirectoryAuthority.capture(root)
            equal = authority.admit(".")
            descendant = authority.admit("child")
            self.assertEqual(os.stat(root).st_ino, equal.identity[1])
            self.assertEqual(os.stat(child).st_ino, descendant.identity[1])
            equal.close(); descendant.close(); authority.close()

    def test_admitted_path_rechecks_identity_before_handover(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            authority = PosixDirectoryAuthority.capture(root)
            lease = authority.admit("child")
            self.assertEqual(str(child.resolve()), lease.admitted_path())
            child.rename(root / "moved")
            child.mkdir()
            # The admitted directory moved; its old path now names another one.
            self.assertEqual(str((root / "moved").resolve()), lease.admitted_path())
            (root / "moved").rmdir()
            with self.assertRaisesRegex(CwdDenied, "directory-removed"):
                lease.admitted_path()
            lease.close(); authority.close()

    def test_admitted_path_rejects_a_replaced_directory(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            child = root / "child"
            child.mkdir()
            authority = PosixDirectoryAuthority.capture(root)
            lease = authority.admit("child")
            path = lease.admitted_path()
            with patch("provider_transport.posix_filesystem.os.readlink",
                       return_value=str(root)):
                with self.assertRaisesRegex(CwdDenied, "identity-changed"):
                    lease.admitted_path()
            self.assertEqual(path, lease.admitted_path())
            lease.close(); authority.close()

    def test_ancestor_sibling_traversal_and_symlink_are_denied(self):
        with TemporaryDirectory() as temporary:
            parent = Path(temporary)
            root = parent / "root"; root.mkdir()
            sibling = parent / "sibling"; sibling.mkdir()
            (root / "escape").symlink_to(sibling, target_is_directory=True)
            authority = PosixDirectoryAuthority.capture(root)
            for requested in ("..", str(parent), str(sibling), "escape"):
                with self.subTest(requested=requested), self.assertRaises(CwdDenied):
                    authority.admit(requested)
            authority.close()

    def test_explicit_tmp_authority_admits_tmp_only_without_symlink_escape(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            origin = root / "origin"; origin.mkdir()
            sibling = root / "scratch"; sibling.mkdir()
            (sibling / "escape").symlink_to(Path("/etc"), target_is_directory=True)
            strict = PosixDirectoryAuthority.capture(origin)
            with self.assertRaisesRegex(CwdDenied, "outside-origin"):
                strict.admit(sibling)
            strict.close()

            authority = PosixDirectoryAuthority.capture(origin, allow_tmp=True)
            lease = authority.admit(sibling)
            self.assertEqual(os.stat(sibling).st_ino, lease.identity[1])
            lease.close()
            for requested in (str(sibling / "escape"), "/tmp/../etc", "/etc"):
                with self.subTest(requested=requested), self.assertRaises(CwdDenied):
                    authority.admit(requested)
            authority.close()

    def test_rename_keeps_admitted_directory_identity(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / "target"; target.mkdir()
            authority = PosixDirectoryAuthority.capture(root)
            lease = authority.admit("target")
            identity = lease.identity
            target.rename(root / "renamed")
            (root / "target").mkdir()
            spawn_path = lease.consume_for_spawn()
            self.assertEqual(identity, (os.stat(spawn_path).st_dev, os.stat(spawn_path).st_ino))
            lease.close(); authority.close()


class LedgerDecisionTests(unittest.TestCase):
    def test_a_process_is_gone_when_absent_or_replaced_but_not_when_unobserved(self):
        self.assertTrue(process_gone("a", None, exists=False))
        self.assertTrue(process_gone("a", "b", exists=True))
        self.assertFalse(process_gone("a", "a", exists=True))
        self.assertFalse(process_gone("a", None, exists=True))
        self.assertFalse(process_gone(None, "b", exists=True))

    def test_a_gone_entry_is_removed_only_when_release_is_certain(self):
        entry = LedgerEntry(LIVE, 7, "a")
        self.assertIsNone(after_gone(entry, release_certain=True))
        self.assertEqual(LedgerEntry(QUARANTINED, 7, "a"), after_gone(entry, release_certain=False))

    def test_owned_roots_are_the_recorded_live_and_quarantined_processes(self):
        entries = (LedgerEntry("reserved"), LedgerEntry(LIVE, 1, "a"), LedgerEntry(QUARANTINED, 2, None),
                   LedgerEntry(QUARANTINED, None, None))
        self.assertEqual(((1, "a"), (2, None)), owned_roots(entries))

    def test_a_failed_start_releases_with_certainty_only_without_a_launched_or_unconfirmed_root(self):
        self.assertTrue(failed_start_release_certain(launched=False, unconfirmed_resolved=True))
        self.assertFalse(failed_start_release_certain(launched=False, unconfirmed_resolved=False))
        self.assertFalse(failed_start_release_certain(launched=True, unconfirmed_resolved=True))


class SpawnAdmissionTests(unittest.TestCase):
    def admission(self, cell, ledger):
        authority = FakeCwdAuthority()
        return SpawnAdmission(
            reservation=ledger.reserve(), cwd_lease=authority.admit("."),
            cell_id="cell", epoch=0, fence_check=lambda epoch: True,
            cell_membership=cell,
        )

    def test_missing_process_identity_stays_quarantined_across_abort(self):
        cell = FakeCellMembership()
        ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
        admission = self.admission(cell, ledger)
        admission.consume(expected_cell_id="cell")
        cell.adopt(123456, None)
        with self.assertRaisesRegex(Exception, "identity-unavailable"):
            admission.spawned(123456)
        admission.abort()
        self.assertEqual(1, ledger.used)
        self.assertEqual(1, ledger.reconcile())
        self.assertEqual(0, ledger.used)

    def test_identity_read_failure_does_not_release_a_live_quarantine(self):
        cell = FakeCellMembership()
        ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
        admission = self.admission(cell, ledger)
        admission.consume(expected_cell_id="cell")
        cell.adopt(os.getpid(), None)
        with self.assertRaisesRegex(Exception, "identity-unavailable"):
            admission.spawned(os.getpid())
        admission.abort()
        self.assertEqual(0, ledger.reconcile())
        self.assertEqual(1, ledger.used)

    def test_a_failed_start_releases_its_reservation_and_lease(self):
        cell = FakeCellMembership()
        ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
        admission = self.admission(cell, ledger)
        with self.assertRaises(OSError):
            admission.spawn_process(("/nonexistent/loop-engine-no-such-program",),
                                    expected_cell_id="cell", options={})
        self.assertEqual(0, ledger.used)
        with self.assertRaises(CwdDenied):
            admission.cwd_lease.admitted_path()

    def test_an_admission_no_start_consumed_returns_its_reservation_and_lease_on_exit(self):
        cell = FakeCellMembership()
        ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
        with self.assertRaisesRegex(RuntimeError, "before-launch"):
            with self.admission(cell, ledger) as admission:
                raise RuntimeError("start-failed-before-launch")
        self.assertEqual(0, ledger.used)
        with self.assertRaises(CwdDenied):
            admission.cwd_lease.admitted_path()
        with self.assertRaisesRegex(AdmissionDenied, "spawn-admission-released"):
            admission.consume(expected_cell_id="cell")

    def test_releasing_a_consumed_admission_leaves_its_start_in_charge(self):
        cell = FakeCellMembership()
        ledger = ChildLedger(cell_id="cell", cell=cell, max_children=1)
        admission = self.admission(cell, ledger)
        admission.consume(expected_cell_id="cell")
        admission.release_unused()
        self.assertEqual(1, ledger.used)
        admission.abort()
        self.assertEqual(0, ledger.used)


if __name__ == "__main__":
    unittest.main()
