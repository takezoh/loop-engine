"""A working-directory authority over a declared tree, held to the directory port contract.

Paths are POSIX strings and nothing touches the filesystem: the test declares
which directories exist under the origin and the temporary root, and which
names are links.
"""
from pathlib import PurePosixPath

from provider_transport.directory_authority import CwdDenied, CwdLease


class FakeHeldDirectory:
    def __init__(self, path, identity):
        self.path = path
        self.identity = identity
        self.closed = False

    @property
    def spawn_path(self):
        return self.path

    def admitted_path(self):
        return self.path

    def close(self):
        self.closed = True


class FakeCwdAuthority:
    def __init__(self, origin="/work", *, directories=(), links=(), temporary_root=None):
        self.origin = PurePosixPath(origin)
        self.temporary_root = None if temporary_root is None else PurePosixPath(temporary_root)
        self.directories = {self.origin, *(PurePosixPath(path) for path in directories)}
        if self.temporary_root is not None:
            self.directories.add(self.temporary_root)
        self.links = {PurePosixPath(path) for path in links}
        self.closed = False

    def admit(self, requested):
        raw = str(requested)
        if not raw or "\x00" in raw:
            raise CwdDenied("cwd-denied:invalid")
        if self.closed:
            raise CwdDenied("cwd-denied:authority-closed")
        candidate = PurePosixPath(raw)
        if not candidate.is_absolute():
            root, parts = self.origin, candidate.parts
        elif candidate.is_relative_to(self.origin):
            root, parts = self.origin, candidate.relative_to(self.origin).parts
        elif self.temporary_root is not None and candidate.is_relative_to(self.temporary_root):
            root, parts = self.temporary_root, candidate.relative_to(self.temporary_root).parts
        else:
            raise CwdDenied("cwd-denied:outside-origin")
        if ".." in parts:
            raise CwdDenied("cwd-denied:traversal")
        current = root
        for part in parts:
            current = current / part
            if current in self.links or current not in self.directories:
                raise CwdDenied("cwd-denied:not-descendant-or-symlink")
        ordered = sorted(str(path) for path in self.directories)
        return CwdLease(FakeHeldDirectory(str(current), (0, ordered.index(str(current)))))

    def close(self):
        self.closed = True
