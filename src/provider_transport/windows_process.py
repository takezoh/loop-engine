"""Kernel-owned Windows process cells backed by Job Objects.

The module is the Windows process effect adapter for the subsession runtime.
Callers reach ``WindowsJobCell`` through the ``CellMembership`` port; only this
adapter owns Win32 process, thread, and Job Object handles.
"""

from __future__ import annotations

from dataclasses import dataclass
import io
import math
import ntpath
import os
import subprocess
import time
from threading import RLock
from typing import Callable, Iterable, Mapping, Protocol

from .command_search import is_command_shim, resolve_command, search_values


KILL_ON_JOB_CLOSE = 0x00002000
BREAKAWAY_OK = 0x00000800
SILENT_BREAKAWAY_OK = 0x00001000
CREATE_SUSPENDED = 0x00000004
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
PROC_THREAD_ATTRIBUTE_JOB_LIST = 0x0002000D
HANDLE_FLAG_INHERIT = 0x00000001
DUPLICATE_SAME_ACCESS = 0x00000002
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
WAIT_FAILED = 0xFFFFFFFF
INFINITE = 0xFFFFFFFF
STD_INPUT_HANDLE = -10
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080


class WindowsProcessError(RuntimeError):
    """The native process/job barrier could not be established or proven."""


class WindowsProcessApi(Protocol):
    def create_job(self, name: str | None = None) -> object: ...
    def open_job(self, name: str) -> object | None: ...
    def configure_job(self, job: object) -> None: ...
    def job_limit_flags(self, job: object) -> int: ...
    def active_processes(self, job: object) -> int: ...
    def terminate_job(self, job: object, exit_code: int) -> None: ...
    def close_handle(self, handle: object) -> None: ...
    def launch_suspended(
        self, job: object, command: object, *, options: Mapping[str, object]
    ) -> object: ...
    def is_process_in_job(self, job: object, process: object) -> bool: ...
    def process_identity(self, process: object) -> str: ...
    def resume_process(self, process: object) -> None: ...
    def process_pid(self, process: object) -> int: ...
    def close_process(self, process: object) -> None: ...
    def terminate_process(self, process: object, exit_code: int) -> None: ...


class WindowsNativeProcess:
    """Popen-compatible result retaining native process and primary-thread handles."""

    def __init__(
        self,
        api: "_Win32ProcessApi",
        *,
        process_handle: object,
        thread_handle: object,
        pid: int,
        thread_id: int,
        args: object,
        stdin: object = None,
        stdout: object = None,
        stderr: object = None,
    ) -> None:
        self._api = api
        import ctypes
        from ctypes import wintypes
        self._ctypes = ctypes
        self._wintypes = wintypes
        self.process_handle = process_handle
        self.thread_handle = thread_handle
        self.pid = pid
        self.thread_id = thread_id
        self.args = args
        self.stdin = stdin
        self.stdout = stdout
        self.stderr = stderr
        self.returncode: int | None = None
        self._terminate_job: Callable[[], None] | None = None

    def bind_job(self, job: "WindowsJob") -> None:
        self._terminate_job = job.terminate

    def poll(self) -> int | None:
        if self.returncode is not None:
            return self.returncode
        result = int(self._api.kernel32.WaitForSingleObject(self.process_handle, 0))
        if result == WAIT_TIMEOUT:
            return None
        if result == WAIT_FAILED:
            raise _last_error("windows-process-poll-failed")
        if result != WAIT_OBJECT_0:
            raise WindowsProcessError("windows-process-wait-state-invalid")
        code = self._wintypes.DWORD()
        if not self._api.kernel32.GetExitCodeProcess(
            self.process_handle, self._ctypes.byref(code)
        ):
            raise _last_error("windows-process-exit-code-unavailable")
        self.returncode = int(code.value)
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is not None:
            return self.returncode
        milliseconds = INFINITE if timeout is None else max(0, min(
            INFINITE - 1, math.ceil(timeout * 1000)
        ))
        result = int(self._api.kernel32.WaitForSingleObject(
            self.process_handle, milliseconds
        ))
        if result == WAIT_TIMEOUT:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if result == WAIT_FAILED:
            raise _last_error("windows-process-wait-failed")
        if result != WAIT_OBJECT_0:
            raise WindowsProcessError("windows-process-wait-state-invalid")
        code = self._wintypes.DWORD()
        if not self._api.kernel32.GetExitCodeProcess(
            self.process_handle, self._ctypes.byref(code)
        ):
            raise _last_error("windows-process-exit-code-unavailable")
        self.returncode = int(code.value)
        return self.returncode

    def terminate(self) -> None:
        if self._terminate_job is not None:
            self._terminate_job()
            return
        if self.poll() is None and not self._api.kernel32.TerminateProcess(
            self.process_handle, 1
        ):
            raise _last_error("windows-process-termination-failed")

    kill = terminate

    def terminate_native(self, exit_code: int = 1) -> None:
        if self.process_handle and not self._api.kernel32.TerminateProcess(
            self.process_handle, exit_code
        ):
            # ERROR_ACCESS_DENIED may mean the process exited between poll and
            # termination; query the same handle before treating it as failure.
            if self.poll() is None:
                raise _last_error("windows-process-termination-failed")

    def close_native_handles(self) -> None:
        if self.thread_handle:
            self._api.close_handle(self.thread_handle)
            self.thread_handle = None
        if self.process_handle:
            self._api.close_handle(self.process_handle)
            self.process_handle = None

    def __del__(self) -> None:
        try:
            self.close_native_handles()
        except BaseException:
            pass


class _Win32ProcessApi:
    """Narrow ctypes adapter for Job Objects and suspended native process launch."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise WindowsProcessError("windows-process-api-unavailable")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._structures()
        self._configure_signatures()

    def _structures(self) -> None:
        c, w = self.ctypes, self.wintypes

        class BASIC_LIMITS(c.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", c.c_longlong),
                ("PerJobUserTimeLimit", c.c_longlong),
                ("LimitFlags", w.DWORD),
                ("MinimumWorkingSetSize", c.c_size_t),
                ("MaximumWorkingSetSize", c.c_size_t),
                ("ActiveProcessLimit", w.DWORD),
                ("Affinity", c.c_size_t),
                ("PriorityClass", w.DWORD),
                ("SchedulingClass", w.DWORD),
            ]

        class IO_COUNTERS(c.Structure):
            _fields_ = [(name, c.c_ulonglong) for name in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )]

        class EXTENDED_LIMITS(c.Structure):
            _fields_ = [
                ("BasicLimitInformation", BASIC_LIMITS),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", c.c_size_t),
                ("JobMemoryLimit", c.c_size_t),
                ("PeakProcessMemoryUsed", c.c_size_t),
                ("PeakJobMemoryUsed", c.c_size_t),
            ]

        class STARTUPINFO(c.Structure):
            _fields_ = [
                ("cb", w.DWORD), ("lpReserved", w.LPWSTR),
                ("lpDesktop", w.LPWSTR), ("lpTitle", w.LPWSTR),
                ("dwX", w.DWORD), ("dwY", w.DWORD),
                ("dwXSize", w.DWORD), ("dwYSize", w.DWORD),
                ("dwXCountChars", w.DWORD), ("dwYCountChars", w.DWORD),
                ("dwFillAttribute", w.DWORD), ("dwFlags", w.DWORD),
                ("wShowWindow", w.WORD), ("cbReserved2", w.WORD),
                ("lpReserved2", c.POINTER(w.BYTE)),
                ("hStdInput", w.HANDLE), ("hStdOutput", w.HANDLE),
                ("hStdError", w.HANDLE),
            ]

        class STARTUPINFOEX(c.Structure):
            _fields_ = [
                ("StartupInfo", STARTUPINFO),
                ("lpAttributeList", c.c_void_p),
            ]

        class PROCESS_INFORMATION(c.Structure):
            _fields_ = [
                ("hProcess", w.HANDLE), ("hThread", w.HANDLE),
                ("dwProcessId", w.DWORD), ("dwThreadId", w.DWORD),
            ]

        class SECURITY_ATTRIBUTES(c.Structure):
            _fields_ = [
                ("nLength", w.DWORD), ("lpSecurityDescriptor", c.c_void_p),
                ("bInheritHandle", w.BOOL),
            ]

        self.BASIC_LIMITS = BASIC_LIMITS
        self.EXTENDED_LIMITS = EXTENDED_LIMITS
        self.STARTUPINFOEX = STARTUPINFOEX
        self.PROCESS_INFORMATION = PROCESS_INFORMATION
        self.SECURITY_ATTRIBUTES = SECURITY_ATTRIBUTES

    def _configure_signatures(self) -> None:
        w = self.wintypes
        k = self.kernel32
        k.CreateJobObjectW.argtypes = [ctypes_pointer(w), w.LPCWSTR]
        k.CreateJobObjectW.restype = w.HANDLE
        k.CreateMutexW.argtypes = [ctypes_pointer(w), w.BOOL, w.LPCWSTR]
        k.CreateMutexW.restype = w.HANDLE
        k.ReleaseMutex.argtypes = [w.HANDLE]
        k.ReleaseMutex.restype = w.BOOL
        k.OpenJobObjectW.argtypes = [w.DWORD, w.BOOL, w.LPCWSTR]
        k.OpenJobObjectW.restype = w.HANDLE
        k.SetInformationJobObject.argtypes = [w.HANDLE, w.INT, ctypes_pointer(w), w.DWORD]
        k.SetInformationJobObject.restype = w.BOOL
        k.QueryInformationJobObject.argtypes = [w.HANDLE, w.INT, ctypes_pointer(w), w.DWORD, ctypes_pointer(w)]
        k.QueryInformationJobObject.restype = w.BOOL
        k.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        k.TerminateJobObject.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.IsProcessInJob.argtypes = [w.HANDLE, w.HANDLE, ctypes_pointer(w.BOOL)]
        k.IsProcessInJob.restype = w.BOOL
        k.GetProcessTimes.argtypes = [w.HANDLE, ctypes_pointer(w.FILETIME), ctypes_pointer(w.FILETIME), ctypes_pointer(w.FILETIME), ctypes_pointer(w.FILETIME)]
        k.GetProcessTimes.restype = w.BOOL
        k.GetProcessId.argtypes = [w.HANDLE]
        k.GetProcessId.restype = w.DWORD
        k.ResumeThread.argtypes = [w.HANDLE]
        k.ResumeThread.restype = w.DWORD
        k.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        k.TerminateProcess.restype = w.BOOL
        k.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        k.WaitForSingleObject.restype = w.DWORD
        k.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes_pointer(w.DWORD)]
        k.GetExitCodeProcess.restype = w.BOOL
        k.InitializeProcThreadAttributeList.argtypes = [ctypes_pointer(w.BYTE), w.DWORD, w.DWORD, ctypes_pointer(self.ctypes.c_size_t)]
        k.InitializeProcThreadAttributeList.restype = w.BOOL
        k.UpdateProcThreadAttribute.argtypes = [ctypes_pointer(w.BYTE), w.DWORD, self.ctypes.c_size_t, ctypes_pointer(w.BYTE), self.ctypes.c_size_t, ctypes_pointer(w.BYTE), ctypes_pointer(self.ctypes.c_size_t)]
        k.UpdateProcThreadAttribute.restype = w.BOOL
        k.DeleteProcThreadAttributeList.argtypes = [ctypes_pointer(w.BYTE)]
        k.DeleteProcThreadAttributeList.restype = None
        k.CreateProcessW.argtypes = [w.LPCWSTR, w.LPWSTR, ctypes_pointer(w.BYTE), ctypes_pointer(w.BYTE), w.BOOL, w.DWORD, ctypes_pointer(w.BYTE), w.LPCWSTR, ctypes_pointer(self.STARTUPINFOEX), ctypes_pointer(self.PROCESS_INFORMATION)]
        k.CreateProcessW.restype = w.BOOL
        k.CreatePipe.argtypes = [ctypes_pointer(w.HANDLE), ctypes_pointer(w.HANDLE), ctypes_pointer(self.SECURITY_ATTRIBUTES), w.DWORD]
        k.CreatePipe.restype = w.BOOL
        k.SetHandleInformation.argtypes = [w.HANDLE, w.DWORD, w.DWORD]
        k.SetHandleInformation.restype = w.BOOL
        k.DuplicateHandle.argtypes = [w.HANDLE, w.HANDLE, w.HANDLE, ctypes_pointer(w.HANDLE), w.DWORD, w.BOOL, w.DWORD]
        k.DuplicateHandle.restype = w.BOOL
        k.GetCurrentProcess.argtypes = []
        k.GetCurrentProcess.restype = w.HANDLE
        k.GetStdHandle.argtypes = [w.DWORD]
        k.GetStdHandle.restype = w.HANDLE
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, ctypes_pointer(self.SECURITY_ATTRIBUTES), w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE

    def create_job(self, name: str | None = None) -> object:
        security = None
        if name is not None:
            from .windows_security import security_attributes
            security = security_attributes()
        self.ctypes.set_last_error(0)
        try:
            handle = self.kernel32.CreateJobObjectW(
                None if security is None else self.ctypes.byref(security.attributes),
                name,
            )
        finally:
            if security is not None:
                security.close()
        if not handle:
            raise _last_error("job-create-failed")
        if self.ctypes.get_last_error() == 183:
            self.close_handle(handle)
            raise WindowsProcessError("job-name-already-exists")
        return handle

    def open_job(self, name: str) -> object | None:
        handle = self.kernel32.OpenJobObjectW(
            0x0004 | 0x0008, False, name
        )
        if not handle:
            error = self.ctypes.get_last_error()
            if error == 2:
                return None
            raise WindowsProcessError("job-open-failed") from OSError(error, "OpenJobObjectW")
        return handle

    def configure_job(self, job: object) -> None:
        limits = self.EXTENDED_LIMITS()
        limits.BasicLimitInformation.LimitFlags = KILL_ON_JOB_CLOSE
        if not self.kernel32.SetInformationJobObject(
            job, 9, self.ctypes.byref(limits), self.ctypes.sizeof(limits)
        ):
            raise _last_error("job-kill-on-close-unavailable")

    def job_limit_flags(self, job: object) -> int:
        value = self.BASIC_LIMITS()
        if not self.kernel32.QueryInformationJobObject(
            job, 2, self.ctypes.byref(value), self.ctypes.sizeof(value), None
        ):
            raise _last_error("job-policy-query-failed")
        return int(value.LimitFlags)

    def active_processes(self, job: object) -> int:
        class ACCOUNTING(self.ctypes.Structure):
            _fields_ = [
                ("TotalUserTime", self.ctypes.c_longlong),
                ("TotalKernelTime", self.ctypes.c_longlong),
                ("ThisPeriodTotalUserTime", self.ctypes.c_longlong),
                ("ThisPeriodTotalKernelTime", self.ctypes.c_longlong),
                ("TotalPageFaultCount", self.wintypes.DWORD),
                ("TotalProcesses", self.wintypes.DWORD),
                ("ActiveProcesses", self.wintypes.DWORD),
                ("TotalTerminatedProcesses", self.wintypes.DWORD),
            ]

        value = ACCOUNTING()
        if not self.kernel32.QueryInformationJobObject(
            job, 1, self.ctypes.byref(value), self.ctypes.sizeof(value), None
        ):
            raise _last_error("job-empty-query-failed")
        return int(value.ActiveProcesses)

    def terminate_job(self, job: object, exit_code: int) -> None:
        if not self.kernel32.TerminateJobObject(job, exit_code):
            raise _last_error("job-termination-failed")

    def close_handle(self, handle: object) -> None:
        if handle and not self.kernel32.CloseHandle(handle):
            raise _last_error("native-handle-close-failed")

    def launch_suspended(
        self, job: object, command: object, *, options: Mapping[str, object]
    ) -> WindowsNativeProcess:
        """Create suspended with PROC_THREAD_ATTRIBUTE_JOB_LIST assigned atomically."""
        clean = dict(options)
        if clean.pop("pass_fds", ()):
            raise WindowsProcessError("windows-pass-fds-forbidden")
        if clean.pop("preexec_fn", None) is not None:
            raise WindowsProcessError("windows-preexec-forbidden")
        if clean.pop("start_new_session", False):
            raise WindowsProcessError("windows-start-new-session-forbidden")
        if clean.pop("close_fds", True) is False:
            raise WindowsProcessError("windows-inherit-all-handles-forbidden")
        if clean.pop("startupinfo", None) is not None:
            raise WindowsProcessError("windows-custom-startupinfo-forbidden")
        shell = bool(clean.pop("shell", False))
        executable = clean.pop("executable", None)
        flags = clean.pop("creationflags", 0)
        native_flags = creation_flags_for_native_spawn(flags)
        if clean.pop("cwd", None) is not None:
            cwd = os.fsdecode(options["cwd"])
        else:
            cwd = None
        env = clean.pop("env", None)
        stdin_spec = clean.pop("stdin", None)
        stdout_spec = clean.pop("stdout", None)
        stderr_spec = clean.pop("stderr", None)
        text_mode = bool(clean.pop("text", clean.pop("universal_newlines", False)))
        encoding = clean.pop("encoding", None) or "utf-8"
        errors = clean.pop("errors", None) or "strict"
        buffering = clean.pop("bufsize", -1)
        if clean:
            raise WindowsProcessError("windows-spawn-option-unsupported")
        if not isinstance(buffering, int) or buffering < -1:
            raise WindowsProcessError("windows-buffer-size-invalid")
        if text_mode and buffering == 0:
            raise WindowsProcessError("windows-text-unbuffered-forbidden")

        if isinstance(command, str):
            command_line = command
        elif isinstance(command, (tuple, list)):
            args = [os.fsdecode(value) for value in command]
            if not args:
                raise WindowsProcessError("windows-command-empty")
            command_line = subprocess.list2cmdline(args)
        else:
            command_line = subprocess.list2cmdline([os.fsdecode(command)])
        if shell:
            shell_path = os.environ.get("ComSpec") or os.path.join(
                os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe"
            )
            if not ntpath.isabs(shell_path):
                raise WindowsProcessError("windows-command-interpreter-unavailable")
            executable = shell_path
            command_line = f'{shell_path} /c "{command_line}"'
        if not command_line or "\x00" in command_line:
            raise WindowsProcessError("windows-command-invalid")
        if executable is not None:
            executable = os.fsdecode(executable)
            if not executable or "\x00" in executable:
                raise WindowsProcessError("windows-executable-invalid")

        env_buffer = self._environment_block(env)
        pi = self.PROCESS_INFORMATION()
        attribute_storage = None
        attributes_ready = False
        temporary_handles: list[object] = []
        parent_pipe_handles: dict[str, object] = {}
        streams: dict[str, object] = {"stdin": None, "stdout": None, "stderr": None}
        process: WindowsNativeProcess | None = None
        created = False
        try:
            std_handles: dict[str, object] = {}
            stdin_child, stdin_parent = self._stdio_handle(
                stdin_spec, direction="stdin", stdout_child=None
            )
            if stdin_parent is not None:
                parent_pipe_handles["stdin"] = stdin_parent
            if stdin_spec == subprocess.PIPE:
                temporary_handles.append(stdin_child)
            std_handles["stdin"] = stdin_child

            stdout_child, stdout_parent = self._stdio_handle(
                stdout_spec, direction="stdout", stdout_child=None
            )
            if stdout_parent is not None:
                parent_pipe_handles["stdout"] = stdout_parent
            if stdout_spec == subprocess.PIPE:
                temporary_handles.append(stdout_child)
            std_handles["stdout"] = stdout_child

            if stderr_spec == subprocess.STDOUT:
                stderr_child, stderr_parent = stdout_child, None
            else:
                stderr_child, stderr_parent = self._stdio_handle(
                    stderr_spec, direction="stderr", stdout_child=stdout_child
                )
                if stderr_parent is not None:
                    parent_pipe_handles["stderr"] = stderr_parent
                if stderr_spec == subprocess.PIPE:
                    temporary_handles.append(stderr_child)
            std_handles["stderr"] = stderr_child

            for handle in std_handles.values():
                if handle not in temporary_handles and handle not in parent_pipe_handles.values():
                    temporary_handles.append(handle)

            child_list = list(dict.fromkeys(self._handle_int(h) for h in std_handles.values()))
            job_list = (self.wintypes.HANDLE * 1)(self._handle_int(job))
            inherited_list = (self.wintypes.HANDLE * len(child_list))(*child_list)
            required = self.ctypes.c_size_t()
            self.kernel32.InitializeProcThreadAttributeList(
                None, 2, 0, self.ctypes.byref(required)
            )
            if not required.value:
                raise _last_error("windows-process-attributes-unavailable")
            attribute_storage = self.ctypes.create_string_buffer(required.value)
            attribute_pointer = self.ctypes.cast(
                attribute_storage, self.ctypes.POINTER(self.wintypes.BYTE)
            )
            if not self.kernel32.InitializeProcThreadAttributeList(
                attribute_pointer, 2, 0, self.ctypes.byref(required)
            ):
                raise _last_error("windows-process-attributes-unavailable")
            attributes_ready = True
            if not self.kernel32.UpdateProcThreadAttribute(
                attribute_pointer, 0, PROC_THREAD_ATTRIBUTE_JOB_LIST,
                self.ctypes.cast(job_list, self.ctypes.POINTER(self.wintypes.BYTE)),
                self.ctypes.sizeof(job_list), None, None,
            ):
                raise _last_error("windows-job-list-attribute-unavailable")
            if not self.kernel32.UpdateProcThreadAttribute(
                attribute_pointer, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                self.ctypes.cast(inherited_list, self.ctypes.POINTER(self.wintypes.BYTE)),
                self.ctypes.sizeof(inherited_list), None, None,
            ):
                raise _last_error("windows-handle-list-attribute-unavailable")

            startup = self.STARTUPINFOEX()
            startup.StartupInfo.cb = self.ctypes.sizeof(self.STARTUPINFOEX)
            startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = self.wintypes.HANDLE(
                self._handle_int(std_handles["stdin"])
            )
            startup.StartupInfo.hStdOutput = self.wintypes.HANDLE(
                self._handle_int(std_handles["stdout"])
            )
            startup.StartupInfo.hStdError = self.wintypes.HANDLE(
                self._handle_int(std_handles["stderr"])
            )
            startup.lpAttributeList = self.ctypes.cast(attribute_pointer, self.ctypes.c_void_p)
            mutable_command = self.ctypes.create_unicode_buffer(command_line)
            environment_pointer = (
                self.ctypes.cast(env_buffer, self.ctypes.POINTER(self.wintypes.BYTE))
                if env_buffer is not None else None
            )
            if not self.kernel32.CreateProcessW(
                executable, mutable_command, None, None, True,
                native_flags,
                environment_pointer, cwd, self.ctypes.byref(startup), self.ctypes.byref(pi),
            ):
                raise _last_error("windows-process-create-failed")
            created = True
            process = WindowsNativeProcess(
                self,
                process_handle=self._handle_value(pi.hProcess),
                thread_handle=self._handle_value(pi.hThread),
                pid=int(pi.dwProcessId), thread_id=int(pi.dwThreadId),
                args=command,
            )
            pi.hProcess = None
            pi.hThread = None

            for name, handle in parent_pipe_handles.items():
                streams[name] = self._stream_for_handle(
                    handle, name=name, text_mode=text_mode, encoding=encoding,
                    errors=errors, buffering=buffering,
                )
                parent_pipe_handles[name] = None
            process.stdin = streams["stdin"]
            process.stdout = streams["stdout"]
            process.stderr = streams["stderr"]
            return process
        except BaseException as exc:
            if created:
                try:
                    self.kernel32.TerminateProcess(
                        process.process_handle if process is not None else pi.hProcess, 1
                    )
                    self.kernel32.WaitForSingleObject(
                        process.process_handle if process is not None else pi.hProcess, 2000
                    )
                except BaseException:
                    pass
            if process is not None:
                try:
                    self.close_process(process)
                except BaseException:
                    pass
            for stream in streams.values():
                if stream is not None:
                    try:
                        stream.close()
                    except BaseException:
                        pass
            for handle in parent_pipe_handles.values():
                if handle:
                    try:
                        self.close_handle(handle)
                    except BaseException:
                        pass
            if created and process is None:
                for value in (pi.hThread, pi.hProcess):
                    if value:
                        try:
                            self.close_handle(value)
                        except BaseException:
                            pass
            if isinstance(exc, WindowsProcessError):
                raise
            if isinstance(exc, OSError) and getattr(exc, "winerror", None):
                raise WindowsProcessError(f"windows-process-create-failed:winerror-{exc.winerror}") from exc
            raise
        finally:
            if attributes_ready:
                self.kernel32.DeleteProcThreadAttributeList(attribute_pointer)
            for handle in temporary_handles:
                if handle:
                    try:
                        self.close_handle(handle)
                    except BaseException:
                        pass

    def _environment_block(self, env: object):
        entries = windows_environment_entries(env)
        if entries is None:
            return None
        rows = [f"{key}={value}" for key, value in entries]
        return self.ctypes.create_unicode_buffer("\x00".join(rows) + "\x00\x00")

    def _stdio_handle(
        self, value: object, *, direction: str, stdout_child: object | None
    ) -> tuple[object, object | None]:
        if value == subprocess.PIPE:
            security = self.SECURITY_ATTRIBUTES(
                self.ctypes.sizeof(self.SECURITY_ATTRIBUTES), None, True
            )
            read_end = self.wintypes.HANDLE()
            write_end = self.wintypes.HANDLE()
            if not self.kernel32.CreatePipe(
                self.ctypes.byref(read_end), self.ctypes.byref(write_end),
                self.ctypes.byref(security), 0,
            ):
                raise _last_error("windows-stdio-pipe-create-failed")
            child, parent = (
                (read_end, write_end) if direction == "stdin" else (write_end, read_end)
            )
            if not self.kernel32.SetHandleInformation(
                parent, HANDLE_FLAG_INHERIT, 0
            ):
                self.close_handle(child)
                self.close_handle(parent)
                raise _last_error("windows-stdio-pipe-inherit-config-failed")
            return self._handle_value(child), self._handle_value(parent)
        if value == subprocess.DEVNULL:
            return self._open_null(direction), None
        if value is None:
            standard = {
                "stdin": STD_INPUT_HANDLE,
                "stdout": STD_OUTPUT_HANDLE,
                "stderr": STD_ERROR_HANDLE,
            }[direction]
            source = self.kernel32.GetStdHandle(standard)
            if not source or self._handle_int(source) in {0, 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF}:
                return self._open_null(direction), None
            return self._duplicate_inheritable(source), None
        if value == subprocess.STDOUT:
            if stdout_child is None:
                raise WindowsProcessError("windows-stdio-stdout-unavailable")
            return stdout_child, None
        descriptor = value if isinstance(value, int) else value.fileno()
        try:
            import msvcrt
            source = msvcrt.get_osfhandle(descriptor)
        except (OSError, ValueError, TypeError) as exc:
            raise WindowsProcessError("windows-stdio-handle-unavailable") from exc
        return self._duplicate_inheritable(source), None

    def _open_null(self, direction: str) -> object:
        access = GENERIC_READ if direction == "stdin" else GENERIC_WRITE
        security = self.SECURITY_ATTRIBUTES(
            self.ctypes.sizeof(self.SECURITY_ATTRIBUTES), None, True
        )
        handle = self.kernel32.CreateFileW(
            "NUL", access, FILE_SHARE_READ | FILE_SHARE_WRITE,
            self.ctypes.byref(security), OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
        )
        invalid = self.wintypes.HANDLE(-1).value
        if not handle or self._handle_int(handle) == self._handle_int(invalid):
            raise _last_error("windows-null-stdio-open-failed")
        return self._handle_value(handle)

    def _duplicate_inheritable(self, source: object) -> object:
        duplicate = self.wintypes.HANDLE()
        current = self.kernel32.GetCurrentProcess()
        if not self.kernel32.DuplicateHandle(
            current, source, current, self.ctypes.byref(duplicate),
            0, True, DUPLICATE_SAME_ACCESS,
        ):
            raise _last_error("windows-stdio-duplicate-failed")
        return self._handle_value(duplicate)

    def _stream_for_handle(
        self, handle: object, *, name: str, text_mode: bool,
        encoding: str, errors: str, buffering: int,
    ) -> object:
        reading = name != "stdin"
        flags = (os.O_RDONLY if reading else os.O_WRONLY) | os.O_BINARY
        import msvcrt
        descriptor = msvcrt.open_osfhandle(self._handle_int(handle), flags)
        mode = "rb" if reading else "wb"
        binary_buffering = -1 if buffering == 1 else buffering
        stream = os.fdopen(descriptor, mode, buffering=binary_buffering)
        if not text_mode:
            return stream
        return io.TextIOWrapper(
            stream, encoding=encoding, errors=errors,
            line_buffering=buffering == 1, write_through=buffering == 0,
        )

    def is_process_in_job(self, job: object, process: object) -> bool:
        result = self.wintypes.BOOL()
        if not self.kernel32.IsProcessInJob(
            self._process_handle(process), job, self.ctypes.byref(result)
        ):
            raise _last_error("windows-job-membership-unavailable")
        return bool(result.value)

    def process_identity(self, process: object) -> str:
        created = self.wintypes.FILETIME()
        exited = self.wintypes.FILETIME()
        kernel = self.wintypes.FILETIME()
        user = self.wintypes.FILETIME()
        handle = self._process_handle(process)
        observed_pid = int(self.kernel32.GetProcessId(handle))
        if not observed_pid or observed_pid != int(process.pid):
            raise WindowsProcessError("windows-process-handle-pid-mismatch")
        if not self.kernel32.GetProcessTimes(
            handle, self.ctypes.byref(created),
            self.ctypes.byref(exited), self.ctypes.byref(kernel),
            self.ctypes.byref(user),
        ):
            raise _last_error("windows-process-identity-unavailable")
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return f"windows:{int(process.pid)}:{ticks}"

    def resume_process(self, process: object) -> None:
        thread = getattr(process, "thread_handle", None)
        if not thread:
            raise WindowsProcessError("windows-primary-thread-handle-unavailable")
        try:
            previous = int(self.kernel32.ResumeThread(self._handle_value(thread)))
            if previous == 0xFFFFFFFF:
                raise _last_error("windows-primary-thread-resume-failed")
            if previous != 1:
                raise WindowsProcessError("windows-primary-thread-state-unexpected")
        finally:
            self.close_handle(thread)
            process.thread_handle = None

    @staticmethod
    def process_pid(process: object) -> int:
        return int(process.pid)

    @staticmethod
    def close_process(process: object) -> None:
        close = getattr(process, "close_native_handles", None)
        if close is not None:
            close()

    def terminate_process(self, process: object, exit_code: int) -> None:
        terminate = getattr(process, "terminate_native", None)
        if terminate is not None:
            terminate(exit_code)
            return
        raise WindowsProcessError("windows-owned-process-termination-unavailable")

    def process_handle(self, process: object) -> object:
        return self._process_handle(process)

    @staticmethod
    def _handle_value(value: object) -> object:
        if hasattr(value, "value") and value.value is not None:
            return int(value.value)
        try:
            return int(value)
        except (TypeError, ValueError):
            return value

    @classmethod
    def _process_handle(cls, process: object) -> object:
        handle = getattr(process, "process_handle", None)
        if handle is None:
            raise WindowsProcessError("windows-process-handle-unavailable")
        return cls._handle_value(handle)

    @staticmethod
    def _handle_int(value: object) -> int:
        if hasattr(value, "value") and value.value is not None:
            return int(value.value)
        return int(value)


def ctypes_pointer(wintypes: object):
    """Return a pointer type after importing ctypes lazily on any platform."""
    import ctypes
    return ctypes.c_void_p


def _last_error(code: str) -> WindowsProcessError:
    import ctypes
    error = ctypes.get_last_error()
    return WindowsProcessError(f"{code}:winerror-{error}")


def creation_flags_for_native_spawn(flags: object) -> int:
    """Purely enforce the suspended, non-breakaway CreateProcess contract."""
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        raise WindowsProcessError("windows-creation-flags-invalid")
    if flags & CREATE_BREAKAWAY_FROM_JOB:
        raise WindowsProcessError("windows-breakaway-forbidden")
    return flags | CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT


def windows_environment_entries(
    environment: object,
) -> tuple[tuple[str, str], ...] | None:
    """Validate and case-insensitively sort an explicit Windows environment.

    ``None`` keeps CreateProcess' standard inheritance behavior. Explicit
    mappings are preserved exactly. ``None`` lets CreateProcess inherit the
    parent environment; this adapter does not augment caller-provided blocks.
    """
    if environment is None:
        return None

    def normalize(items: tuple[tuple[object, object], ...]):
        result: dict[str, tuple[str, str]] = {}
        for key, value in items:
            if not isinstance(key, str) or not isinstance(value, str):
                raise WindowsProcessError("windows-environment-invalid")
            drive_variable = (
                key.startswith("=") and len(key) == 3
                and key[1].isalpha() and key[2] == ":"
            )
            if (
                not key or ("=" in key and not drive_variable)
                or "\x00" in key or "\x00" in value
            ):
                raise WindowsProcessError("windows-environment-invalid")
            folded = key.casefold()
            if folded in result:
                raise WindowsProcessError("windows-environment-duplicate-key")
            result[folded] = (key, value)
        return result

    try:
        explicit_items = tuple(environment.items())
    except (AttributeError, TypeError) as exc:
        raise WindowsProcessError("windows-environment-invalid") from exc
    merged = normalize(explicit_items)
    return tuple(sorted(merged.values(), key=lambda row: row[0].casefold()))


class WindowsJob:
    """One Job Object; its handle is the ownership lease for a process tree."""

    def __init__(
        self,
        *,
        api: WindowsProcessApi | None = None,
        name: str | None = None,
        existing: bool = False,
    ) -> None:
        self.api = _win32_api() if api is None else api
        self.name = name
        if existing:
            if name is None:
                raise ValueError("existing-job-name-required")
            self.handle = self.api.open_job(name)
            if self.handle is None:
                raise WindowsProcessError("windows-job-not-found")
            try:
                flags = self.api.job_limit_flags(self.handle)
                if flags & (BREAKAWAY_OK | SILENT_BREAKAWAY_OK) or not flags & KILL_ON_JOB_CLOSE:
                    raise WindowsProcessError("windows-job-policy-conflict")
            except BaseException:
                self.api.close_handle(self.handle)
                raise
        else:
            self.handle = self.api.create_job(name)
            try:
                self.api.configure_job(self.handle)
                flags = self.api.job_limit_flags(self.handle)
                if flags & (BREAKAWAY_OK | SILENT_BREAKAWAY_OK) or not flags & KILL_ON_JOB_CLOSE:
                    raise WindowsProcessError("windows-job-policy-conflict")
            except BaseException:
                self.api.close_handle(self.handle)
                raise
        self._closed = False
        self._lock = RLock()

    def spawn(
        self,
        command: object,
        *,
        cwd: str | None = None,
        options: Mapping[str, object] | None = None,
    ) -> object:
        with self._lock:
            if self._closed:
                raise WindowsProcessError("windows-job-closed")
            launch_options = dict(options or {})
            if cwd is not None:
                launch_options["cwd"] = cwd
            process = self.api.launch_suspended(
                self.handle, command, options=launch_options
            )
            try:
                # CreateProcessW receives PROC_THREAD_ATTRIBUTE_JOB_LIST, so
                # the child is in this no-breakaway job before it exists as a
                # runnable thread. Confirm the same owned handles before resume.
                bind_job = getattr(process, "bind_job", None)
                if bind_job is not None:
                    bind_job(self)
                if not self.api.is_process_in_job(self.handle, process):
                    raise WindowsProcessError("windows-job-membership-unconfirmed")
                self.api.process_identity(process)
                self.api.resume_process(process)
                return process
            except BaseException:
                try:
                    # Use the retained CreateProcess process handle even if the
                    # membership query itself failed. The thread is still
                    # suspended, so the child has not run provider code.
                    self.api.terminate_process(process, 1)
                except BaseException:
                    pass
                try:
                    self.terminate()
                    self.wait_empty(2.0)
                except BaseException:
                    pass
                try:
                    self.api.close_process(process)
                except BaseException:
                    pass
                raise

    @property
    def active_processes(self) -> int:
        if self._closed:
            raise WindowsProcessError("windows-job-closed")
        return self.api.active_processes(self.handle)

    @property
    def empty(self) -> bool:
        return self.active_processes == 0

    def terminate(self, exit_code: int = 1) -> None:
        if self._closed:
            raise WindowsProcessError("windows-job-closed")
        self.api.terminate_job(self.handle, exit_code)

    def wait_empty(
        self, timeout: float, *, poll_interval: float = 0.01
    ) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            if self.empty:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(poll_interval, max(0.0, deadline - time.monotonic())))

    def close(self, *, require_empty: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            if require_empty and not self.empty:
                raise WindowsProcessError("windows-job-not-empty")
            self.api.close_handle(self.handle)
            self._closed = True

    def __del__(self) -> None:
        try:
            if not self._closed:
                # KILL_ON_JOB_CLOSE is the parent-death cleanup mechanism.
                self.api.close_handle(self.handle)
                self._closed = True
        except BaseException:
            pass


@dataclass
class _OwnedRoot:
    identity: str | None
    job: WindowsJob
    process: object | None
    confirmed: bool = True


class WindowsJobCell:
    """Cell-membership port: one no-breakaway Job Object per admitted root."""

    def __init__(
        self,
        *,
        api: WindowsProcessApi | None = None,
        runnable: Callable[[str], bool] = os.path.isfile,
    ) -> None:
        self.api = _win32_api() if api is None else api
        self._runnable = runnable
        self._roots: dict[int, _OwnedRoot] = {}
        self._next_orphan_id = -1
        self._closed = False
        self._lock = RLock()

    def _resolved(self, command: object, options: Mapping[str, object]) -> tuple[str, ...]:
        """The command with its program found on the child's ``PATH`` and ``PATHEXT``.

        CreateProcess finds only ``.exe`` programs by name and starts a batch
        shim (``.cmd``, ``.bat``) through cmd.exe, which re-reads every argument
        as a command line; a program that resolves to a shim is refused.
        """
        if not isinstance(command, (tuple, list)) or not command:
            raise WindowsProcessError("windows-command-empty")
        environ = options.get("env")
        search = search_values(
            os.environ if environ is None else environ, windows=True, runnable=self._runnable
        )
        program = resolve_command(os.fsdecode(command[0]), search)
        if program is None:
            raise WindowsProcessError("windows-command-not-found")
        if is_command_shim(program):
            raise WindowsProcessError("windows-command-shim-refused")
        return (program, *(os.fsdecode(value) for value in command[1:]))

    @classmethod
    def from_environment(cls) -> "WindowsJobCell":
        return cls()

    def spawn(
        self,
        command: object,
        *,
        cwd: str,
        options: Mapping[str, object],
    ) -> object:
        with self._lock:
            if self._closed:
                raise WindowsProcessError("subsession-process-cell-closed")
        command = self._resolved(command, options)
        job = WindowsJob(api=self.api)
        process = None
        identity = None
        try:
            process = job.spawn(command, cwd=cwd, options=options)
            identity = self.api.process_identity(process)
            pid = self.api.process_pid(process)
            with self._lock:
                if pid in self._roots:
                    raise WindowsProcessError("windows-process-id-already-owned")
                self._roots[pid] = _OwnedRoot(identity, job, process)
            return process
        except BaseException:
            drained = False
            try:
                if job.active_processes:
                    job.terminate()
                drained = job.wait_empty(2.0)
            except BaseException:
                drained = False
            if drained:
                try:
                    job.close()
                except BaseException:
                    pass
                if process is not None:
                    try:
                        self.api.close_process(process)
                    except BaseException:
                        pass
            else:
                try:
                    pid = self.api.process_pid(process) if process is not None else None
                except BaseException:
                    pid = None
                with self._lock:
                    if pid is None or pid in self._roots:
                        pid = self._next_orphan_id
                        self._next_orphan_id -= 1
                    self._roots[pid] = _OwnedRoot(
                        identity, job, process, confirmed=False
                    )
            raise

    def confirm(self, pid: int) -> bool:
        with self._lock:
            root = self._roots.get(pid)
            if root is None:
                return False
            try:
                observed = self.api.process_identity(root.process)
            except BaseException:
                return False
            # Membership was queried while the primary thread remained
            # suspended inside WindowsJob.spawn. A fast child may already
            # have exited by the time the shared receipt is recorded.
            return root.confirmed and observed == root.identity

    def process_identity(self, pid: int) -> str | None:
        with self._lock:
            root = self._roots.get(pid)
        if root is None:
            return None
        if root.process is None:
            return None
        try:
            return self.api.process_identity(root.process)
        except BaseException:
            return None

    def process_exists(self, pid: int) -> bool:
        """An owned root counts while its Job Object has an active process."""
        with self._lock:
            root = self._roots.get(pid)
        if root is None:
            return process_exists(pid)
        try:
            return root.job.active_processes > 0
        except BaseException:
            return True

    def release_certain(self, pid: int | None, identity: str | None) -> bool:
        if pid is None or identity is None:
            return False
        with self._lock:
            root = self._roots.get(pid)
            if root is None:
                # No record means this adapter never admitted this PID.
                return False
            if root.identity is None or root.identity != identity or root.process is None:
                return False
            try:
                if root.job.active_processes != 0:
                    return False
                if self.api.process_identity(root.process) != identity:
                    return False
            except BaseException:
                return False
            root.job.close()
            self.api.close_process(root.process)
            self._roots.pop(pid, None)
            return True

    @property
    def empty(self) -> bool:
        with self._lock:
            roots = tuple(self._roots.values())
        try:
            return all(root.job.active_processes == 0 for root in roots)
        except BaseException:
            return False

    def terminate_all(self) -> None:
        with self._lock:
            roots = tuple(self._roots.values())
        for root in roots:
            if root.job.active_processes:
                root.job.terminate()

    def terminate_roots(
        self, records: Iterable[tuple[int, str | None]]
    ) -> None:
        with self._lock:
            roots = dict(self._roots)
        for pid, identity in records:
            if not isinstance(identity, str):
                continue
            root = roots.get(pid)
            if root is None or root.identity is None or root.identity != identity:
                continue
            root.job.terminate()

    def terminate_root(self, pid: int) -> None:
        with self._lock:
            root = self._roots.get(pid)
        if root is None:
            raise WindowsProcessError("windows-owned-root-unavailable")
        root.job.terminate()

    def resolve_unconfirmed_empty(self, timeout: float) -> bool:
        """Terminate and close only ambiguous roots, proving each Job empty."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                uncertain = tuple(
                    (pid, root) for pid, root in self._roots.items()
                    if not root.confirmed
                )
            if not uncertain:
                return True
            for pid, root in uncertain:
                try:
                    if root.job.active_processes:
                        root.job.terminate()
                    if root.job.active_processes:
                        continue
                    root.job.close()
                    if root.process is not None:
                        self.api.close_process(root.process)
                    with self._lock:
                        if self._roots.get(pid) is root:
                            self._roots.pop(pid)
                except BaseException:
                    return False
            if time.monotonic() >= deadline:
                with self._lock:
                    return not any(
                        not root.confirmed for root in self._roots.values()
                    )
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def wait_empty(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                roots = tuple(self._roots.values())
            if not roots:
                return True
            all_empty = True
            for root in roots:
                try:
                    active = root.job.active_processes
                    if active and not root.confirmed:
                        root.job.terminate()
                        active = root.job.active_processes
                    if active:
                        all_empty = False
                except BaseException:
                    return False
            if all_empty:
                return True
            if time.monotonic() >= deadline:
                return self.empty
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if any(root.job.active_processes for root in self._roots.values()):
                raise WindowsProcessError("subsession-process-cell-not-empty")
            roots = tuple(self._roots.values())
            self._roots.clear()
            self._closed = True
        for root in roots:
            root.job.close()
            if root.process is not None:
                self.api.close_process(root.process)


def process_identity(pid: int) -> str | None:
    """Observe Windows identity as PID plus kernel process creation time."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME), ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited),
            ctypes.byref(kernel), ctypes.byref(user),
        ):
            return None
        ticks = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
        return f"windows:{pid}:{ticks}"
    finally:
        kernel32.CloseHandle(handle)


def process_exists(pid: int) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER proves no PID.
    try:
        result = int(kernel32.WaitForSingleObject(handle, 0))
        if result == 0:
            return False
        if result == 258:
            return True
        return True
    finally:
        kernel32.CloseHandle(handle)


_default_api: WindowsProcessApi | None = None


def _win32_api() -> WindowsProcessApi:
    global _default_api
    if _default_api is None:
        _default_api = _Win32ProcessApi()
    return _default_api

