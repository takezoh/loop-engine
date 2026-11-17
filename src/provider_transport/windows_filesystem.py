"""Handle-based Windows directory and file authority.

Only this module performs Windows filesystem effects. ``WindowsDirectoryAuthority``
is the Windows adapter of the ``CwdAuthority`` port. A cwd authority retains
only its origin (and optional temporary root) directory; admissions reopen that
anchor, check its identity and retain the handles from the anchor through the
authorized leaf, so a subsession never starts outside the caller's directory.
Ancestors above the anchor are never held: a sandboxed caller cannot rename
them, and an unsandboxed process already holds the same rights as this server.

Directory handles deny delete sharing, which keeps every held directory from
being renamed or replaced, but allow write sharing so other programs can still
create and replace entries inside them. File reads deny write sharing so the
bytes cannot change while they are read.
"""

from __future__ import annotations

from dataclasses import dataclass
import ntpath
import os
import tempfile
from typing import Protocol

from .directory_authority import CwdDenied, CwdLease


FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
DIRECTORY_SHARE = FILE_SHARE_READ | FILE_SHARE_WRITE
FILE_SHARE = FILE_SHARE_READ
OPEN_EXISTING = 3
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_ATTRIBUTE_DIRECTORY = 0x00000010
FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
FILE_ATTRIBUTE_NORMAL = 0x00000080
GENERIC_READ = 0x80000000
FILE_READ_ATTRIBUTES = 0x00000080
FILE_LIST_DIRECTORY = 0x00000001
FILE_FLAG_SEQUENTIAL_SCAN = 0x08000000


class FilesystemAuthorityError(OSError):
    """An authorized path could not be acquired with a stable native handle."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    identity: tuple[int, int]
    attributes: int
    size: int
    write_time: int
    short_name: str = ""

    def matches_name(self, name: str) -> bool:
        folded = ntpath.normcase(name)
        return folded == ntpath.normcase(self.name) or (
            bool(self.short_name) and folded == ntpath.normcase(self.short_name)
        )

    @property
    def is_reparse(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_REPARSE_POINT)

    @property
    def is_directory(self) -> bool:
        return bool(self.attributes & FILE_ATTRIBUTE_DIRECTORY)


class WindowsFilesystemApi(Protocol):
    def open_volume_root(self, path: str) -> object: ...
    def open_anchor(self, path: str) -> object: ...
    def open_child(
        self, parent: object, name: str, *, expected: DirectoryEntry,
        directory: bool,
    ) -> object: ...
    def identity(self, handle: object) -> tuple[int, int]: ...
    def attributes(self, handle: object) -> int: ...
    def final_path(self, handle: object) -> str: ...
    def entries(self, handle: object) -> tuple[DirectoryEntry, ...]: ...
    def read_file(self, handle: object) -> tuple[bytes, tuple[int, int, int]]: ...
    def close(self, handle: object) -> None: ...


def relative_parts(root: str, requested: str) -> tuple[str, ...]:
    """Pure lexical containment decision; handles establish filesystem authority."""
    if not requested or "\x00" in requested:
        raise FilesystemAuthorityError("invalid")
    root_path = ntpath.normcase(ntpath.normpath(ntpath.abspath(root)))
    raw = ntpath.normpath(requested if ntpath.isabs(requested) else ntpath.join(root, requested))
    candidate = ntpath.normcase(ntpath.normpath(ntpath.abspath(raw)))
    try:
        common = ntpath.commonpath((root_path, candidate))
    except ValueError as exc:
        raise FilesystemAuthorityError("outside-root") from exc
    if common != root_path:
        raise FilesystemAuthorityError("outside-root")
    relative = ntpath.relpath(candidate, root_path)
    if relative == ".":
        return ()
    parts = tuple(relative.split("\\"))
    if any(part in {"", ".", ".."} or "\x00" in part for part in parts):
        raise FilesystemAuthorityError("traversal")
    return parts


def _components(path: str) -> tuple[str, tuple[str, ...]]:
    absolute = ntpath.normpath(ntpath.abspath(_display_path(path)))
    drive, tail = ntpath.splitdrive(absolute)
    if not drive or not tail.startswith("\\") or drive.startswith("\\"):
        raise FilesystemAuthorityError("volume-root-unavailable")
    parts = tuple(part for part in tail.split("\\") if part)
    if any(part in {".", ".."} for part in parts):
        raise FilesystemAuthorityError("path-traversal")
    return drive + "\\", parts


def _display_path(path: str) -> str:
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


class WindowsDirectoryLease:
    """Retained no-delete handle chain for one directory."""

    def __init__(
        self,
        api: WindowsFilesystemApi,
        handles: list[object],
        *,
        display_path: str,
        identity: tuple[int, int],
    ) -> None:
        self._api = api
        self._handles = handles
        self.display_path = display_path
        self.identity = identity
        self._closed = False

    @property
    def handle(self) -> object:
        if self._closed:
            raise FilesystemAuthorityError("directory-lease-closed")
        return self._handles[-1]

    @property
    def spawn_path(self) -> str:
        if self._closed:
            raise FilesystemAuthorityError("directory-lease-closed")
        return self._api.final_path(self.handle)

    def admitted_path(self) -> str:
        """The held directory's path; the held handles keep it from being renamed."""
        return _display_path(self.spawn_path)

    def open_relative(
        self,
        relative: str,
        *,
        final_directory: bool | None = None,
        expected_identity: tuple[int, int] | None = None,
    ) -> tuple[object, tuple[object, ...]]:
        """Open and retain all traversed handles until the caller closes them."""
        if self._closed:
            raise FilesystemAuthorityError("directory-lease-closed")
        components = tuple(
            part for part in ntpath.normpath(relative).split("\\") if part
        )
        if ntpath.isabs(relative) or any(part in {".", ".."} for part in components):
            raise FilesystemAuthorityError("path-traversal")
        handles: list[object] = []
        current = self.handle
        try:
            for index, name in enumerate(components):
                entries = self._api.entries(current)
                match = next(
                    (entry for entry in entries if entry.matches_name(name)),
                    None,
                )
                if match is None:
                    raise FilesystemAuthorityError("missing")
                if match.is_reparse:
                    raise FilesystemAuthorityError("reparse")
                wants_directory = (
                    final_directory if index == len(components) - 1
                    else True
                )
                if wants_directory is None:
                    wants_directory = match.is_directory
                if match.is_directory != wants_directory:
                    raise FilesystemAuthorityError("entry-type-mismatch")
                child = self._api.open_child(
                    current, match.name, expected=match,
                    directory=wants_directory,
                )
                handles.append(child)
                current = child
            if expected_identity is not None and self._api.identity(current) != expected_identity:
                raise FilesystemAuthorityError("alias-changed")
            if not handles:
                return self.handle, ()
            return handles[-1], tuple(handles)
        except BaseException:
            for handle in reversed(handles):
                try:
                    self._api.close(handle)
                except BaseException:
                    pass
            raise

    def read_file(
        self,
        relative: str,
        *,
        expected_identity: tuple[int, int] | None = None,
    ) -> tuple[bytes, tuple[int, int]]:
        handle, held = self.open_relative(
            relative, final_directory=False, expected_identity=expected_identity
        )
        try:
            before = self._api.identity(handle)
            data, state = self._api.read_file(handle)
            after = self._api.identity(handle)
            if before != after or len(data) != state[0]:
                raise FilesystemAuthorityError("changing")
            return data, before
        finally:
            for owned in reversed(held):
                self._api.close(owned)

    def tree_records(self, *, excluded: frozenset[str] = frozenset()):
        """Return (relative path, type, executable, bytes, file identity) rows."""
        records: list[tuple[str, str, bool, bytes, tuple[int, int] | None]] = []
        pending = [""]
        while pending:
            directory = pending.pop()
            handle, held = self.open_relative(directory, final_directory=True) if directory else (self.handle, ())
            try:
                for entry in self._api.entries(handle):
                    relative = f"{directory}\\{entry.name}" if directory else entry.name
                    normalized = relative.replace("\\", "/")
                    if normalized in excluded or "__pycache__" in relative.split("\\") or relative.endswith(".pyc"):
                        continue
                    if entry.is_reparse:
                        raise FilesystemAuthorityError("reparse")
                    child = self._api.open_child(
                        handle, entry.name, expected=entry,
                        directory=entry.is_directory,
                    )
                    try:
                        identity = self._api.identity(child)
                        attributes = self._api.attributes(child)
                        executable = False
                        if attributes & FILE_ATTRIBUTE_DIRECTORY:
                            records.append((normalized, "directory", executable, b"", identity))
                            pending.append(relative)
                        else:
                            data, _state = self._api.read_file(child)
                            records.append((normalized, "regular", executable, data, identity))
                    finally:
                        self._api.close(child)
            finally:
                for owned in reversed(held):
                    self._api.close(owned)
        records.sort(key=lambda row: row[0])
        return tuple(records)

    def close(self) -> None:
        if self._closed:
            return
        for handle in reversed(self._handles):
            self._api.close(handle)
        self._handles.clear()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class WindowsDirectoryAuthority:
    """Captured origin plus optional explicit temporary-root authority.

    Only the origin and temporary root themselves stay open for the authority's
    lifetime; their ancestors are not held.
    """

    def __init__(
        self, api: WindowsFilesystemApi, origin: WindowsDirectoryLease,
        *, tmp: WindowsDirectoryLease | None = None,
    ) -> None:
        self._api = api
        self._origin = origin
        self._tmp = tmp
        self.display_path = ntpath.abspath(origin.display_path)
        self.identity = origin.identity
        self._closed = False

    @classmethod
    def capture(
        cls,
        cwd: str | os.PathLike[str] | None = None,
        *,
        allow_tmp: bool = False,
        api: WindowsFilesystemApi | None = None,
    ) -> "WindowsDirectoryAuthority":
        selected = _windows_api() if api is None else api
        path = os.getcwd() if cwd is None else os.fspath(cwd)
        origin = open_anchor(path, api=selected)
        temporary = None
        try:
            if allow_tmp:
                temporary = open_anchor(tempfile.gettempdir(), api=selected)
            return cls(selected, origin, tmp=temporary)
        except BaseException:
            origin.close()
            if temporary is not None:
                temporary.close()
            raise

    def admit(self, requested: str | os.PathLike[str]) -> CwdLease:
        try:
            return CwdLease(self._held(requested))
        except FilesystemAuthorityError as exc:
            raise CwdDenied(f"cwd-denied:{exc.code}") from exc

    def _held(self, requested: str | os.PathLike[str]) -> WindowsDirectoryLease:
        if self._closed:
            raise FilesystemAuthorityError("authority-closed")
        raw = os.fspath(requested)
        try:
            relative = relative_parts(self.display_path, raw)
            root = self._origin
        except FilesystemAuthorityError as exc:
            if exc.code != "outside-root" or self._tmp is None:
                raise
            relative = relative_parts(self._tmp.display_path, raw)
            root = self._tmp
        # Reopen the anchor so the lease independently owns its chain.
        current = _duplicate_directory_lease(root)
        if not relative:
            return current
        try:
            for part in relative:
                entries = self._api.entries(current.handle)
                match = next(
                    (entry for entry in entries if entry.matches_name(part)),
                    None,
                )
                if match is None or match.is_reparse or not match.is_directory:
                    raise FilesystemAuthorityError("not-descendant-or-reparse")
                child = self._api.open_child(
                    current.handle, match.name, expected=match, directory=True
                )
                current._handles.append(child)
                current.display_path = self._api.final_path(child)
                current.identity = self._api.identity(child)
            return current
        except BaseException:
            current.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._origin.close()
        if self._tmp is not None:
            self._tmp.close()
        self._closed = True


def _duplicate_directory_lease(
    lease: WindowsDirectoryLease,
) -> WindowsDirectoryLease:
    # A separate acquisition keeps lease ownership independent while the
    # original root authority remains available for later admissions. The held
    # anchor cannot be renamed, so its current path still names the same
    # directory; the identity check rejects anything else.
    return open_anchor(
        lease._api.final_path(lease.handle),
        api=lease._api,
        expected_identity=lease.identity,
    )


def open_anchor(
    path: str | os.PathLike[str],
    *,
    api: WindowsFilesystemApi | None = None,
    expected_identity: tuple[int, int] | None = None,
) -> WindowsDirectoryLease:
    """Open one directory as an authority anchor without holding its ancestors."""
    selected = _windows_api() if api is None else api
    absolute = ntpath.abspath(_display_path(os.fspath(path)))
    handle = selected.open_anchor(absolute)
    try:
        identity = selected.identity(handle)
        if expected_identity is not None and identity != expected_identity:
            raise FilesystemAuthorityError("alias-changed")
        final_path = _display_path(selected.final_path(handle))
        return WindowsDirectoryLease(
            selected, [handle], display_path=final_path, identity=identity
        )
    except BaseException:
        selected.close(handle)
        raise


def open_directory(
    path: str | os.PathLike[str],
    *,
    api: WindowsFilesystemApi | None = None,
) -> WindowsDirectoryLease:
    selected = _windows_api() if api is None else api
    absolute = ntpath.abspath(os.fspath(path))
    volume_root, components = _components(absolute)
    root = selected.open_volume_root(volume_root)
    handles = [root]
    try:
        for component in components:
            entries = selected.entries(handles[-1])
            match = next(
                (entry for entry in entries if entry.matches_name(component)),
                None,
            )
            if match is None or match.is_reparse or not match.is_directory:
                raise FilesystemAuthorityError("path-component-not-directory")
            child = selected.open_child(
                handles[-1], match.name, expected=match, directory=True
            )
            handles.append(child)
        identity = selected.identity(handles[-1])
        final_path = _display_path(selected.final_path(handles[-1]))
        return WindowsDirectoryLease(
            selected, handles, display_path=final_path, identity=identity
        )
    except BaseException:
        for handle in reversed(handles):
            try:
                selected.close(handle)
            except BaseException:
                pass
        raise



class _Win32FilesystemApi:
    """ctypes implementation using no-delete-sharing handles and stable IDs."""

    def __init__(self) -> None:
        if os.name != "nt":
            raise FilesystemAuthorityError("windows-filesystem-api-unavailable")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._bind()

    def _bind(self) -> None:
        k, w, c = self.kernel32, self.wintypes, self.ctypes
        k.CreateFileW.argtypes = [w.LPCWSTR, w.DWORD, w.DWORD, c.c_void_p, w.DWORD, w.DWORD, w.HANDLE]
        k.CreateFileW.restype = w.HANDLE
        k.GetFileInformationByHandle.argtypes = [w.HANDLE, c.c_void_p]
        k.GetFileInformationByHandle.restype = w.BOOL
        k.GetFileInformationByHandleEx.argtypes = [w.HANDLE, w.INT, c.c_void_p, w.DWORD]
        k.GetFileInformationByHandleEx.restype = w.BOOL
        k.GetFinalPathNameByHandleW.argtypes = [w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD]
        k.GetFinalPathNameByHandleW.restype = w.DWORD
        k.ReadFile.argtypes = [w.HANDLE, c.c_void_p, w.DWORD, c.POINTER(w.DWORD), c.c_void_p]
        k.ReadFile.restype = w.BOOL
        k.CloseHandle.argtypes = [w.HANDLE]
        k.CloseHandle.restype = w.BOOL
        k.CreateDirectoryW.argtypes = [w.LPCWSTR, c.c_void_p]
        k.CreateDirectoryW.restype = w.BOOL

    def _open(
        self, path: str, *, directory: bool
    ) -> object:
        access = FILE_READ_ATTRIBUTES | (FILE_LIST_DIRECTORY if directory else GENERIC_READ)
        flags = FILE_FLAG_OPEN_REPARSE_POINT | (FILE_FLAG_BACKUP_SEMANTICS if directory else FILE_FLAG_SEQUENTIAL_SCAN)
        share = DIRECTORY_SHARE if directory else FILE_SHARE
        handle = self.kernel32.CreateFileW(
            path, access, share, None, OPEN_EXISTING, flags, None
        )
        if handle == self.wintypes.HANDLE(-1).value:
            raise _fs_error("open-failed")
        attrs = self.attributes(handle)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            self.close(handle)
            raise FilesystemAuthorityError("reparse")
        if bool(attrs & FILE_ATTRIBUTE_DIRECTORY) != directory:
            self.close(handle)
            raise FilesystemAuthorityError("entry-type-mismatch")
        return handle

    def open_volume_root(self, path: str) -> object:
        return self._open(path, directory=True)

    def open_anchor(self, path: str) -> object:
        # The anchor itself must be a real directory; _open rejects a reparse
        # point because it opens the final component without following it.
        return self._open(path, directory=True)

    def open_child(
        self, parent: object, name: str, *, expected: DirectoryEntry,
        directory: bool,
    ) -> object:
        if not name or name in {".", ".."} or any(char in name for char in "\\/:\x00"):
            raise FilesystemAuthorityError("path-component-invalid")
        path = ntpath.join(self.final_path(parent), name)
        opened = self._open(path, directory=directory)
        if self.identity(opened) != expected.identity:
            self.close(opened)
            raise FilesystemAuthorityError("alias-changed")
        after = next(
            (entry for entry in self.entries(parent) if entry.matches_name(name)),
            None,
        )
        if after is None or after.identity != expected.identity or after.is_reparse:
            self.close(opened)
            raise FilesystemAuthorityError("alias-changed")
        return opened

    def identity(self, handle: object) -> tuple[int, int]:
        info = self._file_info(handle)
        file_index = (int(info.nFileIndexHigh) << 32) | int(info.nFileIndexLow)
        return int(info.dwVolumeSerialNumber), file_index

    def attributes(self, handle: object) -> int:
        return int(self._file_info(handle).dwFileAttributes)

    def _file_info(self, handle: object):
        class BY_HANDLE_FILE_INFORMATION(self.ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", self.wintypes.DWORD),
                ("ftCreationTime", self.wintypes.FILETIME),
                ("ftLastAccessTime", self.wintypes.FILETIME),
                ("ftLastWriteTime", self.wintypes.FILETIME),
                ("dwVolumeSerialNumber", self.wintypes.DWORD),
                ("nFileSizeHigh", self.wintypes.DWORD),
                ("nFileSizeLow", self.wintypes.DWORD),
                ("nNumberOfLinks", self.wintypes.DWORD),
                ("nFileIndexHigh", self.wintypes.DWORD),
                ("nFileIndexLow", self.wintypes.DWORD),
            ]
        info = BY_HANDLE_FILE_INFORMATION()
        if not self.kernel32.GetFileInformationByHandle(handle, self.ctypes.byref(info)):
            raise _fs_error("file-identity-unavailable")
        return info

    def final_path(self, handle: object) -> str:
        needed = self.kernel32.GetFinalPathNameByHandleW(handle, None, 0, 0)
        if not needed:
            raise _fs_error("final-path-unavailable")
        buffer = self.ctypes.create_unicode_buffer(needed + 1)
        length = self.kernel32.GetFinalPathNameByHandleW(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise _fs_error("final-path-unavailable")
        value = buffer.value
        return value

    def entries(self, handle: object) -> tuple[DirectoryEntry, ...]:
        class FILE_ID_BOTH_DIR_INFO(self.ctypes.Structure):
            _fields_ = [
                ("NextEntryOffset", self.wintypes.DWORD),
                ("FileIndex", self.wintypes.DWORD),
                ("CreationTime", self.ctypes.c_longlong),
                ("LastAccessTime", self.ctypes.c_longlong),
                ("LastWriteTime", self.ctypes.c_longlong),
                ("ChangeTime", self.ctypes.c_longlong),
                ("EndOfFile", self.ctypes.c_longlong),
                ("AllocationSize", self.ctypes.c_longlong),
                ("FileAttributes", self.wintypes.DWORD),
                ("FileNameLength", self.wintypes.DWORD),
                ("EaSize", self.wintypes.DWORD),
                ("ShortNameLength", self.wintypes.BYTE),
                ("ShortName", self.wintypes.WCHAR * 12),
                ("FileId", self.ctypes.c_longlong),
                ("FileName", self.wintypes.WCHAR * 1),
            ]

        rows: list[DirectoryEntry] = []
        buffer = self.ctypes.create_string_buffer(64 * 1024)
        info_class = 11  # FileIdBothDirectoryRestartInfo
        volume = self.identity(handle)[0]
        while True:
            if not self.kernel32.GetFileInformationByHandleEx(
                handle, info_class, buffer, len(buffer)
            ):
                error = self.ctypes.get_last_error()
                if error == 18:
                    break
                if rows and error == 38:
                    break
                raise _fs_error("directory-entry-query-failed", error)
            info_class = 10  # FileIdBothDirectoryInfo
            offset = 0
            while True:
                entry = self.ctypes.cast(
                    self.ctypes.addressof(buffer) + offset,
                    self.ctypes.POINTER(FILE_ID_BOTH_DIR_INFO),
                ).contents
                chars = int(entry.FileNameLength) // self.ctypes.sizeof(self.wintypes.WCHAR)
                name = self.ctypes.wstring_at(
                    self.ctypes.addressof(buffer) + offset + FILE_ID_BOTH_DIR_INFO.FileName.offset,
                    chars,
                )
                if name not in {".", ".."}:
                    rows.append(DirectoryEntry(
                        name=name,
                        identity=(volume, int(entry.FileId)),
                        attributes=int(entry.FileAttributes),
                        size=int(entry.EndOfFile),
                        write_time=int(entry.LastWriteTime),
                        short_name=entry.ShortName[:int(entry.ShortNameLength) // self.ctypes.sizeof(self.wintypes.WCHAR)],
                    ))
                next_offset = int(entry.NextEntryOffset)
                if not next_offset:
                    break
                offset += next_offset
        return tuple(rows)

    def read_file(self, handle: object) -> tuple[bytes, tuple[int, int, int]]:
        before = self._file_info(handle)
        if before.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT or before.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY:
            raise FilesystemAuthorityError("entry-type-mismatch")
        chunks = []
        while True:
            buffer = self.ctypes.create_string_buffer(128 * 1024)
            read = self.wintypes.DWORD()
            if not self.kernel32.ReadFile(
                handle, buffer, len(buffer), self.ctypes.byref(read), None
            ):
                raise _fs_error("file-read-failed")
            if not read.value:
                break
            chunks.append(buffer.raw[:read.value])
        after = self._file_info(handle)
        before_id = self.identity(handle)
        before_size = (int(before.nFileSizeHigh) << 32) | int(before.nFileSizeLow)
        after_size = (int(after.nFileSizeHigh) << 32) | int(after.nFileSizeLow)
        before_write = _filetime(before.ftLastWriteTime)
        after_write = _filetime(after.ftLastWriteTime)
        if before_id != self.identity(handle) or before_size != after_size or before_write != after_write:
            raise FilesystemAuthorityError("changing")
        data = b"".join(chunks)
        if len(data) != after_size:
            raise FilesystemAuthorityError("changing")
        return data, (after_size, after_write, int(after.dwFileAttributes))

    def close(self, handle: object) -> None:
        if handle and not self.kernel32.CloseHandle(handle):
            raise _fs_error("native-handle-close-failed")


_api: WindowsFilesystemApi | None = None


def _windows_api() -> WindowsFilesystemApi:
    global _api
    if _api is None:
        _api = _Win32FilesystemApi()
    return _api


def _filetime(value: object) -> int:
    return (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)


def _fs_error(code: str, error: int | None = None) -> FilesystemAuthorityError:
    import ctypes
    error = ctypes.get_last_error() if error is None else error
    return FilesystemAuthorityError(f"{code}:winerror-{error}")
