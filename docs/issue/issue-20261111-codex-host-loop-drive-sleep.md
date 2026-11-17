---
id: issue-20261111-codex-host-loop-drive-sleep
kind: issue
title: On a Codex host there is no completion notification for loop drive, and the main session polls with sleep and stops partway
status: done
created: '2026-11-11'
issue_type: bug
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/instructions.md
summary: On Codex the exit notification of loop drive did not arrive, the main session polled with sleep, and it ended
  its turn while waiting and stopped
disposition_reason: Removed the dependency on the host's exit notification from the loop drive contract. The driver is
  started in a separate process group and session through a launcher that exits right after starting it, so that it
  belongs neither to the caller's process group nor to its descendants (neither Codex stopping the process group nor
  Claude Code stopping by enumerating descendants stops it). driver_process.py owns the lease and the run record of
  each Loop, and loop drive attaches to the running run and returns the report. The driver checks the run record of the
  time of the call under the lease before it drives. Even if the call ends first, the report can be received by running
  the same command again. Removed the assumptions of a background command and an exit notification from
  instructions.md. The current structure is described in docs/architecture/development-loop.md. Live confirmation on
  Codex and Claude Code has not been done
updated: '2026-11-11'
---

## Symptom / opportunity

`runtime/development_loop/instructions.md:25` instructs "Run `loop drive` as a background command that can notify its exit, and wait until it exits" (translated from Japanese). However, the exit notification of a background command does not reach the main session on a Codex host, and the only means of waiting is polling with `sleep`. As a result, (1) the main session ends its turn while waiting for the drive and the work stops, (2) the detection of completion is delayed by up to about 50 seconds, and (3) a progress message and a model call occur about every minute.

## Evidence

- 2026-11-11, the main session of the worktree `pteropod` of knowledge-pipeline (Codex, gpt-6.1-sol low, rollout `01a1c97e-333d-7cb1-87e0-c166af6bfdba`).
- After it ran `open` / `drive` on the design loop at 16:29 JST, the main session ended its turn at 16:31. It stayed stopped for about 3 minutes, until the user prompted it 3 times with "Then why did you stop?" and "Then keep going" (translated from Japanese). The main session's own explanation was "While waiting for the design processing to complete, I ended the response by mistake".
- After that, the main session repeated `sleep {"duration_ms":50000}` and a status message at intervals of about 1 minute, more than 40 times in total (16:40-17:31). It relied on manual calls of `loop status` to learn of completion.
- Related loops: `49c480541fa6…` (design), `29c8763359…` (implementation asm), `62f7ca90b7…` (implementation dashboard-ui). All are saved in `~/.dev-skills/development-loop/.families/`.

## Context needed to start

- The handling of host differences is placed in the cross-host transport, and no per-host dispatch is placed on the execution side (AGENTS.md, "Development principles" and "Change discipline").
- It is unconfirmed whether Codex has a means of background exit notification, or whether it is enough to run `loop drive` in the foreground, blocking up to the boundary.
