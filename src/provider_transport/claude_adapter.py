"""Install and verify the Claude ACP adapter the `claude` provider starts.

The adapter is an npm package. It lives in the transport's runtime directory, apart from every
caller's files. `install` places its latest release there without its optional dependencies, the
Claude Code binaries its SDK bundles, because the launcher has the adapter run the user's `claude`.
`verify` confirms that the package is there; the launcher verifies before every start. The lock that
keeps two installs apart belongs to the caller of `install`.

What is decided is decided from values: where the adapter lives (`runtime_dir`), whether what is
installed is the adapter (`verification_failure`) and which Node may run npm
(`node_executable`). The functions that read files and start processes only gather those values and
act on the answers.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping

from .host_platform import is_windows

PACKAGE = "@agentclientprotocol/claude-agent-acp"
# The package's "bin" entry; node starts it directly, without the .bin link or .cmd shim.
ENTRY_RELATIVE_PATH = Path("node_modules/@agentclientprotocol/claude-agent-acp/dist/index.js")
METADATA_RELATIVE_PATH = Path("node_modules/@agentclientprotocol/claude-agent-acp/package.json")
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


def verification_failure(target: object, *, metadata: object, has_entry: bool) -> str | None:
    """Why what was read from `target` is not the adapter, or None when it is.

    `metadata` is the installed package's parsed `package.json`, or None when there is none.
    """
    if not isinstance(metadata, dict) or not has_entry:
        return f"claude-adapter-missing:{target}"
    if metadata.get("name") != PACKAGE:
        return "claude-adapter-package-mismatch"
    return None


def verify(target: Path) -> None:
    """Raise unless `target` holds the adapter package with its entry script."""
    metadata = target / METADATA_RELATIVE_PATH
    failure = verification_failure(
        target,
        metadata=json.loads(metadata.read_text(encoding="utf-8")) if metadata.is_file() else None,
        has_entry=(target / ENTRY_RELATIVE_PATH).is_file(),
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


def install(target: Path) -> None:
    """Place the adapter's latest release in `target` and verify it. The caller holds the lock."""
    target = Path(os.path.abspath(target))
    assert_no_reparse(target, target)
    target.mkdir(parents=True, exist_ok=True)
    assert_no_reparse(target, target)
    # The optional dependencies are the Claude Code binaries the SDK bundles; the launcher runs the
    # user's claude instead, so they are not installed.
    subprocess.run([*npm_command(), "install", "--prefix", str(target), "--omit=dev", "--omit=optional",
                    f"{PACKAGE}@latest"], check=True, timeout=INSTALL_TIMEOUT_SECONDS)
    verify(target)
