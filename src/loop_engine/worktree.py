"""Read-only observation of a unit worktree: its agent configuration, its tree and what changed.

Hooks, MCP servers and extensions defined here run on the host with the user's
authority when an agent starts in this directory. The engine never interprets
them; it only detects that they differ from the moment the worktree was made.
"""
import hashlib
import os
import stat

from .check_runner import tree_digest  # the engine and the check program digest a tree the same way

AGENT_CONFIG_DIRS = (".claude", ".codex", ".pi", ".agents")
AGENT_CONFIG_FILES = (".mcp.json",)
AGENT_INSTRUCTION_FILES = ("CLAUDE.md", "AGENTS.md", "AGENTS.override.md")
MAX_LINK_DEPTH = 4


def _target(path, depth):
    """What a link resolves to: the content of a file, or of every file under a directory.

    A link's text can stay the same while what it points at changes, and that target can lie
    anywhere an agent may write. The digest therefore covers the target, not only the link.
    """
    resolved = os.path.realpath(path)
    if depth >= MAX_LINK_DEPTH or not os.path.exists(resolved):
        return "unresolved"
    digest = hashlib.sha256()
    if os.path.isfile(resolved):
        try:
            with open(resolved, "rb") as handle:
                for block in iter(lambda: handle.read(1 << 16), b""):
                    digest.update(block)
        except OSError:
            return "unreadable"
        return f"file:{digest.hexdigest()}"
    for directory, directories, files in os.walk(resolved, followlinks=False):
        directories.sort()
        for name in sorted([*files, *[d for d in directories if os.path.islink(os.path.join(directory, d))]]):
            inner = os.path.join(directory, name)
            content = _target(inner, depth + 1)
            digest.update(f"{os.path.relpath(inner, resolved)}\0{content}\0".encode("utf-8", "surrogateescape"))
    return f"dir:{digest.hexdigest()}"


def _entry(root, relative):
    path = os.path.join(root, relative)
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        return f"link:{os.readlink(path)}:{_target(path, 0)}"
    if not stat.S_ISREG(info.st_mode) or info.st_size == 0:
        # Agents and their sandboxes leave empty placeholder files; they define nothing.
        return None
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return f"file:{digest.hexdigest()}"


def agent_config_digest(root):
    """A digest of every non-empty file and link under the agent configuration paths."""
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
    digest = hashlib.sha256()
    for relative in sorted(relatives):
        entry = _entry(root, relative)
        if entry is not None:
            digest.update(f"{relative}\0{entry}\0".encode("utf-8", "surrogateescape"))
    return digest.hexdigest()


def _files(root, ignore):
    found = {}
    for folder, directories, files in os.walk(root, followlinks=False):
        for name in list(directories):
            if os.path.islink(os.path.join(folder, name)):
                files.append(name)
        directories[:] = [name for name in directories if name != ".git" and name not in ignore
                          and not os.path.islink(os.path.join(folder, name))]
        for name in files:
            path = os.path.join(folder, name)
            if name != ".git" and name not in ignore and (os.path.islink(path) or os.path.isfile(path)):
                relative = os.path.relpath(path, root)
                found[relative] = _entry(root, relative) or "empty"
    return found


def changed_paths(base, worktree, ignore):
    """Paths whose content differs between the base revision's tree and the worktree."""
    before, after = _files(base, ignore), _files(worktree, ignore)
    return sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))


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


def planted_configuration(worktrees, worktree):
    """Agent configuration or instruction files in the directories above a worktree.

    The worktree directory is writable by every agent on the machine. Some agents read
    configuration and instructions from parent directories, so anything of that kind between
    the worktree and the worktree root was put there by something other than the engine.
    """
    found = []
    top = os.path.realpath(worktrees)
    directory = os.path.dirname(os.path.realpath(worktree))
    while os.path.commonpath([top, directory]) == top:
        for name in (*AGENT_CONFIG_DIRS, *AGENT_CONFIG_FILES, *AGENT_INSTRUCTION_FILES):
            path = os.path.join(directory, name)
            if os.path.islink(path) or os.path.isdir(path) or (os.path.isfile(path) and os.path.getsize(path) > 0):
                found.append(path)
        if directory == top:
            break
        directory = os.path.dirname(directory)
    return found

