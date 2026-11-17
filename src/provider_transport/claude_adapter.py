"""Install and verify the Claude ACP adapter the `claude` provider starts.

The adapter is a pinned npm package with one pinned patch. It lives in the transport's runtime
directory, apart from every caller's files. `install` places it there and `verify` confirms that what
is there is exactly the pinned, patched package; the launcher verifies before every start. The lock
that keeps two installs apart belongs to the caller of `install`.

What is decided is decided from values: where the adapter lives (`runtime_dir`), what the patched
file is (`transform_bridge`), whether what is installed is the pinned adapter
(`verification_failure`) and which Node may run npm (`node_executable`). The functions that read
files and start processes only gather those values and act on the answers.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Mapping

from .host_platform import is_windows

PACKAGE = "@agentclientprotocol/claude-agent-acp"
VERSION = "0.62.0"
PACKAGE_FILES = ("package.json", "package-lock.json")
# The directory that holds the manifests: the root of this installation of the engine.
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PATCH_PATH = Path(__file__).resolve().parent / "patches/claude-agent-acp-0.62.0-finite-consumer-join-v1.json"
PATCH_SHA256 = "6cc12058b1422684f66a6e3d2b322c169d788047478d943d74638404e624cbb8"
PREIMAGE_SHA256 = "052c2ddfcbff35cee0efdede54d76e8ecddf3653768267290237cb101d280d03"
POSTIMAGE_SHA256 = "be4d60cd62ba1bc0ad17cc11ab5a618808b9de06c1615549b9a35681278e5082"
BRIDGE_RELATIVE_PATH = Path("node_modules/@agentclientprotocol/claude-agent-acp/dist/acp-agent.js")
# The package's "bin" entry; node starts it directly, without the .bin link or .cmd shim.
ENTRY_RELATIVE_PATH = Path("node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js")
INSTALL_TIMEOUT_SECONDS = 300
REPARSE_POINT = 0x0400
NODE_PROBE = "process.stdout.write(JSON.stringify([process.execPath, process.versions.node]))"


def runtime_dir(environ: Mapping[str, str] | None = None, *, windows: bool | None = None) -> Path:
    """Where the adapter is installed: `LOOP_ENGINE_RUNTIME_DIR`, else the user's data directory."""
    env = os.environ if environ is None else environ
    windows = is_windows() if windows is None else windows
    configured = env.get("LOOP_ENGINE_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser()
    if windows:
        local = env.get("LOCALAPPDATA")
        if not local:
            raise RuntimeError("claude-adapter-localappdata-unavailable")
        return Path(local) / "loop-engine"
    home = env.get("HOME")
    if not home:
        raise RuntimeError("claude-adapter-home-unavailable")
    return Path(home).expanduser() / ".local" / "share" / "loop-engine"


def _is_reparse_point(path: Path) -> bool:
    try:
        observed = path.lstat()
    except FileNotFoundError:
        return False
    return path.is_symlink() or bool(getattr(observed, "st_file_attributes", 0) & REPARSE_POINT)


def assert_no_reparse(path: Path, root: Path) -> None:
    """Refuse a path that leaves `root` or passes through a link or a Windows reparse point."""
    absolute, base = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    try:
        absolute.relative_to(base)
    except ValueError as exc:
        raise RuntimeError("claude-adapter-path-escaped") from exc
    current = Path(absolute.anchor)
    if _is_reparse_point(current):
        raise RuntimeError("claude-adapter-reparse-target")
    for component in absolute.parts[1:]:
        current = current / component
        if _is_reparse_point(current):
            raise RuntimeError("claude-adapter-reparse-target")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _patch_document() -> dict[str, object]:
    encoded = PATCH_PATH.read_bytes()
    if sha256_bytes(encoded) != PATCH_SHA256:
        raise RuntimeError("claude-adapter-patch-asset-drift")
    document = json.loads(encoded)
    if (
        type(document.get("schema_version")) is not int
        or document["schema_version"] != 1
        or document.get("package") != PACKAGE
        or document.get("version") != VERSION
        or document.get("target") != "dist/acp-agent.js"
    ):
        raise RuntimeError("claude-adapter-patch-contract-invalid")
    replacements = document.get("replacements")
    if (
        not isinstance(replacements, list)
        or not replacements
        or any(
            not isinstance(item, dict)
            or not isinstance(item.get("before"), str)
            or not item["before"]
            or not isinstance(item.get("after"), str)
            or not item["after"]
            for item in replacements
        )
    ):
        raise RuntimeError("claude-adapter-patch-contract-invalid")
    return document


def transform_bridge(
    source: bytes, *, expected_preimage: str | None = None, expected_postimage: str | None = None,
) -> bytes:
    """The patched bridge: the pinned package's file with every replacement applied exactly once."""
    expected_preimage = expected_preimage or PREIMAGE_SHA256
    expected_postimage = expected_postimage or POSTIMAGE_SHA256
    observed = sha256_bytes(source)
    if observed == expected_postimage:
        return source
    if observed != expected_preimage:
        raise RuntimeError(f"claude-adapter-bridge-source-drift:{observed}")
    text = source.decode("utf-8")
    replacements = _patch_document()["replacements"]
    assert isinstance(replacements, list)
    for item in replacements:
        before, after = str(item["before"]), str(item["after"])
        if text.count(before) != 1:
            raise RuntimeError("claude-adapter-patch-anchor-drift")
        text = text.replace(before, after)
    patched = text.encode("utf-8")
    if sha256_bytes(patched) != expected_postimage:
        raise RuntimeError("claude-adapter-patch-postimage-mismatch")
    return patched


def apply_bridge_patch(target: Path) -> None:
    bridge = target / BRIDGE_RELATIVE_PATH
    if not bridge.is_file():
        raise RuntimeError(f"claude-adapter-bridge-missing:{target}")
    source = bridge.read_bytes()
    patched = transform_bridge(source)
    if patched == source:
        return
    mode = bridge.stat().st_mode
    with tempfile.NamedTemporaryFile(dir=bridge.parent, delete=False) as stream:
        staged = Path(stream.name)
        stream.write(patched)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        staged.chmod(mode)
        os.replace(staged, bridge)
    finally:
        staged.unlink(missing_ok=True)


def _binary_path(target: Path, *, windows: bool | None = None) -> Path:
    windows = is_windows() if windows is None else windows
    return target / "node_modules" / ".bin" / ("claude-agent-acp.cmd" if windows else "claude-agent-acp")


def verification_failure(target: object, *, metadata: object, has_binary: bool, bridge_digest: str | None) -> str | None:
    """Why what was read from `target` is not the pinned, patched adapter, or None when it is.

    `metadata` is the installed package's parsed `package.json`, or None when there is none;
    `bridge_digest` is the sha256 of the installed bridge file, or None when there is none.
    """
    if not isinstance(metadata, dict) or not has_binary:
        return f"claude-adapter-missing:{target}"
    if metadata.get("name") != PACKAGE or metadata.get("version") != VERSION:
        return "claude-adapter-version-mismatch"
    if bridge_digest is None:
        return f"claude-adapter-bridge-missing:{target}"
    if bridge_digest != POSTIMAGE_SHA256:
        return f"claude-adapter-bridge-generation-mismatch:{bridge_digest}"
    return None


def verify(target: Path) -> None:
    """Raise unless `target` holds the pinned package with the pinned patch applied."""
    metadata = target / "node_modules" / Path(*PACKAGE.split("/")) / "package.json"
    bridge = target / BRIDGE_RELATIVE_PATH
    failure = verification_failure(
        target,
        metadata=json.loads(metadata.read_text(encoding="utf-8")) if metadata.is_file() else None,
        has_binary=_binary_path(target).is_file(),
        bridge_digest=sha256_bytes(bridge.read_bytes()) if bridge.is_file() else None,
    )
    if failure is not None:
        raise RuntimeError(failure)


def node_executable(returncode: int, stdout: str) -> str:
    """The executable a probed Node reports for itself, or raise: the probe failed, its output is not
    what `NODE_PROBE` prints, or the Node is older than version 22."""
    if returncode != 0:
        raise RuntimeError("claude-adapter-node-unavailable")
    try:
        executable, observed = json.loads(stdout)
        major = int(str(observed).split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise RuntimeError("claude-adapter-node-version-invalid") from exc
    if not isinstance(executable, str) or not executable:
        raise RuntimeError("claude-adapter-node-version-invalid")
    if major < 22:
        raise RuntimeError("claude-adapter-node-22-required")
    return executable


def native_npm_command() -> list[str]:
    """On Windows, node and npm's JavaScript entry, found without running npm.cmd.

    The node.exe on PATH may be a version manager's shim, so Node reports its own executable;
    npm-cli.js ships beside it.
    """
    node = shutil.which("node.exe")
    if not node:
        raise RuntimeError("claude-adapter-node-and-npm-required")
    try:
        probe = subprocess.run([node, "-e", NODE_PROBE], capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("claude-adapter-node-unavailable") from exc
    try:
        node_path = Path(node_executable(probe.returncode, probe.stdout)).resolve(strict=True)
    except OSError as exc:
        raise RuntimeError("claude-adapter-node-unavailable") from exc
    npm_cli = node_path.parent / "node_modules" / "npm" / "bin" / "npm-cli.js"
    if not npm_cli.is_file():
        raise RuntimeError("claude-adapter-npm-cli-missing")
    return [str(node_path), str(npm_cli.resolve())]


def npm_command(*, windows: bool | None = None) -> list[str]:
    windows = is_windows() if windows is None else windows
    return native_npm_command() if windows else ["npm"]


def install(target: Path, *, package_root: Path = PACKAGE_ROOT) -> None:
    """Place the pinned package in `target`, patch it and verify it. The caller holds the lock."""
    target = Path(os.path.abspath(target))
    assert_no_reparse(target, target)
    target.mkdir(parents=True, exist_ok=True)
    assert_no_reparse(target, target)
    for name in PACKAGE_FILES:
        source, destination = package_root / name, target / name
        assert_no_reparse(source, package_root)
        assert_no_reparse(destination, target)
        if _is_reparse_point(destination):
            raise RuntimeError("claude-adapter-manifest-reparse-target")
        shutil.copyfile(source, destination)
    subprocess.run([*npm_command(), "ci", "--omit=dev", "--prefix", str(target)], check=True,
                   timeout=INSTALL_TIMEOUT_SECONDS)
    apply_bridge_patch(target)
    verify(target)
