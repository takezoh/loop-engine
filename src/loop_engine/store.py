"""The engine's state home: every file under it is written by the engine process only.

Entries (CLI, MCP) read status and results from here and never write. A unit's
events are the source of truth; status and results are derived views.
"""
import errno
import hashlib
import json
import os
import stat
import tempfile
from threading import Lock

from .facts import ID_RE

ROOT_NAME = "loop-engine"


def home_root(environ):
    """The engine root under the dev-skills state home."""
    home = environ.get("DEV_SKILLS_HOME") or os.path.join(os.path.expanduser("~"), ".dev-skills")
    return os.path.join(os.path.abspath(home), ROOT_NAME)


def worktrees_root(environ):
    """Where unit worktrees are made: a directory the agents' own sandboxes let them write.

    The engine's state home is read-only to agents, so worktrees do not live under it.
    `DEV_SKILLS_WORKTREES` names the directory; the default is the user's cache directory.
    """
    chosen = environ.get("DEV_SKILLS_WORKTREES")
    if not chosen:
        cache = environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
        chosen = os.path.join(cache, "dev-skills", "worktrees")
    return os.path.abspath(chosen)


def worktree_path(worktrees, common_dir, unit_id):
    """Unit worktrees live outside the source tree, namespaced by the repository's git directory."""
    namespace = hashlib.sha256(common_dir.encode("utf-8", "surrogateescape")).hexdigest()
    return os.path.join(worktrees, namespace, unit_id)


def unit_dir(root, unit_id):
    if not isinstance(unit_id, str) or not ID_RE.match(unit_id):
        raise ValueError("invalid unit id")
    return os.path.join(root, "units", unit_id)


def read_json(path):
    """The JSON value of a file, or None when it is missing or not yet complete."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def _write_atomic(path, data):
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")


def list_units(root):
    try:
        names = sorted(os.listdir(os.path.join(root, "units")))
    except OSError:
        return []
    return [name for name in names if ID_RE.match(name)]


class UnitStore:
    """One unit's directory. A lock serializes the engine's own threads."""

    def __init__(self, root, unit_id):
        self.root, self.unit_id = root, unit_id
        self.dir = unit_dir(root, unit_id)
        self._lock = Lock()

    def path(self, *names):
        return os.path.join(self.dir, *names)

    def exists(self):
        return os.path.isfile(self.path("contract.json"))

    def create(self, contract, admission):
        """Record an accepted unit: the caller's contract and what the engine fixed on accepting it."""
        os.makedirs(self.path("result", "turns"), mode=0o755, exist_ok=True)
        _write_atomic(self.path("admission.json"), _dump(admission))
        _write_atomic(self.path("contract.json"), _dump(contract))

    def contract(self):
        return read_json(self.path("contract.json"))

    def admission(self):
        return read_json(self.path("admission.json"))

    def events(self):
        events = []
        try:
            with open(self.path("events.jsonl"), encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue  # a line torn by a kill carries no event
                    if isinstance(event, dict) and isinstance(event.get("type"), str):
                        events.append(event)
        except OSError:
            pass
        return events

    def append(self, event):
        with self._lock:
            with open(self.path("events.jsonl"), "a+b") as handle:
                if handle.tell() > 0:
                    handle.seek(-1, os.SEEK_END)
                    if handle.read(1) != b"\n":
                        handle.write(b"\n")
                handle.write(_dump(event) + b"\n")
                handle.flush()
                os.fsync(handle.fileno())

    def write_status(self, status):
        with self._lock:
            _write_atomic(self.path("status.json"), _dump(dict(status, unit_id=self.unit_id)))

    def status(self):
        return read_json(self.path("status.json"))

    def write_json(self, name, value):
        with self._lock:
            _write_atomic(self.path(name), _dump(value))

    def write_result(self, name, data):
        with self._lock:
            _write_atomic(self.path("result", name), data if isinstance(data, bytes) else _dump(data))


# -- the check program agents run: a copy under the state home, named by its content --

CHECK_PROGRAM = "check_runner.py"


class CheckProgramChanged(RuntimeError):
    """The published check program is not the content its directory names: no command names it."""


def check_program_path(root, digest):
    return os.path.join(root, "check", digest, CHECK_PROGRAM)


def _content_digest(path):
    """The sha256 of a regular file reached without following a link at its last component."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    handle = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise CheckProgramChanged(f"{path} is not a regular file")
        digest = hashlib.sha256()
        for block in iter(lambda: os.read(handle, 1 << 16), b""):
            digest.update(block)
        return digest.hexdigest()
    finally:
        os.close(handle)


def verified_check_program(path):
    """`path` when its content still hashes to the name of its directory; else `CheckProgramChanged`."""
    try:
        digest = _content_digest(path)
    except OSError as error:
        raise CheckProgramChanged(f"{path} cannot be read: {error}") from None
    if digest != os.path.basename(os.path.dirname(path)):
        raise CheckProgramChanged(f"{path} does not hold the content its directory names")
    return path


def publish_check_program(root, source):
    """Copy the check program to `<root>/check/<sha256>/check_runner.py` and return that path.

    Agents run the copy: their sandbox can read the state home, not the engine's code location.
    Publishing is idempotent and safe against a concurrent engine: the content is written under a
    temporary name in the same directory and linked to the final name, which never replaces a file
    already there. A file already there must hold the same content; it is never overwritten.
    """
    with open(source, "rb") as handle:
        data = handle.read()
    path = check_program_path(root, hashlib.sha256(data).hexdigest())
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o755, exist_ok=True)
    if not os.path.lexists(path):
        handle, temporary = tempfile.mkstemp(prefix=".publishing-", dir=directory)
        try:
            with os.fdopen(handle, "wb") as written:
                written.write(data)
                written.flush()
                os.fsync(written.fileno())
            os.chmod(temporary, 0o444)
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass  # another engine published it first; its content is checked below
        finally:
            os.unlink(temporary)
    return verified_check_program(path)


# -- requests: entries drop a request file, the engine answers with a response file --

REQUEST_ID_LENGTH = 32


def request_path(root, request_id):
    if not (isinstance(request_id, str) and len(request_id) == REQUEST_ID_LENGTH
            and all(char in "0123456789abcdef" for char in request_id)):
        raise ValueError("invalid request id")
    return os.path.join(root, "inbox", request_id + ".json")


def response_path(root, request_id):
    return os.path.join(root, "outbox", os.path.basename(request_path(root, request_id)))


def write_request(root, request_id, payload):
    """Called by entries. The inbox is the only place an entry writes."""
    os.makedirs(os.path.join(root, "inbox"), mode=0o700, exist_ok=True)
    _write_atomic(request_path(root, request_id), _dump(payload))


def take_requests(root):
    """Called by the engine: every complete request, removed from the inbox."""
    directory = os.path.join(root, "inbox")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    taken = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        payload = read_json(path)
        try:
            os.unlink(path)
            request_path(root, name[:-5])
        except (OSError, ValueError):
            continue
        taken.append((name[:-5], payload))
    return taken


def write_response(root, request_id, payload):
    os.makedirs(os.path.join(root, "outbox"), mode=0o700, exist_ok=True)
    _write_atomic(response_path(root, request_id), _dump(payload))


def drop_old_responses(root, older_than):
    """Remove responses last written before the timestamp `older_than`."""
    directory = os.path.join(root, "outbox")
    try:
        names = os.listdir(directory)
    except OSError:
        return
    for name in names:
        path = os.path.join(directory, name)
        try:
            if os.path.getmtime(path) < older_than:
                os.unlink(path)
        except OSError:
            continue


class LockFailed(Exception):
    """The lock could not be taken for a reason other than another process holding it."""


def lock_contended(error, *, windows):
    """Whether a refused non-blocking lock means that another process holds it.

    Any other errno (permission, a missing directory, a filesystem without locks)
    is a failure to lock, not a sign of another engine.
    """
    if windows:
        return error.errno == errno.EACCES  # msvcrt.locking with LK_NBLCK on a locked byte
    return error.errno in (errno.EAGAIN, errno.EWOULDBLOCK)  # flock with LOCK_NB on a locked file


def _take(handle, *, windows):
    if windows:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class ProcessLock:
    """An exclusive lock on a file, held for as long as the owning process lives."""

    def __init__(self, path):
        self.path, self._handle = path, None

    def acquire(self):
        """True when this process now holds the lock; False when another process does.

        Raises `LockFailed`, naming the cause, when the lock cannot be taken at all.
        """
        windows = os.name == "nt"
        try:
            os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
            handle = open(self.path, "a+b")
        except OSError as error:
            raise LockFailed(f"cannot open the lock file {self.path}: {error}") from error
        try:
            _take(handle, windows=windows)
        except OSError as error:
            handle.close()
            if lock_contended(error, windows=windows):
                return False
            raise LockFailed(f"cannot lock {self.path}: {error}") from error
        self._handle = handle
        return True

    def release(self):
        if self._handle is not None:
            self._handle.close()
            self._handle = None
