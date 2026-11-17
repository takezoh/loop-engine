---
id: adr-20261117-engine-names-its-own-home-and-check-file
kind: adr
title: The engine's state home, environment variables and check file carry the engine's
  own name
status: accepted
created: '2026-11-17'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261116-engine-records-apart-from-what-agents-read}
source_paths: []
summary: The state home is LOOP_ENGINE_HOME (default ~/.loop-engine) with records/
  and published/ under it, worktrees are under LOOP_ENGINE_WORKTREES, and a project
  defines checks in .loop-engine/checks.json. The earlier dev-skills names are not
  read.
updated: '2026-11-17'
---

# The engine's state home, environment variables and check file carry the engine's own name

## Context

The engine was developed inside the dev-skills repository and took its names from it. Its state home was the dev-skills state home (`DEV_SKILLS_HOME`, by default `~/.dev-skills`), which dev-skills also uses for its own state. Inside that home the engine kept its records in `records/loop-engine` and what agents read in `loop-engine`. Worktrees were made under `DEV_SKILLS_WORKTREES` (by default `dev-skills/worktrees` in the user's cache directory), and a project defined its checks in `.dev-skills/checks.json`.

The engine is being separated from the dev-skills repository. dev-skills is one of its callers, and a caller's name in the engine's environment variables, default directories and project file ties every other caller and every project to that caller.

## Decision

1. **The state home is the engine's own.** The environment variable is `LOOP_ENGINE_HOME` and the default is `~/.loop-engine`. The engine shares it with no caller.
2. **The two roots inside the home are `records/` and `published/`.** The engine's records go to `LOOP_ENGINE_HOME/records`, and what agents read (the check program, each unit's `checks.json` and copy of the base revision) goes to `LOOP_ENGINE_HOME/published`. Neither path is a prefix of the other.
3. **Worktrees are made under `LOOP_ENGINE_WORKTREES`.** The default is `loop-engine/worktrees` in the user's cache directory (`XDG_CACHE_HOME`, or `~/.cache`).
4. **A project defines its checks in `.loop-engine/checks.json`.** The engine reads the file at the base revision, as before.
5. **The earlier names are not read.** The engine has no alias, fallback or migration for `DEV_SKILLS_HOME`, `DEV_SKILLS_WORKTREES`, `~/.dev-skills` or `.dev-skills/checks.json`.

## Relation to existing decisions

- Item 6 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) placed state in `~/.dev-skills`. The place becomes `~/.loop-engine`. That the engine is the only writer, and that contracts and answers arrive as data, is unchanged.
- Items 1 and 2 of [the records decision](adr-20261116-engine-records-apart-from-what-agents-read.md) named the two locations `DEV_SKILLS_HOME/records/loop-engine` and `DEV_SKILLS_HOME/loop-engine`. They become `LOOP_ENGINE_HOME/records` and `LOOP_ENGINE_HOME/published`. Item 3 (the agent's sandbox makes the records unreadable, and the engine neither creates nor confirms that) and item 4 (neither path is a prefix of the other) are unchanged; the paths that the sandbox configuration names change with the locations.

## Required changes

- The engine, its tests, its tools and its bundled scenarios use the new names.
- A caller that sets the engine's home sets `LOOP_ENGINE_HOME`. dev-skills keeps `DEV_SKILLS_HOME` for its own state, and its documents, request schemas and tests name the engine's home and `.loop-engine/checks.json`.
- The environment's sandbox configuration allows agents to read `~/.loop-engine/published`, denies `~/.loop-engine/records`, and lets agents write the worktrees root. This configuration is outside the engine's repository.
- A project that defines checks moves `.dev-skills/checks.json` to `.loop-engine/checks.json`.
- Records that an earlier engine left under `~/.dev-skills` are not read. A unit that had stopped there with a stop that can be continued cannot be answered or retried by the renamed engine.

## Alternatives considered

- **Keep the names until the separation.** The documents would describe the engine as independent while its interface named one caller, and the rename would still have to be made. The user did not choose it.
- **Rename the environment variables and default directories and keep `.dev-skills/checks.json`.** Fewer projects would have to change, but every project that defines checks would keep a directory named after one caller. The user did not choose it.
- **Rename only the home and keep `loop-engine` and `records/loop-engine` inside it.** The earlier decision on the two locations would stand as written, but the engine's name would repeat inside a home that is already the engine's. The user chose `published` and `records`.

## Consequences

- The engine's interface names no caller. Several callers can use one engine home.
- dev-skills' own state (`DEV_SKILLS_HOME`) and the engine's state are in different places. A setup that isolates both sets both variables.
- Until the sandbox configuration names the new paths, agents cannot read the check program or the copy of the base revision, no check status line appears, and `checks` facts do not hold. If the configuration allowed reading all of `~/.loop-engine`, agents could read the records.
- Until a project moves its check definitions, a contract that names a check is not accepted for that project.

## Verification

`tests/test_service.py` confirms the two roots under `LOOP_ENGINE_HOME`, the default home, the name of the check file, the default and the override of the worktrees root, and that the earlier variables change none of them. `tests/test_unit_flow.py` confirms that the messages given to roles point only into the published root and that neither root is inside the other or a prefix of the other. `tests/engine_support.py` builds every test engine from `LOOP_ENGINE_HOME` and defines the test repository's checks in `.loop-engine/checks.json`, so the whole suite runs on the new names.

## User's decision

On 2026-11-17 the user said "loop-engine/ will be separated from this repository. dev-skills is only one of its consumers, so fix every mention of dev-skills" (translated from Japanese). Asked what to do with the identifiers that carried the dev-skills name, the user chose "Rename the engine and dev-skills together", whose explanation named `LOOP_ENGINE_HOME`, `LOOP_ENGINE_WORKTREES`, `~/.loop-engine` and `.loop-engine/checks.json` and stated that no fallback for the earlier names is kept. Asked about the layout inside the home, the user chose "published / records".
