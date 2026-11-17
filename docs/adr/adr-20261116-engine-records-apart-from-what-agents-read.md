---
id: adr-20261116-engine-records-apart-from-what-agents-read
kind: adr
title: The engine's records are separated from what agents read and placed where agents cannot read them
status: accepted
created: '2026-11-16'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-verdict-computed-from-checkable-findings}
source_paths: []
summary: The records (contracts, states, events, turn records, results, requests, transport state) move to DEV_SKILLS_HOME/records/loop-engine, which the agent's sandbox makes unreadable. What agents read (the check program, units' checks.json, the base revision copy) stays in DEV_SKILLS_HOME/loop-engine.
---

# The engine's records are separated from what agents read and placed where agents cannot read them

## Context

The engine used the state home (`DEV_SKILLS_HOME/loop-engine`) as a location that the agent's sandbox can read and cannot write. What agents read (the check program, each unit's `checks.json`, the copy of the base revision) and the engine's records (contracts, states, events, turn records, results, the state of the transport's sessions) sat there side by side. The shared sandbox policy allowed reading all of `~/.dev-skills`.

In the records of 2026-11-16, reviewers read the records in this location (the turn records under `result/` and `state.json`) in 9 turns of 4 units. Some turns read the writer's turn record (`result/turns/02-implement.json`, the writer's reply and execution record). Turns that read the copy of the base revision read a location they were given, and are not counted here. The engine does not give the reviewer the writer's report (item 5 of [the review decision](adr-20261114-verdict-computed-from-checkable-findings.md)), but the reviewer could read it as a file. The pi session records (`agents/pi-sessions/`) were in the same location.

## Decision

1. The engine's records (contracts, admissions, states, events, state summaries, turn records, results, requests and responses, locks, transport state) are placed in `DEV_SKILLS_HOME/records/loop-engine`.
2. What agents read, namely the check program (`check/<sha256>/check_runner.py`) and each unit's `checks.json` and copy of the base revision (`units/<unit_id>/`), is placed in `DEV_SKILLS_HOME/loop-engine`. The paths given to roles (the check commands, the location of the base revision) point only to this location.
3. The agent's sandbox makes the records location unreadable. The shared sandbox policy allows reading only `~/.dev-skills/loop-engine`, not all of `~/.dev-skills`, and denies `~/.dev-skills/records`. What makes the location unreadable is the environment's sandbox configuration; the engine does not create it and does not confirm it.
4. The two locations are named so that neither path is a prefix of the other. This is so that, even under a configuration that decides allow or deny by path prefix, a rule for one does not apply to the other.

## Relation to existing decisions

- Item 6 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) (state is placed in `~/.dev-skills`, and the engine is the only writer) is unchanged. Both the records and what is published are under `~/.dev-skills`, and only the engine writes them. The rule that the engine does not own a sandbox (item 2 of the same decision) is also unchanged.
- The rule that the reviewer is not given the writer's report (item 5 of the review decision) can now be kept for file reads as well.

## Required changes

- Narrow the read allowance of the shared sandbox policy (`modules/_lib/sandbox-policy.json` in agent-environments) from `~/.dev-skills` to `~/.dev-skills/loop-engine`, and add `~/.dev-skills/records` to the denials. The shared compiler does not accept a denial inside a read allowance, and Codex treats a denial of `~/` as outside the allowlist, so narrowing the allowance makes the records unreadable in the same way on the three hosts. Each host's setup applies it.
- The records that an earlier engine left in `~/.dev-skills/loop-engine` (the records under `units/`, `agents/`, `inbox/`, `outbox/`) can be read by agents until they are moved to the records location.
- A main session that runs inside the sandbox also can no longer read the records and the other directories of `~/.dev-skills` directly. It receives a unit's state and result through MCP `unit_status` and `unit_result`, for which a process on the host does the reading.

## Alternatives considered

- **The engine looks in the reviewer's execution record for traces of reading the records and does not count that review.** It cannot detect ways of reading in which the path does not appear in the execution record, and the session that has read them also remains. The user did not choose it.
- **Leave the records in the present location and move what agents read to another location.** The location that the entries and the verification tools read does not change, and the earlier records also become unreadable as they are. It differs from the method the user chose (moving the records to another root), so it was not adopted.
- **Keep the read allowance for `~/.dev-skills` and deny only the records location.** The shared compiler rejects a denial inside a read allowance, and Codex cannot express that denial. Not adopted.

## Consequences

- Once the sandbox configuration is in place, no role can read another role's reply, execution record or session record from files. What a role receives is only the facts the contract declares it reads and the requester's answers.
- The records of the earlier Loop (`composition/`, `development-loop/` and others under `~/.dev-skills`) also become unreadable to agents.
- Work that examines the records (such as the investigation of the Loop records) uses reads outside the sandbox or the MCP tools.

## Verification

`tests/test_unit_flow.py` confirms that the messages given to roles do not point to the records location and point to the location agents read, that neither of the two locations is inside the other or a prefix of the other, that only the check program and `checks.json` remain in the location agents read, and that the records location has no `checks.json` and no copy of the base revision. The translation of the shared policy is confirmed by `modules/tests/test_sandbox_contracts.py` and `test_shared_sandbox.py` in agent-environments. That the sandbox makes the records unreadable is confirmed on a real machine after each host's setup.

## User's decision

On 2026-11-16, to the question that asked about the method for item 5, the user chose "Separate the records and block them with the sandbox (recommended)" (translated from Japanese). The explanation of the question was "Leave only what agents read in the present location and move the records to another root. Make that root deny in the shared policy of agent-environments". The first implementation did the opposite and moved what is published, so, following the user's "Blocking on the sandbox side (5), fix the change of the implementation method of 5", it was corrected to the method that was explained, and the shared sandbox policy was also changed.
