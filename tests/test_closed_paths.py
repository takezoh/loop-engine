"""The paths by which an LLM or project content could run on the host stay closed."""
import ast
import os
import stat
import sys
import unittest

from engine_support import SRC, EngineCase, approve, git, produced, write

from loop_engine import git as engine_git

PACKAGE = os.path.join(SRC, "loop_engine")
MODULES = sorted(name for name in os.listdir(PACKAGE) if name.endswith(".py"))
POPEN = []
sys.addaudithook(lambda event, args: POPEN.append(args[0]) if event == "subprocess.Popen" else None)


def source(name):
    with open(os.path.join(PACKAGE, name), encoding="utf-8") as handle:
        return handle.read()


def imports(name):
    tree = ast.parse(source(name))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add((node.module or "").split(".")[0] if node.level == 0 else "." + (node.module or ""))
    calls = {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    return found, calls


class StructureTest(unittest.TestCase):
    def test_decisions_are_pure(self):
        for name in ("facts.py", "prompts.py"):
            found, calls = imports(name)
            self.assertLessEqual(found, {"re", "json", "shlex", "hashlib"}, name)  # computation only, no I/O
            self.assertFalse(calls & {"open", "print", "input", "exec", "eval", "compile", "__import__"}, name)

    def test_only_git_the_entry_client_and_the_check_program_start_processes(self):
        spawning = {name for name in MODULES if "subprocess" in imports(name)[0]}
        self.assertEqual({"git.py", "client.py", "check_runner.py"}, spawning)
        for name in MODULES:
            text = source(name)
            found, calls = imports(name)
            for forbidden in ("os.system", "os.exec", "os.spawn", "os.posix_spawn", "os.fork", "os.popen",
                              "shell=True", "pty."):
                self.assertNotIn(forbidden, text, name)
            self.assertFalse(found & {"importlib", "runpy", "pickle", "marshal", "ctypes", "code"}, name)
            self.assertFalse(calls & {"exec", "eval", "compile", "__import__"}, name)

    def test_engine_owns_no_sandbox_and_passes_agents_no_sandbox_or_approval_setting(self):
        for name in MODULES:
            text = source(name).lower()
            for word in ("bwrap", "bubblewrap", "unshare", "seccomp", "landlock", "approvalpolicy", "approval_policy",
                         "sandbox_policy", "dangerously", "bypass", "skip-permissions", "danger-full-access"):
                self.assertNotIn(word, text, f"{name}: {word}")

    def test_entries_hold_no_progress_judgment_or_recording_logic(self):
        for name in ("cli.py", "client.py"):
            found, _calls = imports(name)
            self.assertFalse(found & {".facts", ".unit", ".service", ".git", ".prompts", ".composition"}, name)

    def test_only_the_composition_touches_the_provider_transport(self):
        for name in MODULES:
            if name != "composition.py":
                self.assertNotIn("cross_host_transport", source(name), name)

    def test_git_arguments_always_carry_the_safe_prefix(self):
        for needle in ("core.hooksPath=/dev/null", "core.fsmonitor=false", "diff.external=",
                       "core.attributesFile=/dev/null", f"--attr-source={engine_git.EMPTY_TREE}"):
            self.assertIn(needle, engine_git.SAFE)
        tree = ast.parse(source("git.py"))
        runs = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute) and node.func.attr == "run"]
        self.assertEqual(1, len(runs))
        self.assertEqual("[self.executable, *SAFE, *args]", ast.unparse(runs[0].args[0]))

    def test_executable_resolution_skips_relative_entries_and_the_given_roots(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            inside, outside = os.path.join(directory, "repo", "bin"), os.path.join(directory, "bin")
            for folder in (inside, outside):
                os.makedirs(folder)
                path = os.path.join(folder, "tool")
                write(folder, "tool", "#!/bin/sh\n")
                os.chmod(path, 0o755)
            path_variable = os.pathsep.join([".", "relative/bin", inside, outside])
            self.assertEqual(os.path.realpath(os.path.join(outside, "tool")), engine_git.resolve_executable(
                "tool", path_variable, outside=(os.path.join(directory, "repo"),)))
            self.assertIsNone(engine_git.resolve_executable("tool", ".:relative/bin"))


class RepositoryProgramsTest(EngineCase):
    """A repository that tries every way git offers to run a program during the engine's git use."""

    def seed(self):
        self.marker = os.path.join(os.path.dirname(self.repo), "ran")
        os.makedirs(self.marker)
        script = os.path.join(os.path.dirname(self.repo), "evil.sh")
        with open(script, "w") as handle:
            handle.write(f"#!/bin/sh\ntouch {self.marker}/$(basename $0)-$$\ncat\n")
        os.chmod(script, stat.S_IRWXU)
        write(self.repo, ".gitattributes", "* filter=evil diff=evil\n")
        for key, value in (("filter.evil.clean", script), ("filter.evil.smudge", script),
                           ("filter.evil.process", script), ("diff.evil.textconv", script),
                           ("diff.evil.command", script), ("diff.external", script),
                           ("core.fsmonitor", script), ("core.pager", script), ("core.editor", script)):
            git(self.repo, "config", key, value)
        hooks = os.path.join(self.repo, ".git", "hooks")
        for hook in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
            with open(os.path.join(hooks, hook), "w") as handle:
                handle.write(f"#!/bin/sh\ntouch {self.marker}/hook-{hook}\n")
            os.chmod(os.path.join(hooks, hook), stat.S_IRWXU)

    def test_no_repository_program_runs_and_only_git_is_started(self):
        # The trap is live: the plain git of the fixture's own commit ran the repository's programs.
        self.assertTrue(os.listdir(self.marker))
        for name in os.listdir(self.marker):
            os.unlink(os.path.join(self.marker, name))

        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            write(request.cwd, ".gitattributes", "* filter=evil diff=evil\n*.txt filter=evil\n")
            return produced(request, commands)
        del POPEN[:]
        engine, status = self.start(script)
        started = list(POPEN)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual([], os.listdir(self.marker))
        self.assertTrue(started)
        self.assertEqual({engine.git.executable}, set(started))
        self.assertIn(b"+made by the producer", self.result("patch.diff"))


if __name__ == "__main__":
    unittest.main()
