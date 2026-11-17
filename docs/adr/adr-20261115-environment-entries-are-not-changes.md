---
id: adr-20261115-environment-entries-are-not-changes
kind: adr
title: Empty read-only files that the environment places in the working directory are not counted as a role's changes
status: accepted
created: '2026-11-15'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-checks-judged-from-execution-record}
source_paths: []
summary: An empty, unwritable regular file at the worktree top named in neither the base revision nor the commit the
  worktree came from is not in the candidate, changed paths, scope, non-writing-role comparison, patch.diff or check
  tree digest. The engine neither knows sandbox paths nor alters the worktree.
updated: '2026-11-15'
---

# Empty read-only files that the environment places in the working directory are not counted as a role's changes

## Context

The user's agents run under a shared sandbox policy. When a path the policy protects is not in the working directory, the agent's host creates that path in the working directory. In measurements on 2026-11-15 with Codex (sending only `thread/start` to `codex app-server` and starting no turn), the following appeared in the thread's cwd in many runs and disappeared neither on unsubscribe nor on a normal exit of the app-server.

- Empty directories: `.agents`, `.aws`, `.codex`
- Empty files: `.pi`, `AGENTS.md`, `AGENTS.override.md`

Seen from the host, these files are regular files of size 0 with mode `0444`, owned by the user. Seen from inside the agent's sandbox, the same paths are files of size 0 with mode `0600` on a read-only mount, and the directories are empty `dr-xr-xr-x` tmpfs. The mode differs between the two views. What agrees is that each is a regular file, that its size is 0, and that the process looking at it cannot write it.

The engine counted these as a role's changes. In the records from a real machine (2026-11-15), the following happened.

- A turn of a role that does not write is judged as `a-turn-that-must-not-write-changed-the-worktree`.
- The writer's changes are judged out of scope, and three empty files (`new file mode 100644 index 0000000..e69de29`) enter `patch.diff`.

In the same records the check facts did not hold, but not because of these entries. The path of the check program the agent ran was not visible from the agent's sandbox, and every run failed without printing a status line (`can't open file '.../loop_engine/check_runner.py'`). However, when the entries exist, the tree digest the check program computes inside the sandbox and the engine's digest disagree if the entries cannot be read inside the sandbox.

## Proposed decision

The user's judgment (2026-11-15):

> "It is normal for the environment to place entries in the working directory, and it is the engine that is wrong to count them as a role's changes" (translated from Japanese)

1. **An environment entry is defined as follows.** It is a name in the top directory of the worktree that satisfies all of the following.
   - The name is in neither the top directory of the base revision nor the top directory of the commit the worktree was made from.
   - It is a regular file (not a symbolic link).
   - Its size is 0.
   - The judging process cannot write it.
2. **An environment entry is included in none of the following.** The candidate tree and its digest, the changed paths, the in-scope judgment, the before-and-after comparison of a turn of a role that does not write, `patch.diff`, and the tree digest that a check's status line is bound to.
3. **Everything else is counted as before.** Writable empty files, non-empty read-only files, files whose name is in either commit (including one that was truncated and made read-only), symbolic links, files below the top directory, and every change to a path that is in the base revision. Empty directories were never counted, by git or by the tree digest.
4. **The judgment is defined in one place, and the engine and the check program evaluate it with the same input.** The engine reads the top-directory names of the two commits from git when the unit starts and writes them to the unit's record and to the check program's configuration (`committed` in `checks.json`). The check program does not read the copy of the base revision beside the worktree.
5. **The engine knows neither the sandbox's paths nor the names the policy protects.** It neither deletes nor changes anything inside the worktree.

## Alternatives considered

- **Keep the environment from creating entries:** The user judged that it is normal for the environment to place entries in the working directory.
- **Hold the list of names the policy protects in the engine:** This would put a copy of the environment's policy in the engine, and the engine would have to change every time the policy changes.
- **Take the baseline again after the agent's session has started:** Entries can also appear in the middle of a session or in a later session. Taking the baseline again puts the role's writes up to that point into the baseline as well, and hides them.

## Consequences

- In a worktree that has only environment entries, a role that does not write is judged not to have written, and the entries do not enter the writer's `patch.diff` or changed paths.
- The digests of the check program and the engine agree regardless of an entry's mode or of whether it can be read.
- If an entry later gets content or becomes writable, it is counted as a change from that point on.
- The only thing this rule lets through is placing an empty file that cannot be written, under a name that is in neither commit, in the top directory of the worktree. Such a file is not counted even when a role places it itself. With tools that give meaning to the existence of such a file (an empty `pytest.ini`, `conftest.py`, `__init__.py` and the like in the top directory), the check runs on a tree that has the file, and the file does not enter `patch.diff`.
- When the environment places an entry below the top directory, it is counted as a change as before.
- When the engine runs as root, read-only files are also judged writable, and the entries are counted.

## Verification

`loop-engine/tests/test_environment_entries.py` confirms the following with a real git repository and a scripted agent.

- When an empty read-only file appears during a turn of a role that does not write, the role is not judged to have written; with a writable empty file it is.
- Entries do not enter the writer's patch, and a writable empty file the role created does.
- In a worktree with the six entries, a unit with a role that does not write and a writer reaches `done`, and the patch has only the writer's changes. The in-scope fact and the check fact hold.
- A tracked file that was truncated and made read-only, an entry that is no longer empty, an entry that has become writable, and a name that is only in the commit the worktree was made from are counted.
- An empty directory changes no digest, and an empty `.pi` does not change the agent configuration digest.
- The digests of the check program and the engine agree when an entry's mode is `0000` and when it appears as an unwritable `0600` (simulated by replacing the writability test). Confirmation on a real read-only mount is done in runs on a real machine.


{% transition from="proposed" to="accepted" date="2026-11-15" %}
The user approved on 2026-11-15. The judgment was "It is normal for the environment to place entries in the working directory, and it is the engine that is wrong to count them as a role's changes" (translated from Japanese). The reply to decision statements 1 to 5 (presented including the limit to the top directory, the judgment by the two commits, and keeping the names of the entries that were not counted in the unit's record) was "I approve".
{% /transition %}
