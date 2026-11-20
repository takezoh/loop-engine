"""The Claude ACP adapter: where it is installed, its pinned patch, its install and check, and the
launcher that starts it. npm and node are replaced by values; nothing here downloads or starts them."""
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from provider_transport import claude_adapter
from provider_transport.command_search import search_values
from provider_transport.launchers import claude_agent_acp as launcher


def _patch_text(key):
    """The patch's replacement texts on one side, joined: a bridge source that holds exactly its anchors."""
    replacements = claude_adapter._patch_document()["replacements"]
    return "\n".join(str(item[key]) for item in replacements).encode("utf-8")


PATCH_BEFORE = _patch_text("before")
PATCH_AFTER = _patch_text("after")


def place_package(target, *, version=claude_adapter.VERSION, bridge=PATCH_AFTER):
    """What `npm ci` leaves in `target` for the adapter: its metadata, its command link and its bridge."""
    package = target / "node_modules" / Path(*claude_adapter.PACKAGE.split("/"))
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({"name": claude_adapter.PACKAGE, "version": version}), encoding="utf-8")
    binary = claude_adapter._binary_path(target)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    if bridge is not None:
        path = target / claude_adapter.BRIDGE_RELATIVE_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bridge)


def fixture_images():
    """Pin the fixture bridge instead of the real package's file, which is not in the repository."""
    return (patch.object(claude_adapter, "PREIMAGE_SHA256", claude_adapter.sha256_bytes(PATCH_BEFORE)),
            patch.object(claude_adapter, "POSTIMAGE_SHA256", claude_adapter.sha256_bytes(PATCH_AFTER)))


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


class BridgePatchTests(unittest.TestCase):
    def test_claude_bridge_transform_is_exact_and_idempotent(self):
        source = PATCH_BEFORE
        preimage = claude_adapter.sha256_bytes(source)
        expected = source.replace(PATCH_BEFORE, PATCH_AFTER)
        postimage = claude_adapter.sha256_bytes(expected)

        patched = claude_adapter.transform_bridge(
            source, expected_preimage=preimage, expected_postimage=postimage
        )

        self.assertEqual(expected, patched)
        self.assertEqual(
            patched,
            claude_adapter.transform_bridge(
                patched,
                expected_preimage=preimage,
                expected_postimage=postimage,
            ),
        )

    def test_claude_bridge_transform_rejects_unknown_or_partial_bytes(self):
        with self.assertRaisesRegex(RuntimeError, "bridge-source-drift"):
            claude_adapter.transform_bridge(
                b"partial teardownSession",
                expected_preimage="0" * 64,
                expected_postimage="1" * 64,
            )

    def test_applying_the_patch_rewrites_the_bridge_once_and_keeps_its_mode(self):
        with TemporaryDirectory() as data:
            target = Path(data)
            place_package(target, bridge=PATCH_BEFORE)
            bridge = target / claude_adapter.BRIDGE_RELATIVE_PATH
            bridge.chmod(0o640)
            before, after = fixture_images()
            with before, after:
                claude_adapter.apply_bridge_patch(target)
                self.assertEqual(PATCH_AFTER, bridge.read_bytes())
                claude_adapter.apply_bridge_patch(target)
            self.assertEqual(PATCH_AFTER, bridge.read_bytes())
            if os.name != "nt":
                self.assertEqual(0o640, bridge.stat().st_mode & 0o777)
            self.assertEqual([bridge.name], sorted(path.name for path in bridge.parent.iterdir()))

    def test_applying_the_patch_without_a_bridge_is_refused(self):
        with TemporaryDirectory() as data:
            with self.assertRaisesRegex(RuntimeError, "claude-adapter-bridge-missing"):
                claude_adapter.apply_bridge_patch(Path(data))


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
    def test_install_uses_the_engines_lockfile_and_verifies_the_patched_adapter(self):
        with TemporaryDirectory() as engine, TemporaryDirectory() as data:
            package_root = Path(engine)
            target = Path(data) / "loop-engine"
            (package_root / "package.json").write_text("{}", encoding="utf-8")
            (package_root / "package-lock.json").write_text("{}", encoding="utf-8")

            def fake_npm(argv, check, timeout):
                self.assertEqual(
                    ["npm", "ci", "--omit=dev", "--prefix", str(target)], argv
                )
                self.assertTrue(check)
                self.assertEqual(claude_adapter.INSTALL_TIMEOUT_SECONDS, timeout)
                self.assertEqual("{}", (target / "package-lock.json").read_text())
                place_package(target, bridge=PATCH_BEFORE)

            before, after = fixture_images()
            with patch.object(claude_adapter.subprocess, "run", side_effect=fake_npm) as run, before, after:
                claude_adapter.install(target, package_root=package_root)
                claude_adapter.verify(target)

            run.assert_called_once()
            self.assertEqual("{}", (target / "package.json").read_text())
            self.assertEqual("{}", (target / "package-lock.json").read_text())
            self.assertEqual(
                PATCH_AFTER,
                (target / claude_adapter.BRIDGE_RELATIVE_PATH).read_bytes(),
            )

    def test_install_of_a_package_whose_bridge_is_not_the_pinned_one_fails(self):
        with TemporaryDirectory() as engine, TemporaryDirectory() as data:
            package_root, target = Path(engine), Path(data) / "loop-engine"
            for name in claude_adapter.PACKAGE_FILES:
                (package_root / name).write_text("{}", encoding="utf-8")
            before, after = fixture_images()
            with patch.object(claude_adapter.subprocess, "run",
                              side_effect=lambda argv, check, timeout: place_package(target, bridge=b"another build")), \
                    before, after:
                with self.assertRaisesRegex(RuntimeError, "claude-adapter-bridge-source-drift"):
                    claude_adapter.install(target, package_root=package_root)

    def test_check_rejects_missing_provider(self):
        with TemporaryDirectory() as data:
            with self.assertRaisesRegex(RuntimeError, "claude-adapter-missing"):
                claude_adapter.verify(Path(data))

    def test_verify_rejects_another_version_and_an_unpatched_bridge(self):
        before, after = fixture_images()
        with before, after:
            with TemporaryDirectory() as data:
                place_package(Path(data))
                claude_adapter.verify(Path(data))
            with TemporaryDirectory() as data:
                place_package(Path(data), version="0.0.0")
                with self.assertRaisesRegex(RuntimeError, "claude-adapter-version-mismatch"):
                    claude_adapter.verify(Path(data))
            with TemporaryDirectory() as data:
                place_package(Path(data), bridge=None)
                with self.assertRaisesRegex(RuntimeError, "claude-adapter-bridge-missing"):
                    claude_adapter.verify(Path(data))
            with TemporaryDirectory() as data:
                place_package(Path(data), bridge=PATCH_BEFORE)
                with self.assertRaisesRegex(RuntimeError, "claude-adapter-bridge-generation-mismatch"):
                    claude_adapter.verify(Path(data))


class DecisionsFromValuesTests(unittest.TestCase):
    """What the adapter decides, given the values the installer and the launcher read."""

    def failure(self, **changes):
        values = {"metadata": {"name": claude_adapter.PACKAGE, "version": claude_adapter.VERSION},
                  "has_binary": True, "bridge_digest": claude_adapter.POSTIMAGE_SHA256, **changes}
        return claude_adapter.verification_failure("/runtime", **values)

    def test_only_the_pinned_package_with_the_pinned_patch_passes(self):
        self.assertIsNone(self.failure())
        self.assertEqual("claude-adapter-missing:/runtime", self.failure(metadata=None))
        self.assertEqual("claude-adapter-missing:/runtime", self.failure(has_binary=False))
        self.assertEqual("claude-adapter-version-mismatch",
                         self.failure(metadata={"name": claude_adapter.PACKAGE, "version": "0.0.0"}))
        self.assertEqual("claude-adapter-version-mismatch",
                         self.failure(metadata={"name": "another", "version": claude_adapter.VERSION}))
        self.assertEqual("claude-adapter-bridge-missing:/runtime", self.failure(bridge_digest=None))
        self.assertEqual("claude-adapter-bridge-generation-mismatch:" + claude_adapter.PREIMAGE_SHA256,
                         self.failure(bridge_digest=claude_adapter.PREIMAGE_SHA256))

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


class LauncherTests(unittest.TestCase):
    def launch(self, environ, *, windows=False, verify=None, node_files=(), arguments=("ok",)):
        """Run the Claude launcher's main in this process; returns (exit status, the argv it started)."""
        started = []
        self.environments = []

        def execve(path, argv, env):
            started.append(list(argv))
            self.environments.append(dict(env))
            raise SystemExit(0)

        def run(argv, check, env):
            started.append(list(argv))
            self.environments.append(dict(env))
            return subprocess.CompletedProcess(argv, 0)

        search = search_values(
            environ, windows=windows, runnable=set(node_files).__contains__)
        with patch.dict(os.environ, environ, clear=True), \
                patch.object(launcher, "is_windows", return_value=windows), \
                patch.object(launcher, "host_command_search", return_value=search), \
                patch.object(claude_adapter, "verify", side_effect=verify) as verified, \
                patch.object(launcher.os, "execve", side_effect=execve), \
                patch.object(launcher.subprocess, "run", side_effect=run):
            try:
                status = launcher.main(list(arguments))
            except SystemExit as exit:
                status = exit.code
        self.verified = [call.args[0] for call in verified.call_args_list]
        return status, started

    def test_bundled_launcher_starts_the_adapter_entry_from_the_runtime_directory(self):
        status, started = self.launch({"HOME": "/home/test", "PATH": "/opt/node/bin"},
                                      node_files={"/opt/node/bin/node"})
        self.assertEqual(0, status)
        runtime = Path("/home/test/.local/share/loop-engine")
        self.assertEqual([[
            "/opt/node/bin/node",
            str(runtime / claude_adapter.ENTRY_RELATIVE_PATH),
            "ok",
        ]], started)
        self.assertEqual([runtime], self.verified)

    def test_bundled_launcher_offers_the_bound_model_as_the_custom_option(self):
        for windows, environ, node in (
            (False, {"HOME": "/home/test", "PATH": "/opt/node/bin"}, "/opt/node/bin/node"),
            (True, {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local", "Path": r"C:\node"}, r"C:\node\node.exe"),
        ):
            with self.subTest(windows=windows):
                status, started = self.launch(environ, windows=windows, node_files={node},
                                              arguments=("--model", "claude-opus-5-5", "ok"))
                self.assertEqual(0, status)
                self.assertEqual(["ok"], started[0][2:])
                self.assertEqual("claude-opus-5-5", self.environments[0]["ANTHROPIC_CUSTOM_MODEL_OPTION"])

    def test_bundled_launcher_without_a_model_leaves_the_environment_as_it_is(self):
        status, _ = self.launch({"HOME": "/home/test", "PATH": "/opt/node/bin"},
                                node_files={"/opt/node/bin/node"})
        self.assertEqual(0, status)
        self.assertNotIn("ANTHROPIC_CUSTOM_MODEL_OPTION", self.environments[0])

    def test_bundled_launcher_refuses_a_malformed_model_before_starting(self):
        for arguments in (("--model",), ("--model", " "), ("--model", "a", "--model", "b")):
            with self.subTest(arguments=arguments):
                status, started = self.launch({"HOME": "/home/test", "PATH": "/opt/node/bin"},
                                              node_files={"/opt/node/bin/node"}, arguments=arguments)
                self.assertEqual(2, status)
                self.assertEqual([], started)

    def test_bundled_launcher_uses_install_time_runtime_override(self):
        status, started = self.launch(
            {"HOME": "/home/test", "PATH": "/opt/node/bin", "LOOP_ENGINE_RUNTIME_DIR": "/srv/shared runtime"},
            node_files={"/opt/node/bin/node"},
        )
        self.assertEqual(0, status)
        self.assertEqual(str(Path("/srv/shared runtime") / claude_adapter.ENTRY_RELATIVE_PATH), started[0][1])
        self.assertEqual([Path("/srv/shared runtime")], self.verified)

    def test_bundled_launcher_starts_nothing_when_the_installed_adapter_fails_its_check(self):
        status, started = self.launch(
            {"HOME": "/home/test", "PATH": "/opt/node/bin"}, node_files={"/opt/node/bin/node"},
            verify=RuntimeError("claude-adapter-bridge-generation-mismatch"),
        )
        self.assertEqual(127, status)
        self.assertEqual([], started)

    def test_bundled_launcher_starts_nothing_when_no_adapter_is_installed(self):
        with TemporaryDirectory() as data:
            search = search_values({"PATH": "/opt/node/bin"}, windows=False, runnable={"/opt/node/bin/node"}.__contains__)
            with patch.dict(os.environ, {"LOOP_ENGINE_RUNTIME_DIR": data, "PATH": "/opt/node/bin"}, clear=True), \
                    patch.object(launcher, "is_windows", return_value=False), \
                    patch.object(launcher, "host_command_search", return_value=search), \
                    patch.object(launcher.os, "execve") as execve, \
                    patch.object(launcher.subprocess, "run") as run:
                self.assertEqual(127, launcher.main(["ok"]))
            execve.assert_not_called()
            run.assert_not_called()

    def test_bundled_launcher_on_windows_starts_node_exe_and_refuses_a_node_shim(self):
        environ = {"LOCALAPPDATA": r"C:\Users\Test\AppData\Local", "Path": r"C:\shims;C:\node"}
        status, started = self.launch(environ, windows=True, node_files={r"C:\node\node.exe"})
        self.assertEqual(0, status)
        self.assertEqual(r"C:\node\node.exe", started[0][0])
        status, started = self.launch(environ, windows=True, node_files={r"C:\shims\node.cmd"})
        self.assertEqual(127, status)
        self.assertEqual([], started)


if __name__ == "__main__":
    unittest.main()
