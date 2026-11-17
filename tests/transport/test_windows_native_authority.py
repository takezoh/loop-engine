import unittest

from provider_transport.windows_filesystem import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    DirectoryEntry,
    FilesystemAuthorityError,
    _Win32FilesystemApi,
    WindowsDirectoryAuthority,
    open_directory,
    relative_parts,
    _components,
)
from provider_transport.windows_process import (
    BREAKAWAY_OK,
    CREATE_BREAKAWAY_FROM_JOB,
    KILL_ON_JOB_CLOSE,
    WindowsJob,
    WindowsJobCell,
    WindowsProcessError,
    _Win32ProcessApi,
    creation_flags_for_native_spawn,
    windows_environment_entries,
)
from provider_transport.windows_security import private_object_sddl
from provider_transport.cwd_authority import (
    AdmissionDenied,
    ChildLedger,
    SpawnAdmission,
)
from provider_transport.directory_authority import CwdDenied
from provider_transport.windows_filesystem import open_anchor
from .windows_api_fake import FakeFilesystemApi, FakeProcessApi, any_program


class WindowsNativeAuthorityTests(unittest.TestCase):
    def test_short_directory_alias_opens_the_verified_long_name(self):
        from dataclasses import replace
        api = FakeFilesystemApi()
        api.rows["volume"] = (replace(api.rows["volume"][0], short_name="WORK~1"),)
        lease = open_directory(r"C:\work~1\child", api=api)
        self.assertEqual(lease.identity, (1, 20))
        self.assertIn(("child", "volume", "work"), api.opened)
        lease.close()

    def test_short_alias_does_not_admit_reparse_points(self):
        from dataclasses import replace
        api = FakeFilesystemApi()
        api.rows["volume"] = (replace(api.rows["volume"][0], short_name="WORK~1", attributes=FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT),)
        with self.assertRaises(FilesystemAuthorityError):
            open_directory(r"C:\work~1", api=api)
    def test_volume_components_accept_local_extended_path_but_reject_unc(self):
        self.assertEqual(_components(r"\\?\C:\root\source"), ("C:\\", ("root", "source")))
        with self.assertRaises(FilesystemAuthorityError):
            _components(r"\\?\UNC\server\share\source")

    def test_relative_parts_preserves_single_and_nested_components(self):
        self.assertEqual(relative_parts(r"C:\root", "source"), ("source",))
        self.assertEqual(relative_parts(r"C:\root", r"source\nested\file"), ("source", "nested", "file"))
        with self.assertRaises(FilesystemAuthorityError):
            relative_parts(r"C:\root", r"..\outside")

    def test_creation_policy_requires_suspended_and_rejects_breakaway(self):
        flags = creation_flags_for_native_spawn(0)
        self.assertTrue(flags & 0x00000004)
        with self.assertRaisesRegex(WindowsProcessError, "breakaway-forbidden"):
            creation_flags_for_native_spawn(CREATE_BREAKAWAY_FROM_JOB)
        with self.assertRaisesRegex(WindowsProcessError, "creation-flags-invalid"):
            creation_flags_for_native_spawn(True)

    def test_environment_mapping_is_exact_case_insensitive_and_unicode_sorted(self):
        environment = {
            "zeta": "雪",
            "alpha": "café",
        }
        entries = windows_environment_entries(environment)
        self.assertEqual(
            (("alpha", "café"), ("zeta", "雪")),
            entries,
        )
        self.assertIsNone(windows_environment_entries(None))
        self.assertEqual(
            (("Path", "C:\\tools"),),
            windows_environment_entries({"Path": "C:\\tools"}),
        )
        with self.assertRaisesRegex(WindowsProcessError, "duplicate-key"):
            windows_environment_entries({
                "Path": "first", "PATH": "second",
            })
        with self.assertRaisesRegex(WindowsProcessError, "environment-invalid"):
            windows_environment_entries({"BAD=NAME": "value"})
        with self.assertRaisesRegex(WindowsProcessError, "environment-invalid"):
            windows_environment_entries({"": "value"})

    def test_production_createprocess_path_uses_environment_validator(self):
        class FakeCtypes:
            @staticmethod
            def create_unicode_buffer(value):
                return value

        api = object.__new__(_Win32ProcessApi)
        api.ctypes = FakeCtypes
        self.assertEqual(
            "Path=C:\\tools\x00\x00",
            api._environment_block({"Path": "C:\\tools"}),
        )
        self.assertIsNone(api._environment_block(None))
        for environment, error in (
            ({"Path": "first", "PATH": "second"}, "duplicate-key"),
            ({"BAD=NAME": "value"}, "environment-invalid"),
            ({"": "value"}, "environment-invalid"),
        ):
            with self.subTest(environment=environment):
                with self.assertRaisesRegex(WindowsProcessError, error):
                    api.launch_suspended(
                        "job", ("trusted-command",),
                        options={"cwd": "C:\\work", "env": environment},
                    )

    def test_job_membership_and_identity_are_confirmed_before_resume(self):
        api = FakeProcessApi()
        job = WindowsJob(api=api)
        process = job.spawn(("trusted-command",), cwd="C:\\work")

        names = [row[0] for row in api.events]
        self.assertLess(names.index("create-suspended-in-job"), names.index("confirm-membership"))
        self.assertLess(names.index("confirm-membership"), names.index("identity"))
        self.assertLess(names.index("identity"), names.index("resume"))
        self.assertTrue(process.resumed)
        self.assertEqual(KILL_ON_JOB_CLOSE, api.job_limit_flags(job.handle))
        self.assertFalse(api.job_limit_flags(job.handle) & BREAKAWAY_OK)
        api.active[job.handle] = 0
        job.close()

    def test_unconfirmed_membership_terminates_owned_suspended_process(self):
        api = FakeProcessApi(member=False)
        job = WindowsJob(api=api)

        with self.assertRaisesRegex(WindowsProcessError, "membership-unconfirmed"):
            job.spawn(("untrusted-command",), cwd="C:\\work")

        names = [row[0] for row in api.events]
        self.assertNotIn("resume", names)
        self.assertIn("terminate-owned-process", names)
        self.assertIn("terminate-job", names)
        self.assertIn("close-process", names)
        job.close()

    def test_identity_failure_terminates_child_before_resume(self):
        api = FakeProcessApi(identity_ok=False)
        job = WindowsJob(api=api)

        with self.assertRaisesRegex(WindowsProcessError, "identity-unavailable"):
            job.spawn(("untrusted-command",), cwd="C:\\work")

        self.assertNotIn("resume", [row[0] for row in api.events])
        self.assertTrue(api.process.terminated)
        job.close()

    def test_empty_wait_keeps_receipt_identity_until_certain_release(self):
        api = FakeProcessApi()
        cell = WindowsJobCell(api=api, runnable=any_program)
        process = cell.spawn(("provider",), cwd="C:\\work", options={})
        identity = cell.process_identity(process.pid)
        api.active[process.job] = 0

        self.assertTrue(cell.wait_empty(0.01))
        self.assertTrue(cell.confirm(process.pid))
        self.assertTrue(cell.release_certain(process.pid, identity))
        self.assertIn(process.process_handle, api.closed)
        self.assertTrue(cell.empty)
        cell.close()

    def test_terminate_root_targets_its_owned_job(self):
        api = FakeProcessApi()
        cell = WindowsJobCell(api=api, runnable=any_program)
        process = cell.spawn(("provider",), cwd="C:\\work", options={})

        cell.terminate_root(process.pid)

        self.assertEqual(0, api.active[process.job])
        self.assertTrue(cell.wait_empty(0.01))
        identity = cell.process_identity(process.pid)
        self.assertTrue(cell.release_certain(process.pid, identity))
        cell.close()

    def test_create_failure_proves_uncertain_roots_without_waiting_for_live_peers(self):
        fs_api = FakeFilesystemApi()
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=fs_api)
        process_api = FakeProcessApi()
        cell = WindowsJobCell(api=process_api, runnable=any_program)
        existing = cell.spawn(("existing-provider",), cwd="C:\\work", options={})
        ledger = ChildLedger(cell_id="fake-windows-cell", cell=cell)
        admission = SpawnAdmission(
            reservation=ledger.reserve(),
            cwd_lease=authority.admit("."),
            cell_id="fake-windows-cell",
            epoch=7,
            fence_check=lambda _epoch: True,
            cell_membership=cell,
        )
        process_api.fail_launch = True
        try:
            with self.assertRaisesRegex(WindowsProcessError, "fake-process-create-failed"):
                admission.spawn_process(
                    ("new-provider",),
                    expected_cell_id="fake-windows-cell",
                    options={},
                )

            self.assertEqual(0, ledger.used)
            self.assertEqual(1, process_api.active[existing.job])
            self.assertTrue(cell.confirm(existing.pid))
        finally:
            process_api.active[existing.job] = 0
            identity = cell.process_identity(existing.pid)
            cell.release_certain(existing.pid, identity)
            cell.close()
            authority.close()

    def test_unconfirmed_root_cannot_release_until_kernel_job_is_empty(self):
        api = FakeProcessApi(identity_ok=False)
        api.termination_sticks = True
        cell = WindowsJobCell(api=api, runnable=any_program)

        with self.assertRaisesRegex(WindowsProcessError, "identity-unavailable"):
            cell.spawn(("untrusted-provider",), cwd="C:\\work", options={})

        job = next(iter(api.active))
        self.assertFalse(cell.resolve_unconfirmed_empty(0.01))
        self.assertEqual(1, api.active[job])
        api.active[job] = 0
        self.assertTrue(cell.resolve_unconfirmed_empty(0.01))
        self.assertEqual(0, api.active[job])
        cell.close()

    def test_spawn_admission_holds_cwd_through_native_receipt(self):
        fs_api = FakeFilesystemApi()
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=fs_api)
        process_api = FakeProcessApi()
        cell = WindowsJobCell(api=process_api, runnable=any_program)

        ledger = ChildLedger(cell_id="fake-windows-cell", cell=cell)
        admission = SpawnAdmission(
            reservation=ledger.reserve(),
            cwd_lease=authority.admit("."),
            cell_id="fake-windows-cell",
            epoch=7,
            fence_check=lambda epoch: epoch == 7,
            cell_membership=cell,
        )
        try:
            process, receipt = admission.spawn_process(
                ("provider",),
                expected_cell_id="fake-windows-cell",
                options={"stdin": None},
            )
            launch = next(row for row in process_api.events if row[0] == "create-suspended-in-job")
            self.assertEqual("C:\\work", launch[3]["cwd"])
            self.assertEqual(process.pid, receipt.pid)
            self.assertEqual("windows:4001:987654", receipt.process_start_identity)
            process_api.active[process.job] = 0
            ledger.release_live(receipt, certain=True)
            self.assertEqual(0, ledger.used)
            cell.close()
        finally:
            authority.close()

    def test_wrong_cell_is_rejected_before_native_process_creation(self):
        fs_api = FakeFilesystemApi()
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=fs_api)
        process_api = FakeProcessApi()
        cell = WindowsJobCell(api=process_api, runnable=any_program)
        ledger = ChildLedger(cell_id="fake-windows-cell", cell=cell)
        admission = SpawnAdmission(
            reservation=ledger.reserve(),
            cwd_lease=authority.admit("."),
            cell_id="fake-windows-cell",
            epoch=7,
            fence_check=lambda _epoch: True,
            cell_membership=cell,
        )
        try:
            with self.assertRaisesRegex(AdmissionDenied, "cell-mismatch"):
                admission.spawn_process(
                    ("provider",),
                    expected_cell_id="different-cell",
                    options={},
                )
            self.assertNotIn(
                "create-suspended-in-job",
                [row[0] for row in process_api.events],
            )
            self.assertEqual(0, ledger.used)
        finally:
            cell.close()
            authority.close()

    def test_directory_authority_holds_volume_to_leaf_handles(self):
        api = FakeFilesystemApi()
        lease = open_directory("C:\\work", api=api)
        self.assertEqual((1, 10), lease.identity)
        self.assertEqual("C:\\work", lease.spawn_path)
        self.assertEqual([("volume", "C:\\"), ("child", "volume", "work")], api.opened)
        lease.close()
        self.assertEqual(["work", "volume"], api.closed)

    def test_cwd_authority_holds_only_its_origin_not_its_ancestors(self):
        api = FakeFilesystemApi()
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=api)
        try:
            self.assertEqual([("anchor", "C:\\work")], api.opened)
            lease = authority.admit("child")
            self.assertEqual("C:\\work\\child", lease.consume_for_spawn())
            self.assertNotIn("volume", [row[0] for row in api.opened])
            lease.close()
            self.assertEqual(["child", "work"], api.closed)
        finally:
            authority.close()

    def test_admission_rejects_an_origin_path_that_names_another_directory(self):
        api = FakeFilesystemApi()
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=api)
        try:
            api.ids["work"] = (1, 99)
            for requested in (".", "child"):
                with self.subTest(requested=requested):
                    with self.assertRaisesRegex(CwdDenied, "alias-changed"):
                        authority.admit(requested)
            self.assertNotIn(("child", "work", "child"), api.opened)
        finally:
            authority.close()

    def test_directory_handles_share_writes_but_never_delete_and_files_share_reads(self):
        opens = []

        class Handle:
            def __init__(self, value):
                self.value = value

        class Kernel:
            def CreateFileW(self, path, _access, share, *_args):
                opens.append((path, share))
                return len(opens)

        api = object.__new__(_Win32FilesystemApi)
        api.kernel32 = Kernel()
        api.wintypes = type("FakeWinTypes", (), {"HANDLE": Handle})
        api.attributes = lambda handle: (
            FILE_ATTRIBUTE_DIRECTORY if opens[handle - 1][0].endswith("dir") else 0
        )
        api.open_anchor("C:\\dir")
        api.open_volume_root("C:\\dir")
        api._open("C:\\file", directory=False)
        self.assertEqual(
            [
                ("C:\\dir", FILE_SHARE_READ | FILE_SHARE_WRITE),
                ("C:\\dir", FILE_SHARE_READ | FILE_SHARE_WRITE),
                ("C:\\file", FILE_SHARE_READ),
            ],
            opens,
        )

    def test_relative_file_read_uses_validated_handle_and_rejects_reparse(self):
        api = FakeFilesystemApi()
        lease = open_anchor("C:\\work", api=api)
        try:
            data, identity = lease.read_file("payload.json")
            self.assertEqual(b'{"ok":true}', data)
            self.assertEqual((1, 30), identity)
            self.assertIn(("read-handle", "payload.json"), api.opened)
            with self.assertRaisesRegex(FilesystemAuthorityError, "reparse"):
                lease.open_relative("junction", final_directory=True)
        finally:
            lease.close()

    def test_alias_swap_and_outside_root_are_denied(self):
        api = FakeFilesystemApi(swapped={"child"})
        authority = WindowsDirectoryAuthority.capture("C:\\work", api=api)
        lease = open_anchor("C:\\work", api=api)
        try:
            with self.assertRaisesRegex(FilesystemAuthorityError, "alias-changed"):
                lease.open_relative("child", final_directory=True)
            with self.assertRaisesRegex(CwdDenied, "alias-changed"):
                authority.admit("child")
            with self.assertRaisesRegex(CwdDenied, "outside-root"):
                authority.admit("C:\\sibling")
        finally:
            lease.close()
            authority.close()

    def test_private_object_descriptor_grants_only_the_user_and_system(self):
        user = "S-1-5-21-1-2-3-1001"
        self.assertEqual(
            f"O:{user}D:P(A;OICI;GA;;;{user})(A;OICI;GA;;;S-1-5-18)",
            private_object_sddl(user, inherit=True),
        )


if __name__ == "__main__":
    unittest.main()
