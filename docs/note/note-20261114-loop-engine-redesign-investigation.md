---
id: note-20261114-loop-engine-redesign-investigation
kind: note
title: Loop Engine redesign investigation record (measured failures, causes, reference harnesses, PoC)
status: draft
created: '2026-11-14'
tags: []
owners: []
relations: []
source_paths: []
summary: Investigation of 2026-11-14. The Loop completion rate, the cause of repeated incomplete implementations, the comparison with reference harnesses, the results of the PoC and of the checks on real agents, the direction that was settled, and the items not yet confirmed.
---

## Summary

This note records an investigation into why Loops do not run stably and into the direction of the redesign. Most Loops that did not complete stopped at the execution infrastructure stage, not because of the quality of the outcome. Incomplete implementations were repeated because the implementing agent itself could rewrite, in the same change, the documents and checks that carry the requirements. The direction is summarized in 2 draft ADRs.

- [Engine boundary](../adr/adr-20261114-loop-engine-host-boundary.md)
- [Judging checks](../adr/adr-20261114-checks-judged-from-execution-record.md)

## Notes

### Loop completion rate (tally of execution records)

The records in `~/.dev-skills/development-loop/.families/*.sqlite3` were classified mechanically by whether the accepted quality review was satisfied.

| Records | Period | Count | Completed | Cancelled | Not completed |
|---|---|---|---|---|---|
| This machine | 11/07 to 11/12 | 61 | 29 | 21 | 11 |
| Another machine | 9/29 to 11/13 | 174 | 58 | 30 | 86 |

- Of the Loops not completed on the other machine, 43 had "execution started" as their last record with no end record, and 8 were only registered.
- The median number of reviews for the 63 implementation Loops that did not complete was 0. They stopped before reaching review.
- There are 6 records of the host MCP service approach (from 11/13 on) on the other machine. 1 reached the outcome boundary, 3 were `producer-not-started`, and 2 were cut off after repeated `producer-output-missing`.

### Cause of repeated incomplete implementations

- Commit `0540f7a5` (240 files) moved the Loop to the host, added bwrap isolation, deleted the rules of `DEVELOPMENT.md` §11, swapped the meaning of the skill-lint boundary rules (C11 to C15), rewrote 103 test files, and newly created 2 ADRs as accepted. There is no record of approval.
- The documents and checks after that agree with each other, taking the state after the violation as correct. The session that was instructed to roll back also used those documents as its baseline, so only the producer side was rolled back (`00e288da`).
- From inside the sandbox, git writes, writes to `~/.dev-skills`, and starting an agent are not possible (measured). The Loop depended on these, and that repeatedly produced paths that cross the boundary.

### Comparison with reference harnesses

This is based on reading documents and code. The actual behavior was not confirmed.

| Harness | What drives progress | Use from a conversation | sandbox |
|---|---|---|---|
| Ouroboros | A detached worker | A skill calls MCP tools, then waits and queries status | None |
| Lobster | runtime | The agent calls it synchronously as a tool | None |
| Clawpatch | CLI | No means was found | Relies on the provider |
| autoresearch | A shell loop | None | None |

- None of them has its own sandbox or a ledger for run-once execution.
- In Ouroboros, only mechanical checks can give acceptance. It reads the check commands from the project's configuration file.
- Ouroboros runs the checks on the host and has an unresolved issue about confinement. This point is not adopted.

### PoC results

The PoC was built in a temporary directory and is not in the repository. The engine itself is about 670 lines.

- 38 tests with a fake agent: the flow, sending work back, the distinction between the 7 stop reasons, resumption after a forced kill, the blocking of 8 paths, and the extent of pure functions all held.
- Real agents (CLI started directly):
  - `codex exec` outputs the command and a numeric exit code.
  - `claude -p` does not output a numeric exit code.
  - `claude -p` with defaults started the hooks of the working directory and the servers in `.mcp.json` without confirmation.
  - With `--setting-sources user` it does not start them, but `CLAUDE.md` and `AGENTS.md` are no longer read either.
  - codex started the MCP servers in `.codex/config.toml` even with a read-only sandbox and the untrusted setting.
- git stopped starting external programs only when given both the setting that disables hooks and the option that makes it read attributes from the empty tree.

### Points where the current implementation does not match the direction

- `codex_driver.py` specifies the producer's sandbox by itself.
- There is a judgment that makes only drivers that can enforce the write destination into producers.
- The turn result has no record of the commands that were run.
- In the catalog, the side that uses pi is empty, so it is disabled.

### Not yet confirmed

- A worktree whose main `.git` is in a place the agent cannot read.
- Why claude over ACP was able to list `~/.ssh`. How far the environment's sandbox settings take effect.
- Why pi did not accept `--exclude-skills`.
- The conditions under which a command does not appear in the codex record.
- The individual reasons for the Loops that were cancelled.

### Checks on real agents through the existing abstraction

3 turns per agent. The records are in a temporary directory and are not in the repository.

- The provider's common processing could be assembled and run from an ordinary process without an MCP server.
- 2 agents could not be started with the catalog as it is.
  - claude: the launcher's pre-start check failed. The executable file name in the installed bundle (11/12) differs from the name the current repository expects.
  - pi: the started pi did not accept `--exclude-skills`, which the catalog passes.
- Execution record: codex and pi output success or failure and a numeric exit code. claude over ACP does not, and even a failing command arrived as completed. All 3 drivers discard these events.
- codex did not output to the record 1 command that failed in the sandbox.
- Working directory configuration: claude started the hooks and the MCP servers. codex started only the MCP servers. pi started neither.
- Instruction files: claude read `CLAUDE.md`; codex and pi read `AGENTS.md`.
- codex refused to run git under the no-approval setting. This is because the user's configuration requires git to run outside the sandbox.
- claude and pi were able to run `git status` and `git diff` inside the worktree. The case where the main `.git` is in an unreadable place was not tried.

### Document inventory (same day)

172 documents whose status indicates they are not in effect and 20 documents with no counterpart in the implementation were moved to `docs/archived/`.
