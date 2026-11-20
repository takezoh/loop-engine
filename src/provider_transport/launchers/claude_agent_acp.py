"""Start the Claude ACP adapter of the user's environment.

The launcher starts the ``claude-agent-acp`` on ``PATH`` and names the ``claude`` on ``PATH`` to it as
``CLAUDE_CODE_EXECUTABLE``, so the adapter runs the Claude Code the user runs. On Windows npm puts
the adapter on ``PATH`` as a ``.cmd`` shim, which would run through cmd.exe and read the arguments
again as a command line; the launcher starts the package's entry script beside the shim with the
``node.exe`` on ``PATH`` instead.
"""

from __future__ import annotations

import ntpath
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from provider_transport.host_platform import is_windows  # noqa: E402
from provider_transport.command_search import (  # noqa: E402
    CommandSearch,
    host_command_search,
    is_command_shim,
    resolve_command,
)


NOT_INSTALLED = 127
ADAPTER = "claude-agent-acp"
CLAUDE_CODE_EXECUTABLE = "CLAUDE_CODE_EXECUTABLE"
# Where npm places the package's entry script relative to the directory of its Windows shim.
SHIM_ENTRY = ("node_modules", "@agentclientprotocol", "claude-agent-acp", "dist", "index.js")


def provider_environment(environ: dict[str, str], claude: str) -> dict[str, str]:
    """The adapter's environment: the caller's, naming the Claude Code the adapter runs."""
    return {**environ, CLAUDE_CODE_EXECUTABLE: claude}


def adapter_command(adapter: str, arguments: list[str], search: CommandSearch) -> list[str] | None:
    """The adapter's argv, or None when a Windows shim's entry cannot be started."""
    if not (search.windows and is_command_shim(adapter)):
        return [adapter, *arguments]
    entry = ntpath.join(ntpath.dirname(adapter), *SHIM_ENTRY)
    node = resolve_command("node", search)
    if node is None or is_command_shim(node) or not search.runnable(entry):
        return None
    return [node, entry, *arguments]


def main(arguments: list[str]) -> int:
    windows = is_windows()
    search = host_command_search()
    claude = resolve_command("claude", search)
    if claude is None or (windows and is_command_shim(claude)):
        # A batch shim would run through cmd.exe, which reads the arguments again as a command line.
        print(f"the Claude ACP adapter needs a claude executable on PATH: {claude}", file=sys.stderr)
        return NOT_INSTALLED
    adapter = resolve_command(ADAPTER, search)
    if adapter is None:
        print(f"the {ADAPTER} of the environment is not on PATH", file=sys.stderr)
        return NOT_INSTALLED
    command = adapter_command(adapter, arguments, search)
    if command is None:
        print(f"the {ADAPTER} shim needs its package entry and a node executable on PATH: {adapter}",
              file=sys.stderr)
        return NOT_INSTALLED
    environment = provider_environment(dict(os.environ), claude)
    if not windows:
        os.execve(command[0], command, environment)
    return subprocess.run(command, check=False, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
