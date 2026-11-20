"""The Claude ACP adapter: where it is installed, its install and its check. npm and node are replaced
by values; nothing here downloads or starts them."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provider_transport import claude_adapter


def place_package(target, *, name=claude_adapter.PACKAGE, entry=True):
    """What `npm install` leaves in `target` for the adapter: its metadata and its entry script."""
    metadata = target / claude_adapter.METADATA_RELATIVE_PATH
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"name": name, "version": "1.2.3"}), encoding="utf-8")
    if entry:
        path = target / claude_adapter.ENTRY_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")


class RuntimeDirectoryTests(unittest.TestCase):
    def test_runtime_dir_is_the_engines_own_data_directory(self):
        self.assertEqual(
            Path("/home/test/.local/share/loop-engine"),
            claude_adapter.runtime_dir({"HOME": "/home/test"}, windows=False),
        )

    def test_runtime_dir_accepts_one_install_time_override(self):
        self.assertEqual(
            Path("/opt/engine-runtime"),
            claude_adapter.runtime_dir(
                {"HOME": "/home/test", "LOOP_ENGINE_RUNTIME_DIR": "/opt/engine-runtime"}, windows=False),
        )

    def test_native_runtime_defaults_to_local_app_data(self):
        self.assertEqual(
            Path("C:/Users/Test/AppData/Local/loop-engine"),
            claude_adapter.runtime_dir({"LOCALAPPDATA": "C:/Users/Test/AppData/Local"}, windows=True),
        )

    def test_runtime_dir_without_a_user_directory_is_refused(self):
        with self.assertRaisesRegex(RuntimeError, "claude-adapter-home-unavailable"):
            claude_adapter.runtime_dir({}, windows=False)
        with self.assertRaisesRegex(RuntimeError, "claude-adapter-localappdata-unavailable"):
            claude_adapter.runtime_dir({"HOME": "/home/test"}, windows=True)


class NpmCommandTests(unittest.TestCase):
    def test_native_npm_resolution_invokes_node_with_npm_cli_not_cmd(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            # node.exe on PATH is a version manager's shim with no npm beside it;
            # Node itself reports the installation that holds npm-cli.js.
            shim = root / "shims/node.exe"
            shim.parent.mkdir()
            shim.write_bytes(b"shim")
            node = root / "installs/node/24.1.0/node.exe"
            npm_cli = node.parent / "node_modules/npm/bin/npm-cli.js"
            npm_cli.parent.mkdir(parents=True)
            node.write_bytes(b"node")
            npm_cli.write_text("// npm\n", encoding="utf-8")
            cases = (
                (json.dumps([str(node), "24.1.0"]), None),
                (json.dumps([str(node), "20.9.0"]), "claude-adapter-node-22-required"),
                (json.dumps([str(root / "missing/node.exe"), "24.1.0"]), "claude-adapter-node-unavailable"),
                ("not json", "claude-adapter-node-version-invalid"),
            )
            for stdout, error in cases:
                with self.subTest(error=error), patch.object(
                    claude_adapter.shutil,
                    "which",
                    side_effect=lambda name: str(shim) if name == "node.exe" else None,
                ), patch.object(
                    claude_adapter.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, stdout, ""),
                ) as run:
                    if error:
                        with self.assertRaisesRegex(RuntimeError, error):
                            claude_adapter.npm_command(windows=True)
                    else:
                        command = claude_adapter.npm_command(windows=True)
                        self.assertEqual([str(node.resolve()), str(npm_cli.resolve())], command)
                    run.assert_called_once_with(
                        [str(shim), "-e", claude_adapter.NODE_PROBE], capture_output=True, text=True,
                        timeout=15, check=False,
                    )
            npm_cli.unlink()
            with patch.object(claude_adapter.shutil, "which", return_value=str(shim)), patch.object(
                claude_adapter.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 0, json.dumps([str(node), "24.1.0"]), ""),
            ), self.assertRaisesRegex(RuntimeError, "claude-adapter-npm-cli-missing"):
                claude_adapter.native_npm_command()
            with patch.object(claude_adapter.shutil, "which", return_value=None), \
                    self.assertRaisesRegex(RuntimeError, "claude-adapter-node-and-npm-required"):
                claude_adapter.native_npm_command()
            self.assertEqual(["npm"], claude_adapter.npm_command(windows=False))


class InstallAndVerifyTests(unittest.TestCase):
    """What install asks npm for; which program runs npm on each platform is `NpmCommandTests`' subject."""

    def setUp(self):
        npm = patch.object(claude_adapter, "npm_command", return_value=["npm"])
        npm.start()
        self.addCleanup(npm.stop)

    def test_install_places_the_latest_release_without_optional_dependencies_and_verifies(self):
        with TemporaryDirectory() as data:
            target = Path(data) / "loop-engine"

            def fake_npm(argv, check, timeout):
                self.assertEqual(
                    ["npm", "install", "--prefix", str(target), "--omit=dev", "--omit=optional",
                     claude_adapter.PACKAGE + "@latest"], argv)
                self.assertTrue(check)
                self.assertEqual(claude_adapter.INSTALL_TIMEOUT_SECONDS, timeout)
                place_package(target)

            with patch.object(claude_adapter.subprocess, "run", side_effect=fake_npm) as run:
                claude_adapter.install(target)
                claude_adapter.verify(target)
            run.assert_called_once()

    def test_install_that_leaves_no_adapter_fails(self):
        with TemporaryDirectory() as data:
            target = Path(data) / "loop-engine"
            with patch.object(claude_adapter.subprocess, "run",
                              side_effect=lambda argv, check, timeout: place_package(target, entry=False)):
                with self.assertRaisesRegex(RuntimeError, "claude-adapter-missing"):
                    claude_adapter.install(target)

    def test_verify_rejects_a_missing_package_another_package_and_a_missing_entry(self):
        with TemporaryDirectory() as data:
            with self.assertRaisesRegex(RuntimeError, "claude-adapter-missing"):
                claude_adapter.verify(Path(data))
        with TemporaryDirectory() as data:
            place_package(Path(data))
            claude_adapter.verify(Path(data))
        with TemporaryDirectory() as data:
            place_package(Path(data), name="another")
            with self.assertRaisesRegex(RuntimeError, "claude-adapter-package-mismatch"):
                claude_adapter.verify(Path(data))
        with TemporaryDirectory() as data:
            place_package(Path(data), entry=False)
            with self.assertRaisesRegex(RuntimeError, "claude-adapter-missing"):
                claude_adapter.verify(Path(data))


class DecisionsFromValuesTests(unittest.TestCase):
    """What the adapter decides, given the values the installer and the launcher read."""

    def failure(self, **changes):
        values = {"metadata": {"name": claude_adapter.PACKAGE, "version": "1.2.3"},
                  "has_entry": True, **changes}
        return claude_adapter.verification_failure("/runtime", **values)

    def test_only_the_adapter_package_with_its_entry_passes(self):
        self.assertIsNone(self.failure())
        self.assertEqual("claude-adapter-missing:/runtime", self.failure(metadata=None))
        self.assertEqual("claude-adapter-missing:/runtime", self.failure(has_entry=False))
        self.assertEqual("claude-adapter-package-mismatch",
                         self.failure(metadata={"name": "another", "version": "1.2.3"}))

    def test_a_probed_node_is_usable_only_when_it_ran_reported_itself_and_is_22_or_later(self):
        self.assertEqual("/n/node.exe", claude_adapter.node_executable(0, json.dumps(["/n/node.exe", "22.0.0"])))
        for returncode, stdout, error in (
                (1, json.dumps(["/n/node.exe", "24.1.0"]), "claude-adapter-node-unavailable"),
                (0, json.dumps(["/n/node.exe", "21.9.9"]), "claude-adapter-node-22-required"),
                (0, json.dumps(["", "24.1.0"]), "claude-adapter-node-version-invalid"),
                (0, json.dumps([None, "24.1.0"]), "claude-adapter-node-version-invalid"),
                (0, json.dumps(["/n/node.exe"]), "claude-adapter-node-version-invalid"),
                (0, json.dumps(["/n/node.exe", "latest"]), "claude-adapter-node-version-invalid"),
                (0, "not json", "claude-adapter-node-version-invalid")):
            with self.subTest(stdout=stdout, returncode=returncode), self.assertRaisesRegex(RuntimeError, error):
                claude_adapter.node_executable(returncode, stdout)


if __name__ == "__main__":
    unittest.main()
