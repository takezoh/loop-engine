---
id: adr-20261121-files-git-ignores-are-not-changes
kind: adr
title: A new file that git ignores by the base revision's rules and by the candidate's
  own is not a change
status: accepted
created: '2026-11-21'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261115-environment-entries-are-not-changes}
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
source_paths: []
summary: A path the base revision does not have is left out of the changed paths only
  when the ignore rules of the base revision and of the worktree both exclude it.
  The base rules come from the engine's copy, so an agent cannot widen the exclusion,
  and anything git would deliver is counted.
updated: '2026-11-21'
---

# A new file that git ignores by the base revision's rules and by the candidate's own is not a change

## Context

The changed paths compare every file of the worktree with git's record of the base revision, and everything except the environment entries and the names in `ignore` of `.loop-engine/checks.json` counts. `patch.diff` comes from git, which leaves out what the repository's `.gitignore` files exclude. A file git ignores was therefore a change for the scope judgment and for the reviewers' list of changed files, and never part of the patch.

In the records of 2026-11-20, two units on the same repository failed this way. The writer ran `npm install` in `services/minutes/harness`, which made 8,578 files under `node_modules/`; that directory's `.gitignore` excludes `node_modules/`, and none of the files entered `patch.diff`. The in-scope fact named each file as changed outside the scope. The follow-up message listed them twice, as findings and as changed files, and came to 1,152,878 characters. Codex refused the turn (`input_too_large`, with a limit of 1,048,576), both units stopped with `unknown_result`, and a retry sends the same message.

Limiting the size of the message would not remove the cause: the writer would still be sent back to remove files that the checks may need and that the patch never carries. A project can list such names in `ignore`, but that repeats what its `.gitignore` already says, and a project that does not is stopped as above.

Which files git ignores can also be decided by `.gitignore` files in the worktree, which agents write. A rule taken only from there would let a role hide any new file from the scope judgment and from review, together with the rule itself (a new directory whose `.gitignore` says `*`).

## Decision

1. A path that git's record of the base revision does not have is left out of the changed paths when, and only when, both of the following exclude it.
   - The ignore rules of the base revision. They are read from the engine's copy of the base revision, through the git directory fixed when the worktree was made and without the index (`git check-ignore --no-index`). Agents cannot write either.
   - The ignore rules as git applies them to the worktree, with the worktree's index (`git check-ignore`). A path git tracks, or would add to the patch, is never left out.
2. A path that the base revision has, changed or removed, is always counted, whatever the rules say.
3. A path left out of the changed paths is left out of the scope judgment, the list of changed files handed to reviewers and the share of changed files a reviewer must have read. The tree digest and the checks bound to it, the before-and-after comparison of a turn of a role that does not write, and `patch.diff` are as before.
4. The engine does not read or interpret ignore rules itself. git decides, started by the one module that starts git, with hooks and filters turned off.

## Relation to existing decisions

- Item 3 of [the environment entries decision](adr-20261115-environment-entries-are-not-changes.md) counts everything other than an environment entry as before, files below the top directory included. Item 1 here leaves out of that the new files both rule sets ignore. Its other items are unchanged.
- The `git` fact of [the unit completion decision](adr-20261114-unit-completes-when-facts-hold.md) still compares the changed paths with the scope; which paths are changed follows this decision.

## Alternatives considered

- **Limit the size of the follow-up message.** The refusal goes away, but the writer is still sent back for files the patch never carries, and a unit whose checks need them cannot reach `done`. Rejected.
- **Keep counting them, and have each project list them in `ignore`.** This repeats `.gitignore`, and a project that does not list them stops as on 2026-11-20. `ignore` stays for what a check writes into the tree it is bound to. Rejected as the answer to this problem.
- **Use only the worktree's rules, as the patch does.** A role could hide a new file with a rule it writes itself. Rejected.
- **Use only the base revision's rules.** A file that the candidate stops ignoring, by editing a `.gitignore` inside the scope, would enter the patch without being judged. Rejected.
- **Also leave ignored files out of the tree digest.** The checks run on the whole tree, and files under ignored paths can change their result. The digest stays as it is. Rejected.

## Consequences

- A writer can make what the project's `.gitignore` already excludes (dependencies, build output, caches) without being sent back for it, and follow-up messages no longer carry those paths.
- A file placed under a path that both rule sets ignore can affect the checks without being judged or delivered, as [the environment entries decision](adr-20261115-environment-entries-are-not-changes.md) accepts for its narrower case. It never enters `patch.diff`.
- A new file that only the worktree's rules ignore is counted although `patch.diff` leaves it out, so an attempt to hide a file shows as a change.
- A follow-up message still grows with the number of paths git would deliver; a change of very many such paths can still be more than an agent accepts.

## Verification

`tests/test_ignored_files.py` runs units with a real repository and scripted agents. A writer that makes files under a path the base revision's `.gitignore` excludes reaches `done`, and the files are in neither the changed paths nor `patch.diff`. A new directory that ignores itself through its own `.gitignore` is counted. A file that a `.gitignore` edited inside the scope stops ignoring is counted and is in `patch.diff`. A file the base revision has is counted whatever the rules say.

## User's decision

On 2026-11-21 the user was asked how to fix the units that stopped with `input_too_large`: (a) leave out of the changed paths the new files that both the base revision's and the candidate's `.gitignore` exclude, recorded in a new ADR; (b) keep the rule and have each project declare them in `ignore` of `.loop-engine/checks.json`; or (c) limit the size of the follow-up message. The user chose (a), "git が除外する生成物は数えない" (do not count the generated files git excludes), after asking for a structural rather than a local fix and asking whether a message limit was appropriate.
