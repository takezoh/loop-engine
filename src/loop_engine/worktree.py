"""Read-only observation of a unit worktree: its agent configuration, its tree and what changed.

Hooks, MCP servers and extensions defined here run on the host with the user's
authority when an agent starts in this directory. The engine never interprets
them; it only detects that they differ from the moment the worktree was made.
"""
import hashlib
import os
import re
import stat

# the engine and the check program digest a tree, and tell the environment's entries, the same way
from .check_runner import environment_entry, tree_digest

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
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 16), b""):
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
    if not os.path.exists(resolved):
        return "unresolved"
    if depth >= MAX_LINK_DEPTH:
        raise TooLarge()
    digest = hashlib.sha256()
    if os.path.isfile(resolved):
        try:
            budget.read(resolved, digest)
        except OSError:
            return "unreadable"
        return f"file:{digest.hexdigest()}"
    for directory, directories, files in os.walk(resolved, followlinks=False):
        directories.sort()
        for name in sorted([*files, *[d for d in directories if os.path.islink(os.path.join(directory, d))]]):
            budget.entry()
            inner = os.path.join(directory, name)
            if os.path.islink(inner):
                content = f"link:{os.readlink(inner)}:{_target(inner, depth + 1, budget)}"
            elif os.path.isfile(inner):
                inner_digest = hashlib.sha256()
                try:
                    budget.read(inner, inner_digest)
                    content = f"file:{inner_digest.hexdigest()}"
                except OSError:
                    content = "unreadable"
            else:
                content = "special"
            digest.update(f"{os.path.relpath(inner, resolved)}\0{content}\0".encode("utf-8", "surrogateescape"))
    return f"dir:{digest.hexdigest()}"


def _entry(root, relative, budget=None):
    budget = budget or _Budget()
    path = os.path.join(root, relative)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return f"link:{os.readlink(path)}:{_target(path, 0, budget)}"
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        # Agents and their sandboxes leave empty placeholder files; they define nothing.
        return None
    digest = hashlib.sha256()
    budget.read(path, digest)
    return f"file:{digest.hexdigest()}"


def agent_config_digest(root):
    """A digest of every non-empty file and link under the agent configuration paths.

    None when the configuration is more than the engine will read: the caller must not drive the unit.
    """
    try:
        return _agent_config_digest(root)
    except TooLarge:
        return None


def _agent_config_digest(root):
    budget = _Budget()
    relatives = [name for name in AGENT_CONFIG_FILES]
    for top in AGENT_CONFIG_DIRS:
        base = os.path.join(root, top)
        if os.path.islink(base):
            relatives.append(top)
            continue
        for directory, directories, files in os.walk(base, followlinks=False):
            for name in directories:
                if os.path.islink(os.path.join(directory, name)):
                    files.append(name)
            relatives.extend(os.path.relpath(os.path.join(directory, name), root) for name in files)
            if len(relatives) > MAX_CONFIG_ENTRIES:
                raise TooLarge()
    digest = hashlib.sha256()
    for relative in sorted(relatives):
        budget.entry()
        entry = _entry(root, relative, budget)
        if entry is not None:
            digest.update(f"{relative}\0{entry}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _tree_entry(path):
    """A file of a tree for comparison: a link by its text, a file by its content. Links are not followed."""
    if os.path.islink(path):
        return f"link:{os.readlink(path)}"
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 16), b""):
                digest.update(block)
    except OSError:
        return "unreadable"
    return f"file:{digest.hexdigest()}"


def _files(root, ignore, committed):
    found = {}
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if os.path.islink(os.path.join(folder, name)):
                files.append(name)
        directories[:] = [name for name in directories if name != ".git" and name not in ignore
                          and not os.path.islink(os.path.join(folder, name))]
        for name in files:
            path = os.path.join(folder, name)
            if folder == root and environment_entry(root, name, committed):
                continue
            if name != ".git" and name not in ignore and (os.path.islink(path) or os.path.isfile(path)):
                relative = os.path.relpath(path, root)
                found[relative] = _tree_entry(path)
    return found


def changed_paths(base, worktree, ignore, committed):
    """Paths whose content differs between the base revision's tree and the worktree."""
    before, after = _files(base, ignore, committed), _files(worktree, ignore, committed)
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
    if os.path.commonpath([root, path]) != root or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


NAMESPACE_RE = re.compile(r"^[0-9a-f]{64}$")
UNIT_DIRECTORY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}-[0-9a-f]{16}(\.base)?$")


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
            if not pattern.match(name) or os.path.islink(path) or not os.path.isdir(path):
                found.append(path)
    if os.path.dirname(namespace) != root:
        found.append(namespace)
    return found
