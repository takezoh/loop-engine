"""Fakes of the Win32 process and filesystem APIs the Windows adapters call."""
import ntpath

from provider_transport.windows_filesystem import (
    FILE_ATTRIBUTE_DIRECTORY,
    FILE_ATTRIBUTE_REPARSE_POINT,
    DirectoryEntry,
    FilesystemAuthorityError,
)
from provider_transport.windows_process import (
    KILL_ON_JOB_CLOSE,
    WindowsProcessError,
)


class FakeProcess:
    def __init__(self, pid=4001):
        self.pid = pid
        self.process_handle = f"process:{pid}"
        self.thread_handle = f"thread:{pid}"
        self.resumed = False
        self.terminated = False


class FakeProcessApi:
    def __init__(self, *, member=True, identity_ok=True):
        self.member = member
        self.identity_ok = identity_ok
        self.events = []
        self.flags = {}
        self.active = {}
        self.closed = []
        self.process = FakeProcess()
        self.launches = 0
        self.fail_launch = False
        self.termination_sticks = False

    def create_job(self, name=None):
        job = f"job:{len(self.flags) + 1}"
        self.flags[job] = 0
        self.active[job] = 0
        self.events.append(("create-job", job))
        return job

    def open_job(self, name):
        return name if name in self.flags else None

    def configure_job(self, job):
        self.flags[job] = KILL_ON_JOB_CLOSE
        self.events.append(("configure-job", job, self.flags[job]))

    def job_limit_flags(self, job):
        return self.flags[job]

    def active_processes(self, job):
        return self.active[job]

    def terminate_job(self, job, exit_code):
        self.events.append(("terminate-job", job, exit_code))
        if not self.termination_sticks:
            self.active[job] = 0

    def close_handle(self, handle):
        self.events.append(("close-handle", handle))
        self.closed.append(handle)
        if handle in self.active and self.flags[handle] & KILL_ON_JOB_CLOSE:
            self.active[handle] = 0

    def launch_suspended(self, job, command, *, options):
        # The provider models PROC_THREAD_ATTRIBUTE_JOB_LIST: membership is
        # established by CreateProcess before this call returns.
        self.events.append(("create-suspended-in-job", job, command, dict(options)))
        if self.fail_launch:
            raise WindowsProcessError("fake-process-create-failed")
        if self.launches:
            self.process = FakeProcess(pid=4001 + self.launches)
        self.launches += 1
        self.active[job] += 1
        self.process.job = job
        self.process.in_job = self.member
        return self.process

    def is_process_in_job(self, job, process):
        self.events.append(("confirm-membership", job, process.process_handle))
        return process.in_job and process.job == job

    def process_identity(self, process):
        self.events.append(("identity", process.process_handle))
        if not self.identity_ok:
            raise WindowsProcessError("fake-identity-unavailable")
        return f"windows:{process.pid}:987654"

    def resume_process(self, process):
        self.events.append(("resume", process.thread_handle))
        if not process.in_job:
            raise AssertionError("resume-before-membership")
        process.resumed = True
        process.thread_handle = None

    def process_pid(self, process):
        return process.pid

    def close_process(self, process):
        self.events.append(("close-process", process.process_handle))
        self.closed.append(process.process_handle)

    def terminate_process(self, process, exit_code):
        self.events.append(("terminate-owned-process", process.process_handle, exit_code))
        process.terminated = True
        process.thread_handle = None
        if not self.termination_sticks:
            self.active[getattr(process, "job", "job:1")] = 0


class FakeFilesystemApi:
    def __init__(self, *, swapped=()):
        self.swapped = set(swapped)
        self.opened = []
        self.closed = []
        self.rows = {
            "volume": (
                DirectoryEntry("work", (1, 10), FILE_ATTRIBUTE_DIRECTORY, 0, 1),
            ),
            "work": (
                DirectoryEntry("child", (1, 20), FILE_ATTRIBUTE_DIRECTORY, 0, 2),
                DirectoryEntry("payload.json", (1, 30), 0, 7, 3),
                DirectoryEntry(
                    "junction", (1, 40),
                    FILE_ATTRIBUTE_DIRECTORY | FILE_ATTRIBUTE_REPARSE_POINT, 0, 4,
                ),
            ),
            "child": (),
            "temp": (
                DirectoryEntry("scratch", (1, 60), FILE_ATTRIBUTE_DIRECTORY, 0, 5),
            ),
            "scratch": (),
        }
        self.ids = {"volume": (1, 1), "work": (1, 10), "child": (1, 20), "temp": (1, 50)}
        self.paths = {
            "volume": "C:\\", "work": "C:\\work", "child": "C:\\work\\child",
            "temp": "C:\\Temp",
        }

    def open_volume_root(self, path):
        self.opened.append(("volume", path))
        return "volume"

    def open_anchor(self, path):
        self.opened.append(("anchor", path))
        handle = next((name for name, value in self.paths.items() if value == path), None)
        if handle is None:
            raise FilesystemAuthorityError("open-failed")
        return handle

    def entries(self, handle):
        return self.rows.get(handle, ())

    def open_child(self, parent, name, *, expected, directory):
        if name in self.swapped:
            raise FilesystemAuthorityError("alias-changed")
        handle = name
        current = next((row for row in self.entries(parent) if row.name == name), None)
        if current is None or current.identity != expected.identity:
            raise FilesystemAuthorityError("alias-changed")
        if current.is_reparse or current.is_directory != directory:
            raise FilesystemAuthorityError("entry-type-mismatch")
        self.ids[handle] = current.identity
        self.paths[handle] = ntpath.join(self.paths[parent], name)
        self.opened.append(("child", parent, name))
        return handle

    def identity(self, handle):
        return self.ids[handle]

    def attributes(self, handle):
        if handle == "child":
            return FILE_ATTRIBUTE_DIRECTORY
        return 0

    def final_path(self, handle):
        return self.paths[handle]

    def read_file(self, handle):
        self.opened.append(("read-handle", handle))
        return b'{"ok":true}', (11, 3, 0)

    def close(self, handle):
        self.closed.append(handle)


def any_program(path: str) -> bool:
    """A Windows file test under which every candidate a command search tries exists."""
    return True
