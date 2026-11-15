"""What an entry (CLI, MCP) does: hand the engine a request and read its records.

An entry holds no progress, judgment or recording logic. It starts the engine
process when none is running, always with the same fixed command.
"""
import os
import subprocess
import sys
import time
import uuid

from . import store
from .platform_facts import CURRENT

ENGINE_MAIN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
POLL_SECONDS = 0.1
RESPAWN_SECONDS = 2.0


_started = []


class EngineUnavailable(RuntimeError):
    pass


def lock_path(root):
    return os.path.join(root, "engine.lock")


def engine_command():
    """The one command that starts the engine: this installation's interpreter and engine main."""
    return [sys.executable, "-I", ENGINE_MAIN, "serve"]


def ensure_engine(root, environ):
    """Start the engine process unless one already holds the state home; return what was started."""
    _started[:] = [process for process in _started if process.poll() is None]  # reap engines that exited
    probe = store.ProcessLock(lock_path(root))
    try:
        if not probe.acquire():
            return None
    except store.LockFailed as error:
        raise EngineUnavailable(f"the engine's state home cannot be locked: {error}") from error
    probe.release()
    process = subprocess.Popen(
        engine_command(), env=dict(environ), cwd=os.path.dirname(ENGINE_MAIN),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        **CURRENT.detached,
    )
    _started.append(process)
    return process


def request(root, payload, *, environ, timeout=30.0):
    """Send one request and return the engine's response."""
    request_id = uuid.uuid4().hex
    store.write_request(root, request_id, payload)
    response = store.response_path(root, request_id)
    deadline = time.monotonic() + timeout
    checked = 0.0
    while time.monotonic() < deadline:
        value = store.read_json(response)
        if value is not None:
            return value
        if time.monotonic() - checked >= RESPAWN_SECONDS:
            ensure_engine(root, environ)
            checked = time.monotonic()
        time.sleep(POLL_SECONDS)
    raise EngineUnavailable("the engine did not answer in time")


def status(root, unit_id):
    return store.read_json(os.path.join(store.unit_dir(root, unit_id), "status.json"))


def wait(root, unit_id, *, after_revision=None, timeout=30.0, environ=None):
    """The unit's status once it stopped or moved past `after_revision`, else its current status."""
    deadline = time.monotonic() + timeout
    checked = 0.0
    while True:
        current = status(root, unit_id)
        if current is not None and (current.get("state") == "stopped" or (
                after_revision is not None and current.get("revision", 0) > after_revision)):
            return current
        if time.monotonic() >= deadline:
            return current
        if environ is not None and time.monotonic() - checked >= RESPAWN_SECONDS:
            ensure_engine(root, environ)  # a running unit needs an engine to advance
            checked = time.monotonic()
        time.sleep(POLL_SECONDS)


def result(root, unit_id, name):
    """The bytes of one result file of a unit, or None."""
    if name not in ("patch.diff", "stop.json"):
        raise ValueError("unknown result")
    try:
        with open(os.path.join(store.unit_dir(root, unit_id), "result", name), "rb") as handle:
            return handle.read()
    except OSError:
        return None
