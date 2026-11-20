"""Read-only observation of a unit worktree: its agent configuration, its tree and what changed.

Hooks, MCP servers and extensions defined here run on the host with the user's
authority when an agent starts in this directory. The engine never interprets
them; it only detects that they differ from the moment the worktree was made.
"""
import hashlib
import os
import re
import stat

# the engine and the check program read a tree, and tell the environment's entries, the same way
from .check_runner import Unreadable, environment_entry, read_blocks, refuse, tree_digest, tree_files
from .facts import inside
from .platform_facts import CURRENT

AGENT_CONFIG_DIRS = (".claude", ".codex", ".pi", ".agents")
AGENT_CONFIG_FILES = (".mcp.json",)
MAX_LINK_DEPTH = 4
MAX_CONFIG_ENTRIES = 2000
MAX_CONFIG_BYTES = 16 * 1024 * 1024


class TooLarge(Exception):
    """The agent configuration is more than the engine will read; the unit is not safe to drive."""


class _Budget:
    """Counts what one digest reads, so a link into a huge tree cannot keep the engine busy."""

    def __init__(self):
        self.entries, self.bytes = 0, 0

    def entry(self):
        self.entries += 1
        if self.entries > MAX_CONFIG_ENTRIES:
            raise TooLarge()

    def read(self, path, digest):
        for block in read_blocks(path):
            self.bytes += len(block)
            if self.bytes > MAX_CONFIG_BYTES:
                raise TooLarge()
            digest.update(block)


def _target(path, depth, budget):
    """What a link resolves to: the content of a file, or of every file under a directory.

    A link's text can stay the same while what it points at changes, and that target can lie
    anywhere an agent may write. The digest therefore covers the target, not only the link.
    """
    resolved = os.path.realpath(path)
    if not os.path.lexists(resolved):
        return "unresolved"
    if depth >= MAX_LINK_DEPTH:
        raise TooLarge()
    digest = hashlib.sha256()
    if not os.path.isdir(resolved):
        return _content(resolved, budget, depth)
    for directory, directories, files in os.walk(resolved, followlinks=False, onerror=refuse):
        directories.sort()
        for name in sorted([*files, *[d for d in directories if os.path.islink(os.path.join(directory, d))]]):
            budget.entry()
            inner = os.path.join(directory, name)
            digest.update(f"{os.path.relpath(inner, resolved)}\0{_content(inner, budget, depth + 1)}\0".encode(
                "utf-8", "surrogateescape"))
    return f"dir:{digest.hexdigest()}"


def _content(path, budget, depth):
    """One path of the agent configuration by what it is: a link with its target, a file by its content,
    anything else by its type. A path the engine cannot read raises `Unreadable`."""
    try:
        info = os.lstat(path)
    except OSError as error:
        refuse(error)
    if stat.S_ISLNK(info.st_mode):
        try:
            text = os.readlink(path)
        except OSError as error:
            refuse(error)
        return f"link:{text}:{_target(path, depth, budget)}"
    if not stat.S_ISREG(info.st_mode):
        return f"special:{stat.S_IFMT(info.st_mode):o}"
    digest = hashlib.sha256()
    budget.read(path, digest)
    return f"file:{digest.hexdigest()}"


def agent_config_digest(root, committed):
    """A digest of every path under the agent configuration names, of any file type.

    Only the environment's entries (see `check_runner.environment_entry`) are left out, as from
    every other judgment of the worktree. None when the configuration is more than the engine will
    read, or anything in it cannot be read: the caller must not drive the unit.
    """
    try:
        return _agent_config_digest(root, committed)
    except (TooLarge, Unreadable):
        return None


def _agent_config_digest(root, committed):
    budget = _Budget()
    relatives = []
    for top in (*AGENT_CONFIG_DIRS, *AGENT_CONFIG_FILES):
        path = os.path.join(root, top)
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as error:
            refuse(error)
        if environment_entry(root, top, committed):
            continue
        if not stat.S_ISDIR(info.st_mode):
            relatives.append(top)
            continue
        for directory, directories, files in os.walk(path, followlinks=False, onerror=refuse):
            for name in directories:
                if os.path.islink(os.path.join(directory, name)):
                    files.append(name)
            relatives.extend(os.path.relpath(os.path.join(directory, name), root) for name in files)
            if len(relatives) > MAX_CONFIG_ENTRIES:
                raise TooLarge()
    digest = hashlib.sha256()
    for relative in sorted(relatives):
        budget.entry()
        digest.update(f"{relative}\0{_content(os.path.join(root, relative), budget, 0)}\0".encode(
            "utf-8", "surrogateescape"))
    return digest.hexdigest()


def _object_id(path, info, object_format):
    """The id git gives the blob of a file of the tree: a link's text, or a file's content."""
    digest = hashlib.new(object_format)
    if stat.S_ISLNK(info.st_mode):
        try:
            data = os.fsencode(os.readlink(path))
        except OSError as error:
            refuse(error)
        digest.update(b"blob %d\0" % len(data) + data)
        return ("link", digest.hexdigest())
    hashed = hashlib.new(object_format)
    hashed.update(b"blob %d\0" % info.st_size)  # content that changes while it is read is counted as changed
    for block in read_blocks(path):
        hashed.update(block)
    return ("file", hashed.hexdigest())


def changed_paths(base_files, worktree, ignore, committed):
    """Paths whose content differs between git's record of the base commit and the worktree.

    `base_files` is `Git.tree_files` of the base commit. The worktree is read as the check program
    reads it (`check_runner.tree_files`); anything in it the engine cannot read raises `Unreadable`.
    """
    object_format, listed = base_files
    before = {path: entry for path, entry in listed.items()
              if not any(part == ".git" or part in ignore for part in path.split("/"))}
    after = {relative.replace(os.sep, "/"): _object_id(path, info, object_format)
             for relative, path, info in tree_files(worktree, ignore, committed)}
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


def environment_entries(worktree, committed):
    """The names of the entries the environment placed in the worktree's top directory, which git leaves out."""
    return [name for name in sorted(os.listdir(worktree))
            if name != ".git" and environment_entry(worktree, name, committed)]


def read_text(worktree, relative):
    """The text of a file inside the worktree, or None; a path that leaves the worktree is not read."""
    if not isinstance(relative, str) or not relative or "\x00" in relative:
        return None
    root = os.path.realpath(worktree)
    path = os.path.realpath(os.path.join(root, relative))
    if not inside(path, root, CURRENT.path_style) or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


NAMESPACE_RE = re.compile(r"^[0-9a-f]{64}$")
UNIT_DIRECTORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}-[0-9a-f]{16}$")


def unexpected_entries(worktrees, worktree):
    """Anything in the directories above a worktree that the engine did not create.

    The worktree root is writable by every agent on the machine, and agents read configuration
    and instructions from parent directories. The engine creates only namespace directories in
    the root and unit directories in a namespace, so anything else was put there by something
    else and the unit is not driven.
    """
    root = os.path.realpath(worktrees)
    namespace = os.path.dirname(os.path.realpath(worktree))
    found = []
    for directory, pattern in ((root, NAMESPACE_RE), (namespace, UNIT_DIRECTORY_RE)):
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return [directory]
        for name in names:
            path = os.path.join(directory, name)
            if not pattern.fullmatch(name) or os.path.islink(path) or not os.path.isdir(path):
                found.append(path)
    if os.path.dirname(namespace) != root:
        found.append(namespace)
    return found
