"""A process cell over this process's direct children, held to the cell port contract.

It starts real processes, each the leader of its own session and process
group, and accounts only for the roots it started or a test adopted; it has no
kernel cell, so it cannot see a descendant that leaves its root's group.
"""
import os
import signal
import subprocess
import time


class FakeCellMembership:
    def __init__(self):
        self.roots = {}
        self._processes = {}
        self.closed = False

    def spawn(self, command, *, cwd, options):
        if self.closed:
            raise RuntimeError("subsession-process-cell-closed")
        process = subprocess.Popen(command, **{**dict(options), "cwd": cwd, "start_new_session": True})
        self.roots[process.pid] = f"fake:{process.pid}"
        self._processes[process.pid] = process
        return process

    def adopt(self, pid, identity):
        """Own a root this cell did not start, with the identity the test chooses (None: unobservable)."""
        self.roots[pid] = identity

    def confirm(self, pid):
        return pid in self.roots

    def process_identity(self, pid):
        return self.roots.get(pid) if self.process_exists(pid) else None

    def process_exists(self, pid):
        process = self._processes.get(pid)
        if process is not None:
            return process.poll() is None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def release_certain(self, pid, identity):
        if pid is not None and self.process_exists(pid):
            return False
        self.roots.pop(pid, None)
        self._processes.pop(pid, None)
        return True

    def terminate_roots(self, roots):
        for pid, identity in roots:
            if identity is not None and self.roots.get(pid) == identity:
                self._kill(pid)

    def terminate_root(self, pid):
        if pid not in self.roots:
            raise RuntimeError("fake-cell-root-unavailable")
        self._kill(pid)

    def terminate_all(self):
        for pid in tuple(self.roots):
            self._kill(pid)

    def resolve_unconfirmed_empty(self, timeout):
        return True

    def wait_empty(self, timeout):
        deadline = time.monotonic() + timeout
        while any(self.process_exists(pid) for pid in tuple(self.roots)):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def close(self):
        self.closed = True

    def _kill(self, pid):
        """Signal a root's group only while its process is unreaped, so its PID is still its own."""
        process = self._processes.get(pid)
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
