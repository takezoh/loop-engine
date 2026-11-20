"""Process entry, run as a script with `python -I`: `serve` is the engine, `mcp` the MCP entry for the
session connected at the current directory, anything else the CLI.

With -I nothing from the current directory or the environment reaches the
import path, so the engine imports only this installation's own packages.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# The engine and the provider transport are the two packages of this directory.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(HERE))


def serve():
    from loop_engine import composition
    engine, lock_path = composition.build(os.environ)
    engine.serve(lock_path)
    return 0


def mcp():
    from loop_engine import composition, mcp_stdio
    try:
        tools = composition.unit_entry(os.environ, os.getcwd())
    except PermissionError as error:
        print(str(error), file=sys.stderr)
        return 2
    mcp_stdio.serve(tools)
    return 0


def main(argv):
    if argv == ["serve"]:
        return serve()
    if argv == ["mcp"]:
        return mcp()
    from loop_engine import cli
    return cli.main(argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
