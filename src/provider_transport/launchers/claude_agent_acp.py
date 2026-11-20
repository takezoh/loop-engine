"""Start the Claude ACP adapter that the engine's install placed in the transport's runtime directory.

The adapter starts only after `claude_adapter.verify` confirms the pinned, patched package. node
starts the package's entry script directly, so neither the ``.bin`` link nor a Windows ``.cmd``
shim is involved.

The catalog gives the launcher the model the session will bind (``--model <id>``). The adapter
offers only its model picker and resolves any other value to the nearest entry, so the launcher
adds the exact id to that picker as Claude Code's custom model option. The session then binds,
and reads back, the very id that was asked for.
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
MALFORMED = 2
CUSTOM_MODEL_OPTION = "ANTHROPIC_CUSTOM_MODEL_OPTION"


def launch_options(arguments: list[str]) -> tuple[str | None, list[str]]:
    """The model the catalog gave (``--model <id>``, at most once) and the adapter's own arguments."""
    model = None
    rest: list[str] = []
    index = 0
    while index < len(arguments):
        if arguments[index] == "--model":
            if model is not None or index + 1 >= len(arguments) or not arguments[index + 1].strip():
                raise ValueError("claude-launcher-model-malformed")
            model = arguments[index + 1]
            index += 2
            continue
        rest.append(arguments[index])
        index += 1
    return model, rest


def provider_environment(environ: dict[str, str], model: str | None) -> dict[str, str]:
    """The adapter's environment: the caller's, with the bound model offered as the custom option."""
    if model is None:
        return dict(environ)
    return {**environ, CUSTOM_MODEL_OPTION: model}


def provider_command(runtime: Path, node: str, arguments: list[str]) -> list[str]:
    """The adapter's argv: node and the package's entry script in ``runtime``."""
    return [node, str(runtime / claude_adapter.ENTRY_RELATIVE_PATH), *arguments]


def main(arguments: list[str]) -> int:
    try:
        model, arguments = launch_options(arguments)
    except ValueError as exc:
        print(f"the Claude ACP adapter launch is malformed: {exc}", file=sys.stderr)
        return MALFORMED
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
    environment = provider_environment(dict(os.environ), model)
    if not windows:
        os.execve(node, command, environment)
    return subprocess.run(command, check=False, env=environment).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
