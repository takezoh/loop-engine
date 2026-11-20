"""Start the Claude ACP adapter that the engine's install placed in the transport's runtime directory.

The adapter starts only after `claude_adapter.verify` confirms the pinned, patched package. node
starts the package's entry script directly, so neither the ``.bin`` link nor a Windows ``.cmd``
shim is involved.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from provider_transport import claude_adapter  # noqa: E402
from provider_transport.host_platform import is_windows  # noqa: E402
from provider_transport.command_search import (  # noqa: E402
    host_command_search,
    is_command_shim,
    resolve_command,
)


NOT_INSTALLED = 127


def provider_command(runtime: Path, node: str, arguments: list[str]) -> list[str]:
    """The adapter's argv: node and the package's entry script in ``runtime``."""
    return [node, str(runtime / claude_adapter.ENTRY_RELATIVE_PATH), *arguments]


def main(arguments: list[str]) -> int:
    windows = is_windows()
    try:
        runtime = claude_adapter.runtime_dir(windows=windows)
        claude_adapter.verify(runtime)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"the Claude ACP adapter is not installed: {exc}", file=sys.stderr)
        return NOT_INSTALLED
    node = resolve_command("node", host_command_search())
    if node is None or (windows and is_command_shim(node)):
        # A batch shim would run through cmd.exe, which re-reads the arguments as a command line.
        print(f"the Claude ACP adapter needs a node executable on PATH: {node}", file=sys.stderr)
        return NOT_INSTALLED
    command = provider_command(runtime, node, arguments)
    if not windows:
        os.execv(node, command)
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
