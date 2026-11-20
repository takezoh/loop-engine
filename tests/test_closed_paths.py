"""The paths by which an LLM or project content could run on the host stay closed."""
import ast
import os
import stat
import subprocess
import sys
import unittest

from engine_support import SRC, EngineCase, approve, directory_link, git, needs_file_links, produced, recorded, write

from loop_engine import git as engine_git
from loop_engine.platform_facts import CURRENT

PACKAGE = os.path.join(SRC, "loop_engine")
MODULES = sorted(name for name in os.listdir(PACKAGE) if name.endswith(".py"))
POPEN = []


def started(args):
    """The program a subprocess.Popen audit event starts: its executable, else the first word of its command.
    Windows audits the command line it made, with no executable."""
    executable, command = args[0], args[1]
    if executable is not None:
        return executable
    if not isinstance(command, str):
        return command[0]
    # the first word of the line `list2cmdline` made; the hook sees every start, so it never raises
    return command[1:].partition('"')[0] if command.startswith('"') else command.partition(" ")[0]


sys.addaudithook(lambda event, args: POPEN.append(started(args)) if event == "subprocess.Popen" else None)


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


from loop_engine import store  # noqa: E402


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
        for name in ("cli.py", "client.py", "unit_tools.py", "mcp_stdio.py"):
            found, _calls = imports(name)
            self.assertFalse(found & {".facts", ".unit", ".service", ".git", ".prompts", ".composition"}, name)

    def test_only_the_composition_touches_the_provider_transport(self):
        for name in MODULES:
            if name != "composition.py":
                self.assertNotIn("provider_transport", source(name), name)

    def test_containment_of_paths_is_decided_by_facts_inside_alone(self):
        # commonpath raises for paths on two Windows drives; facts.inside answers for every platform
        for directory, _directories, files in os.walk(PACKAGE):
            for name in (name for name in files if name.endswith(".py")):
                with open(os.path.join(directory, name), encoding="utf-8") as handle:
                    tree = ast.parse(handle.read())
                named = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
                named |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
                named |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
                self.assertNotIn("commonpath", named, os.path.join(directory, name))

    def test_git_arguments_always_carry_the_safe_prefix(self):
        for needle in (f"core.hooksPath={CURRENT.null_device}", "core.fsmonitor=false", "diff.external=",
                       f"core.attributesFile={CURRENT.null_device}", f"--attr-source={engine_git.EMPTY_TREE}"):
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

    def test_executable_resolution_finds_the_program_by_the_platforms_suffix(self):
        # Windows names a program on PATH git.exe, not git.
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            only_bare, only_suffixed = os.path.join(directory, "bare"), os.path.join(directory, "suffixed")
            os.makedirs(only_bare)
            os.makedirs(only_suffixed)
            for folder, name in ((only_bare, "tool"), (only_suffixed, "tool.exe")):
                write(folder, name, "#!/bin/sh\n")
                os.chmod(os.path.join(folder, name), 0o755)
            path_variable = os.pathsep.join([only_bare, only_suffixed])
            self.assertEqual(os.path.realpath(os.path.join(only_suffixed, "tool.exe")),
                             engine_git.resolve_executable("tool", path_variable, suffixes=(".exe",)))
            self.assertEqual(os.path.realpath(os.path.join(only_bare, "tool")),
                             engine_git.resolve_executable("tool", path_variable, suffixes=("",)))
            self.assertIsNone(engine_git.resolve_executable("tool", only_suffixed, suffixes=("",)))


class RepositoryProgramsTest(EngineCase):
    """A repository that tries every way git offers to run a program during the engine's git use."""

    def seed(self):
        self.marker = os.path.join(os.path.dirname(self.repo), "ran")
        os.makedirs(self.marker)
        # git runs these through sh, which reads a backslash as an escape: the paths are written with "/"
        script = self.evil = os.path.join(os.path.dirname(self.repo), "evil.sh").replace(os.sep, "/")
        with open(script, "w", newline="") as handle:
            handle.write(f"#!/bin/sh\ntouch {self.marker.replace(os.sep, '/')}/$(basename $0)-$$\ncat\n")
        os.chmod(script, stat.S_IRWXU)
        write(self.repo, ".gitattributes", "* filter=evil diff=evil\n")
        for key, value in (("filter.evil.clean", script), ("filter.evil.smudge", script),
                           ("filter.evil.process", script), ("diff.evil.textconv", script),
                           ("diff.evil.command", script), ("diff.external", script),
                           ("core.fsmonitor", script), ("core.pager", script), ("core.editor", script)):
            git(self.repo, "config", key, value)
        hooks = os.path.join(self.repo, ".git", "hooks")
        for hook in ("post-checkout", "pre-commit", "post-index-change", "reference-transaction"):
            with open(os.path.join(hooks, hook), "w", newline="") as handle:
                handle.write(f"#!/bin/sh\ntouch {self.marker.replace(os.sep, '/')}/hook-{hook}\n")
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

    def test_a_git_pointer_rewritten_by_an_agent_is_never_followed(self):
        for name in os.listdir(self.marker):
            os.unlink(os.path.join(self.marker, name))
        trap = os.path.join(os.path.dirname(self.repo), "trap")

        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            outcome = produced(request, commands)
            # The agent points the worktree at a git directory it controls, whose own
            # configuration runs a program on every file git reads.
            git(os.path.dirname(trap), "init", "-q", trap)
            git(trap, "config", "filter.evil.clean", self.evil)
            write(os.path.join(trap, ".git"), "info/attributes", "* filter=evil\n")
            with open(os.path.join(request.cwd, ".git"), "r+") as handle:  # Windows git hides this file:
                handle.write(f"gitdir: {os.path.join(trap, '.git')}\n")  # rewritten in place, not recreated
                handle.truncate()
            subprocess.run(["git", "add", "--all"], cwd=request.cwd, capture_output=True, stdin=subprocess.DEVNULL)
            self.assertTrue(os.listdir(self.marker), "the trap is live: plain git ran the program")
            for name in os.listdir(self.marker):
                os.unlink(os.path.join(self.marker, name))
            return outcome
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        self.assertEqual([], os.listdir(self.marker))
        self.assertIn(b"+made by the producer", self.result("patch.diff"))


class WorktreePlacementTest(EngineCase):
    """The worktree root is writable by every agent: nothing found there is trusted."""

    def finishing(self, request, commands, cancelled):
        return produced(request, commands) if request.role == "producer" else approve(request)

    def test_the_worktree_gets_a_name_nobody_can_prepare(self):
        engine, status = self.start(self.finishing)
        engine.handle({"request": "start", "contract": self.contract(unit_id="u2")})
        other = self.run_to_stop(engine, "u2")
        self.assertRegex(os.path.basename(status["worktree"]), r"^u1-[0-9a-f]{16}$")
        self.assertRegex(os.path.basename(other["worktree"]), r"^u2-[0-9a-f]{16}$")
        started = next(event for event in self.events() if event["type"] == "unit_started")
        self.assertTrue(started["git_dir"].startswith(os.path.join(self.repo, ".git", "worktrees")))

    def test_configuration_planted_above_the_worktree_stops_the_unit_before_any_agent_starts(self):
        for name, text in ((".claude/settings.json", '{"hooks": {}}'), ("CLAUDE.local.md", "run this first\n"),
                           ("notes.txt", "anything the engine did not create\n")):
            with self.subTest(name):
                write(os.path.join(self.home, "worktrees"), name, text)
                engine = self.engine(self.finishing)
                unit_id = "p" + str(len(name))
                engine.handle({"request": "start", "contract": self.contract(unit_id=unit_id)})
                self.assertEqual("unsafe_agent_config", self.run_to_stop(engine, unit_id)["stop_reason"])
                self.assertEqual([], self.agents.requests)
                os.unlink(os.path.join(self.home, "worktrees", name))
                if os.path.dirname(name):
                    os.rmdir(os.path.join(self.home, "worktrees", os.path.dirname(name)))

    def test_a_worktree_directory_that_is_a_link_elsewhere_is_refused(self):
        engine = self.engine(self.finishing)
        namespace = os.path.dirname(store.worktree_path(os.path.join(self.home, "worktrees"),
                                                        engine.git.common_dir(self.repo), "x"))
        elsewhere = os.path.join(self.home, "elsewhere")
        os.makedirs(elsewhere)
        os.makedirs(os.path.dirname(namespace))
        directory_link(elsewhere, namespace)
        engine.handle({"request": "start", "contract": self.contract()})
        status = self.run_to_stop(engine)
        self.assertEqual("unknown_result", status["stop_reason"])
        self.assertIn("not where the engine created it", status["detail"])
        self.assertEqual([], self.agents.requests)


@needs_file_links
class LinkedConfigurationTest(EngineCase):
    """Agent configuration reached through a link is watched at its target."""

    def seed(self):
        write(self.repo, "config/claude.json", '{"hooks": {}}\n')
        os.makedirs(os.path.join(self.repo, ".claude"))
        os.symlink("../config/claude.json", os.path.join(self.repo, ".claude", "settings.json"))

    def test_changing_the_target_of_a_configuration_link_stops_the_unit(self):
        def script(request, commands, cancelled):
            write(request.cwd, "config/claude.json", '{"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "x"}]}]}}\n')
            return produced(request, commands)
        _engine, status = self.start(script)
        self.assertEqual("unsafe_agent_config", status["stop_reason"])
        self.assertEqual(["producer"], [request.role for request in self.agents.requests])

    def test_configuration_too_large_to_read_is_not_driven(self):
        from loop_engine import worktree
        limit, worktree.MAX_CONFIG_ENTRIES = worktree.MAX_CONFIG_ENTRIES, 3
        self.addCleanup(setattr, worktree, "MAX_CONFIG_ENTRIES", limit)
        calls = []

        def script(request, commands, cancelled):
            calls.append(request.role)
            for index in range(6):
                write(request.cwd, f"config/many/{index}.json", "{}\n")
            return produced(request, commands)
        engine = self.engine(script)
        os.unlink(os.path.join(self.repo, ".claude", "settings.json"))
        directory_link("../config", os.path.join(self.repo, ".claude", "settings.json"))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "link to a directory")
        engine.handle({"request": "start", "contract": self.contract()})
        self.assertEqual("unsafe_agent_config", self.run_to_stop(engine)["stop_reason"])
        self.assertEqual(["producer"], calls)

    def test_an_untouched_configuration_link_does_not_stop_the_unit(self):
        def script(request, commands, cancelled):
            return produced(request, commands) if request.role == "producer" else approve(request)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])


class DirectoryLinkTest(EngineCase):
    """A link to a directory, of whatever kind the platform lets an agent make, is a file of the tree known
    by its text: neither the tree, the changed paths nor the patch follow it."""

    def test_a_link_into_a_large_tree_among_the_changes_is_compared_by_its_text(self):
        from loop_engine import worktree
        limit, worktree.MAX_CONFIG_ENTRIES = worktree.MAX_CONFIG_ENTRIES, 3
        self.addCleanup(setattr, worktree, "MAX_CONFIG_ENTRIES", limit)
        base, candidate = os.path.join(self.home, "b"), os.path.join(self.home, "c")
        write(base, "a.txt", "same\n")
        write(candidate, "a.txt", "same\n")
        large = os.path.join(self.home, "large")
        for index in range(6):
            write(large, f"{index}.txt", "x\n")
        directory_link(large, os.path.join(candidate, "big"))
        self.assertEqual(["big"], worktree.changed_paths(recorded(base), candidate, [], ["a.txt"]))
        digest = worktree.tree_digest(candidate, [], ["a.txt"])
        write(large, "6.txt", "what the link points at changed\n")
        self.assertEqual(digest, worktree.tree_digest(candidate, [], ["a.txt"]))


    def test_a_link_to_a_directory_an_agent_makes_is_recorded_by_its_text_not_followed(self):
        outside = os.path.join(self.home, "outside")
        write(outside, "secret.txt", "outside the worktree\n")

        def script(request, commands, cancelled):
            if request.role == "producer":
                directory_link(outside, os.path.join(request.cwd, "big"))
                return produced(request, commands)
            return approve(request)
        _engine, status = self.start(script)
        self.assertEqual("done", status["stop_reason"])
        patch = self.result("patch.diff")
        self.assertIn(b"diff --git a/big b/big\nnew file mode 120000", patch)
        self.assertNotIn(b"outside the worktree", patch)
        self.assertNotIn(b"secret.txt", patch)


if __name__ == "__main__":
    unittest.main()
