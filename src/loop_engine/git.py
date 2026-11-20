"""The only module that starts git. Fixed subcommands, with every path by which git
runs another program (hooks, filters, external diff, fsmonitor, pagers) turned off."""
import os
import subprocess

from .facts import inside
from .platform_facts import CURRENT

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
SAFE = ("--no-pager", "-c", f"core.hooksPath={CURRENT.null_device}", "-c", "core.fsmonitor=false",
        "-c", "diff.external=", "-c", f"core.attributesFile={CURRENT.null_device}", "-c", "submodule.recurse=false",
        "-c", "gc.auto=0", "-c", "maintenance.auto=false", "-c", "core.sshCommand=false",
        "-c", "protocol.allow=never", f"--attr-source={EMPTY_TREE}")
TIMEOUT_SECONDS = 300


class GitError(RuntimeError):
    pass


def resolve_executable(name, path_variable, outside=(), suffixes=("",)):
    """The first executable `name` on PATH, as a file named `name` plus one of `suffixes`,
    skipping relative entries and anything inside `outside`."""
    for directory in (path_variable or "").split(os.pathsep):
        if not os.path.isabs(directory):
            continue
        for suffix in suffixes:
            candidate = os.path.realpath(os.path.join(directory, name + suffix))
            if not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
                continue
            if any(inside(candidate, os.path.realpath(root), CURRENT.path_style) for root in outside):
                continue
            return candidate
    return None


class Git:
    def __init__(self, environ):
        self.executable = resolve_executable("git", environ.get("PATH"), suffixes=CURRENT.program_suffixes)
        if self.executable is None:
            raise GitError("git not found on PATH")
        home = environ.get("HOME", CURRENT.home)
        self.env = {"PATH": environ.get("PATH", ""), **({"HOME": home} if home is not None else {}),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": CURRENT.null_device,
                    "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}

    def _run(self, cwd, *args, check=True, data=None):
        given = {"stdin": subprocess.DEVNULL} if data is None else {"input": data}
        process = subprocess.run(
            [self.executable, *SAFE, *args], cwd=cwd, env=self.env, capture_output=True, timeout=TIMEOUT_SECONDS,
            **given,
        )
        if check and process.returncode != 0:
            name = next((arg for arg in args if not arg.startswith("-")), args[0])
            raise GitError(f"git {name} failed: {process.stderr.decode(errors='replace')[:500]}")
        return process

    def toplevel(self, directory):
        """The top directory of the work tree, in this machine's path form (git prints "/" on every platform)."""
        return os.path.normpath(self._run(directory, "rev-parse", "--show-toplevel").stdout.decode().rstrip("\n"))

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

    def top_names(self, directory, commit):
        """The names in the top directory of a commit's tree."""
        listed = self._run(directory, "ls-tree", "-z", "--name-only", f"{commit}^{{tree}}").stdout
        return sorted(os.fsdecode(name) for name in listed.split(b"\0") if name)

    def tree_files(self, worktree, git_dir, commit):
        """(object format, {path: ("file" | "link", blob id)}): git's record of every file of a commit.

        Read through the git directory fixed when the worktree was made. Submodule entries are not files.
        """
        at = (f"--git-dir={git_dir}", f"--work-tree={worktree}")
        object_format = self._run(worktree, *at, "rev-parse", "--show-object-format").stdout.decode().strip()
        listed = self._run(worktree, *at, "ls-tree", "-r", "-z", "--full-tree", f"{commit}^{{tree}}").stdout
        files = {}
        for item in listed.split(b"\0"):
            if not item:
                continue
            meta, path = item.split(b"\t", 1)
            mode, kind, oid = meta.split(b" ")
            if kind == b"blob":
                files[os.fsdecode(path)] = ("link" if mode == b"120000" else "file", oid.decode())
        return object_format, files

    def add_worktree(self, repository, path, commit):
        """Create a worktree and return its git directory inside the repository.

        The directory is read here, before any agent has run in the worktree. Later git calls
        name it explicitly, so a `.git` file an agent rewrites is never followed.
        """
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._run(repository, "worktree", "add", "--detach", path, commit)
        git_dir = self._run(path, "rev-parse", "--absolute-git-dir").stdout.decode().rstrip("\n")
        owned = os.path.join(os.path.realpath(self.common_dir(repository)), "worktrees")
        if os.path.dirname(os.path.realpath(git_dir)) != owned:
            raise GitError("the new worktree's git directory is not inside the repository")
        return os.path.realpath(git_dir)

    def prune_worktrees(self, repository):
        """Forget worktrees whose directories are gone."""
        self._run(repository, "worktree", "prune", check=False)

    def ignored(self, work_tree, git_dir, paths, index):
        """The paths among `paths` that the ignore rules of `work_tree` exclude.

        With `index`, git applies the rules as it does to the worktree: a path in the index is never
        excluded. Without it, only the rules decide (`--no-index`).
        """
        if not paths:
            return set()
        at = (f"--git-dir={git_dir}", f"--work-tree={work_tree}")
        process = self._run(work_tree, *at, "check-ignore", *(() if index else ("--no-index",)), "-z", "--stdin",
                            check=False, data=b"".join(os.fsencode(path) + b"\0" for path in paths))
        if process.returncode not in (0, 1):  # 1: none of the paths is ignored
            raise GitError(f"git check-ignore failed: {process.stderr.decode(errors='replace')[:500]}")
        return {os.fsdecode(path) for path in process.stdout.split(b"\0") if path}

    def patch(self, worktree, base, git_dir, left_out, links=()):
        """Everything the worktree differs from `base` by, untracked files included, except `left_out`.

        `left_out` are top-directory names the environment placed in the worktree: they are taken out
        of the index too, so one that was counted before it became such an entry is not kept there.
        `links` are the worktree's links (relative, with "/"). Each is recorded by its text, as git records
        a symbolic link, and never added through: git would follow a junction into what it points at.
        """
        at = (f"--git-dir={git_dir}", f"--work-tree={worktree}")
        self._run(worktree, *at, "add", "--all", "--", ".", *(f":(exclude,literal){link}" for link in links))
        if left_out:
            self._run(worktree, *at, "--literal-pathspecs", "rm", "--cached", "--quiet", "--ignore-unmatch",
                      "--", *left_out)
        for link in links:
            self._run(worktree, *at, "--literal-pathspecs", "rm", "-r", "--cached", "--quiet", "--ignore-unmatch",
                      "--", link)
            text = os.fsencode(os.readlink(os.path.join(worktree, *link.split("/"))))
            blob = self._run(worktree, *at, "hash-object", "-w", "--stdin", data=text).stdout.decode().strip()
            self._run(worktree, *at, "update-index", "--add", "--cacheinfo", f"120000,{blob},{link}")
        return self._run(worktree, *at, "diff", "--cached", "--binary", "--no-ext-diff", "--no-textconv", base).stdout
