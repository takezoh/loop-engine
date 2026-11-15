"""The fixed check program. An agent runs it inside its own sandbox:

    python -I check_runner.py <checks-file> <name>           the check on the candidate
    python -I check_runner.py <checks-file> <name> --base    the check on the base revision,
                                                              with only the candidate's tests

    loop-engine-check <name> tree=candidate digest=<candidate> exit=<code>
    loop-engine-check <name> tree=base digest=<candidate> base=<base revision as copied> exit=<code>

It runs the named check's argv from the engine-written checks file and ends
with one status line the engine reads from the provider's record. The line
carries the digest of the candidate tree the check took its files from (with
--base, its tests), so the engine can tell a check that ran before a later
change. On the base revision the line also carries the digest of the base
revision as the program copied it, so the engine can tell a copy that is not
the one it placed. A tree it cannot read in full, or a check that cannot run on
the base revision, gives the digest `none`, which no judgment accepts. It takes
no command from its caller.

The engine publishes this file into its state home and names that copy in the
commands it gives; the program needs nothing but the standard library.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile

STATUS_PREFIX = "loop-engine-check"


def environment_entry(root, name, committed):
    """Whether `name`, in the top directory of `root`, is an entry the environment placed there.

    Such an entry is a regular file, not a link, of size 0 that the evaluating process cannot write,
    whose name is not in `committed`: the names the top directory has in the base revision or in the
    commit the worktree was made from. It holds no content and is not part of the tree: neither its
    digest, nor the changed paths, nor the patch. A file that is not empty, is writable, is a link,
    lies below the top directory or has a committed name is part of the tree as any other file.
    """
    if name in committed:
        return False
    path = os.path.join(root, name)
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and info.st_size == 0 and not os.access(path, os.W_OK)


class Unreadable(OSError):
    """A path of the tree the evaluating process cannot read: the tree cannot be judged."""


def refuse(error):
    """Raise the error as `Unreadable`: what cannot be read makes the judgment fail, not the tree shrink."""
    raise Unreadable(error.errno, error.strerror, error.filename) from error


def tree_files(root, ignore, committed):
    """(relative path, path, lstat) of every file of the tree under root, in a fixed order.

    The tree is every file and link except .git, the ignored names and the environment's entries; a
    link to a directory is a file of the tree and is not followed. Anything the process cannot list or
    stat raises `Unreadable`: a path left out because it could not be read would shrink the tree.
    """
    for folder, directories, files in os.walk(root, followlinks=False, onerror=refuse):
        kept = []
        for name in sorted(directories):
            if name == ".git" or name in ignore:
                continue
            if os.path.islink(os.path.join(folder, name)):
                files.append(name)
            else:
                kept.append(name)
        directories[:] = kept
        for name in sorted(files):
            if name == ".git" or name in ignore or (folder == root and environment_entry(root, name, committed)):
                continue
            path = os.path.join(folder, name)
            try:
                info = os.lstat(path)
            except OSError as error:
                refuse(error)
            if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode):
                yield os.path.relpath(path, root), path, info
            # a sandbox shows devices where it hides a path; they are not part of the tree


def read_blocks(path):
    """The content of a file in blocks; a file that cannot be read raises `Unreadable`."""
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 16), b""):
                yield block
    except OSError as error:
        refuse(error)


def tree_digest(root, ignore, committed):
    """Digest of the tree under root (see `tree_files`); raises `Unreadable`. The engine computes the same."""
    digest = hashlib.sha256()
    for relative, path, info in tree_files(root, ignore, committed):
        digest.update(relative.encode("utf-8", "surrogateescape") + b"\0")
        if stat.S_ISLNK(info.st_mode):
            try:
                digest.update(b"link:" + os.readlink(path).encode("utf-8", "surrogateescape"))
            except OSError as error:
                refuse(error)
        else:
            for block in read_blocks(path):
                digest.update(block)
        digest.update(b"\0")
    return digest.hexdigest()[:32]


def _replace(source, target, ignore):
    if os.path.isdir(target) and not os.path.islink(target):
        shutil.rmtree(target)
    elif os.path.lexists(target):
        os.remove(target)
    if os.path.isdir(source) and not os.path.islink(source):
        # caches compiled from an earlier version of a test must not travel with it
        shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git", *ignore))
    elif os.path.lexists(source):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)


def _run_on_base(command, cwd, base, test_paths, ignore, committed):
    """Run the check on a copy of the base revision whose tests are the candidate's.

    Returns (exit code, digest of the copied base revision before the tests replaced its own).
    """
    with tempfile.TemporaryDirectory() as work:
        target = os.path.join(work, "tree")
        shutil.copytree(base, target, symlinks=True, ignore=shutil.ignore_patterns(".git", *ignore))
        base_digest = tree_digest(target, ignore, committed)
        for relative in test_paths:
            _replace(os.path.join(cwd, relative), os.path.join(target, relative), ignore)
        return subprocess.run(command, cwd=target, stdin=subprocess.DEVNULL).returncode, base_digest


def main(argv):
    if len(argv) not in (2, 3) or (len(argv) == 3 and argv[2] != "--base"):
        print("usage: check_runner.py <checks-file> <name> [--base]", file=sys.stderr)
        return 2
    path, name = argv[0], argv[1]
    tree = "base" if len(argv) == 3 else "candidate"
    try:
        with open(path, encoding="utf-8") as handle:
            config = json.load(handle)
        check, cwd, base, ignore = config["checks"][name], config["cwd"], config["base"], config["ignore"]
        command, test_paths, committed = check["argv"], check["test_paths"], frozenset(config["committed"])
        digest = tree_digest(cwd, ignore, committed)  # before the check writes caches
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"cannot read the tree or the checks file: {error}", file=sys.stderr)
        print(f"{STATUS_PREFIX} {name} tree={tree} digest=none exit=127")
        return 127
    line = f"{STATUS_PREFIX} {name} tree={tree} digest={digest}"
    try:
        if tree == "candidate":
            code = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL).returncode
        else:
            code, base_digest = _run_on_base(command, cwd, base, test_paths, ignore, committed)
            line += f" base={base_digest}"
    except OSError as error:
        print(f"cannot run check: {error}", file=sys.stderr)
        if tree == "base":
            # a check that could not run on the base revision has not shown that a test fails there
            print(f"\n{STATUS_PREFIX} {name} tree={tree} digest=none exit=127", flush=True)
            return 127
        code = 127
    sys.stdout.flush()
    sys.stderr.flush()
    print(f"\n{line} exit={code}", flush=True)
    return 0 if (code == 0) == (tree == "candidate") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
