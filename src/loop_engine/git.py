"""The only module that starts git. Fixed subcommands, with every path by which git
runs another program (hooks, filters, external diff, fsmonitor, pagers) turned off."""
import os
import subprocess

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SAFE = ("--no-pager", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
        "-c", "diff.external=", "-c", "core.attributesFile=/dev/null", "-c", "submodule.recurse=false",
        "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.sshCommand=false",
        "-c", "protocol.allow=never", f"--attr-source={EMPTY_TREE}")
TIMEOUT_SECONDS = 300


class GitError(RuntimeError):
    pass


def resolve_executable(name, path_variable, outside=()):
    """The first executable `name` on PATH, skipping relative entries and anything inside `outside`."""
    for directory in (path_variable or "").split(os.pathsep):
        if not os.path.isabs(directory):
            continue
        candidate = os.path.realpath(os.path.join(directory, name))
        if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
            continue
        if any(os.path.commonpath([candidate, os.path.realpath(root)]) == os.path.realpath(root) for root in outside):
            continue
        return candidate
    return None


class Git:
    def __init__(self, environ):
        self.executable = resolve_executable("git", environ.get("PATH"))
        if self.executable is None:
            raise GitError("git not found on PATH")
        self.env = {"PATH": environ.get("PATH", ""), "HOME": environ.get("HOME", "/"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
                    "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}

    def _run(self, cwd, *args, check=True):
        process = subprocess.run(
            [self.executable, *SAFE, *args], cwd=cwd, env=self.env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=TIMEOUT_SECONDS,
        )
        if check and process.returncode != 0:
            raise GitError(f"git {args[0]} failed: {process.stderr.decode(errors='replace')[:500]}")
        return process

    def toplevel(self, directory):
        return self._run(directory, "rev-parse", "--show-toplevel").stdout.decode().rstrip("\n")

    def common_dir(self, directory):
        raw = self._run(directory, "rev-parse", "--git-common-dir").stdout.decode().rstrip("\n")
        return os.path.realpath(os.path.join(directory, raw))

    def head(self, directory):
        return self._run(directory, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()

    def is_ancestor(self, directory, commit, descendant):
        """Whether `commit` exists and `descendant` was built on it."""
        return self._run(directory, "merge-base", "--is-ancestor", commit, descendant, check=False).returncode == 0

    def read_file(self, directory, commit, relative):
        """The bytes of a file at a commit, or None when the commit has no such file."""
        process = self._run(directory, "cat-file", "blob", f"{commit}:{relative}", check=False)
        return process.stdout if process.returncode == 0 else None

    def add_worktree(self, repository, path, commit):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._run(repository, "worktree", "add", "--detach", path, commit)

    def patch(self, worktree, base):
        """Everything the worktree differs from `base` by, untracked files included."""
        self._run(worktree, "add", "--all", "--", ".")
        return self._run(worktree, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", base).stdout
