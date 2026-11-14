"""The fixed check program. An agent runs it inside its own sandbox:

    python -I check_runner.py <checks-file> <name>           the check on the candidate
    python -I check_runner.py <checks-file> <name> --base    the check on the base revision,
                                                              with only the candidate's tests

It runs the named check's argv from the engine-written checks file and ends
with one status line the engine reads from the provider's record. The line
carries the digest of the tree the check ran on, so the engine can tell a
check that ran before a later change. It takes no command from its caller.
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile

STATUS_PREFIX = "loop-engine-check"


def tree_digest(root, ignore):
    """Digest of every file under root except .git and the ignored names. The engine computes the same."""
    digest = hashlib.sha256()
    for folder, directories, files in os.walk(root, followlinks=False):
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
            if name == ".git" or name in ignore:
                continue
            path = os.path.join(folder, name)
            if not os.path.islink(path) and not os.path.isfile(path):
                continue  # a sandbox shows devices where it hides a path; they are not part of the tree
            digest.update(os.path.relpath(path, root).encode("utf-8", "surrogateescape") + b"\0")
            if os.path.islink(path):
                digest.update(b"link:" + os.readlink(path).encode("utf-8", "surrogateescape"))
            else:
                try:
                    with open(path, "rb") as handle:
                        for block in iter(lambda: handle.read(1 << 16), b""):
                            digest.update(block)
                except OSError:
                    digest.update(b"unreadable")
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


def _run_on_base(command, cwd, base, test_paths, ignore):
    """Run the check on a copy of the base revision whose tests are the candidate's."""
    with tempfile.TemporaryDirectory() as work:
        target = os.path.join(work, "tree")
        shutil.copytree(base, target, symlinks=True, ignore=shutil.ignore_patterns(".git", *ignore))
        for relative in test_paths:
            _replace(os.path.join(cwd, relative), os.path.join(target, relative), ignore)
        return subprocess.run(command, cwd=target, stdin=subprocess.DEVNULL).returncode


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
        command, test_paths = check["argv"], check["test_paths"]
        digest = tree_digest(cwd, ignore)  # before the check writes caches
    except (OSError, ValueError, KeyError, TypeError):
        print(f"{STATUS_PREFIX} {name} tree={tree} digest=none exit=127")
        return 127
    try:
        if tree == "candidate":
            code = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL).returncode
        else:
            code = _run_on_base(command, cwd, base, test_paths, ignore)
    except OSError as error:
        print(f"cannot run check: {error}", file=sys.stderr)
        code = 127
    sys.stdout.flush()
    sys.stderr.flush()
    print(f"\n{STATUS_PREFIX} {name} tree={tree} digest={digest} exit={code}", flush=True)
    return 0 if (code == 0) == (tree == "candidate") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
