"""Windows user identity and private-object ACL primitives.

Pure policy stays separate from token and ACL syscalls so the decision can be
tested on non-Windows hosts with a fake token/ACL provider.
"""

from __future__ import annotations

from dataclasses import dataclass
import os


SYSTEM_SID = "S-1-5-18"


def private_object_sddl(user_sid: str, *, inherit: bool = False) -> str:
    """Return a protected DACL granting access only to this user and SYSTEM."""
    if not isinstance(user_sid, str) or not user_sid.startswith("S-1-") or any(
        char in user_sid for char in "()\x00"
    ):
        raise ValueError("windows-user-sid-invalid")
    flags = "OICI" if inherit else ""
    # Elevated tokens may otherwise default the owner to Administrators.
    return f"O:{user_sid}D:P(A;{flags};GA;;;{user_sid})(A;{flags};GA;;;{SYSTEM_SID})"


class WindowsIdentityError(RuntimeError):
    pass


def current_user_sid() -> str:
    """Return the current process token's user SID using Win32 token APIs."""
    if os.name != "nt":
        raise WindowsIdentityError("windows-token-unavailable")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    advapi32.OpenProcessToken.argtypes = [
        ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
    ]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
    ):
        raise WindowsIdentityError("windows-token-query-failed")
    try:
        needed = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        if not needed.value:
            raise WindowsIdentityError("windows-token-user-unavailable")
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi32.GetTokenInformation(
            token, 1, buffer, needed.value, ctypes.byref(needed)
        ):
            raise WindowsIdentityError("windows-token-user-unavailable")
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
        sid_text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(
            sid_pointer, ctypes.byref(sid_text)
        ):
            raise WindowsIdentityError("windows-token-sid-unavailable")
        try:
            return sid_text.value
        finally:
            kernel32.LocalFree(ctypes.cast(sid_text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)


@dataclass
class NativeSecurityAttributes:
    """Lifetime owner for a Win32 SECURITY_ATTRIBUTES and its descriptor."""

    attributes: object
    descriptor: object
    kernel32: object

    def close(self) -> None:
        if self.descriptor:
            self.kernel32.LocalFree(self.descriptor)
            self.descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def security_attributes(
    user_sid: str | None = None, *, inherit: bool = False
) -> NativeSecurityAttributes:
    """Build Win32 SECURITY_ATTRIBUTES with the private user/SYSTEM DACL."""
    if os.name != "nt":
        raise WindowsIdentityError("windows-security-descriptor-unavailable")
    import ctypes
    from ctypes import wintypes

    user_sid = current_user_sid() if user_sid is None else user_sid
    sddl = private_object_sddl(user_sid, inherit=inherit)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl, 1, ctypes.byref(descriptor), None
    ):
        raise WindowsIdentityError("windows-security-descriptor-invalid")

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", wintypes.BOOL),
        ]

    value = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), descriptor, False)

    return NativeSecurityAttributes(value, descriptor, kernel32)
