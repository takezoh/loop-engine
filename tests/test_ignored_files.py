"""New files that git ignores by the base revision's rules and by the worktree's own are not changes."""
import os

from engine_support import EngineCase, approve, git, produced, write

from loop_engine import store

BASE_IGNORE = "node_modules/\n*.log\n"


def patched_paths(patch):
    return sorted(line.split(" b/", 1)[1] for line in patch.decode().splitlines() if line.startswith("diff --git "))


class IgnoredFilesTest(EngineCase):
    def seed(self):
        write(self.repo, ".gitignore", BASE_IGNORE)
        write(self.repo, "tracked.log", "kept in the base revision although the rules ignore it\n")
        git(self.repo, "add", "--force", "tracked.log")

    def run_unit(self, make, unit_id="u1", scope=("out.txt",)):
        """A writer that runs `make` in the worktree before writing out.txt, and a reviewer that approves."""
        def script(request, commands, cancelled):
            if request.role == "reviewer":
                return approve(request)
            make(request.cwd)
            return produced(request, commands)
        contract = self.contract(unit_id=unit_id, unit={"goal": "make out.txt", "scope": list(scope),
                                                        "criteria": [{"id": "c1", "text": "out.txt is made"}]})
        contract["facts"].append({"id": "in-scope", "required": True, "by": "git", "subject": "candidate"})
        contract["executions"][1]["reads"].append("in-scope")
        _engine, status = self.start(script, **contract)
        changed = store.read_json(self.unit(unit_id).path("state.json"))["changed"]
        return status["stop_reason"], changed, patched_paths(self.result("patch.diff", unit_id))

    def test_files_the_base_revisions_rules_ignore_are_neither_changes_nor_in_the_patch(self):
        def install(cwd):
            for name in ("node_modules/pkg/index.js", "node_modules/.bin/tool", "deep/node_modules/x.js", "run.log"):
                write(cwd, name, "generated\n")
        self.assertEqual(("done", ["out.txt"], ["out.txt"]), self.run_unit(install))

    def test_a_new_directory_that_ignores_itself_is_a_change_although_the_patch_leaves_it_out(self):
        def hide(cwd):
            write(cwd, "hidden/.gitignore", "*\n")
            write(cwd, "hidden/conftest.py", "planted\n")
        stop, changed, patched = self.run_unit(hide)
        self.assertNotEqual("done", stop)
        self.assertEqual(["hidden/.gitignore", "hidden/conftest.py", "out.txt"], changed)
        self.assertEqual(["out.txt"], patched)

    def test_a_file_that_an_edited_gitignore_stops_ignoring_is_a_change_and_in_the_patch(self):
        def keep(cwd):
            with open(os.path.join(cwd, ".gitignore"), "a") as handle:
                handle.write("!keep.log\n")
            write(cwd, "keep.log", "delivered\n")
        stop, changed, patched = self.run_unit(keep, scope=("out.txt", ".gitignore"))
        self.assertNotEqual("done", stop)
        self.assertEqual([".gitignore", "keep.log", "out.txt"], changed)
        self.assertEqual([".gitignore", "keep.log", "out.txt"], patched)

    def test_a_file_the_base_revision_has_is_a_change_whatever_the_rules_say(self):
        def edit(cwd):
            write(cwd, "tracked.log", "changed by the writer\n")
        stop, changed, patched = self.run_unit(edit)
        self.assertNotEqual("done", stop)
        self.assertEqual(["out.txt", "tracked.log"], changed)
        self.assertEqual(["out.txt", "tracked.log"], patched)
