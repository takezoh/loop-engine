---
id: adr-20261114-unit-completes-when-facts-hold
kind: adr
title: A unit's completion is decided by its facts holding, and the engine derives the next execution from the contract's data
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-loop-engine-host-boundary}
source_paths: []
summary: The contract the engine receives declares only the facts to establish and the executions that can establish them. The engine knows neither the number of roles, nor their order, nor the Profile, and chooses the next execution from the dependencies of the facts.
updated: '2026-11-14'
---

# A unit's completion is decided by its facts holding, and the engine derives the next execution from the contract's data

## Terms

A **fact** is something that must hold for the unit to complete, such as "a candidate exists", "the checks passed" or "it was reviewed independently". An **execution** is 1 turn of an agent. The **contract** is the only input the engine receives when a unit starts.

## Context

A Profile does not declare the order of executions. What it declares is the facts to establish and the executions that can establish them. The order is decided by the dependencies of the facts the executions read.

The first version of the new engine, written on 2026-11-14, did not have this mechanism and fixed 2 roles as "writer → checks → reviewer". This baked the shape of the implementation Profile into the engine, and it could not express review (which has no writer) or investigate (whose outcome is only a report).

The current decision function (`runtime/loop_kernel/service/domain.py`) chooses the next execution from facts, but it has the following problems.

- 3 invalidation schemes overlap: matching against the candidate's digest, clearing everything by attempt count, and cascading clearing for the baseline.
- The decision function contains Profile-specific terms (pursuit, the baseline snapshot, the gate for document recording, specific execution names).
- The same relation is written in 3 places (the facts a role owns, the executions that establish a fact, the role of an execution).
- There are executions in which the engine itself runs project commands. This violates the [engine boundary decision](adr-20261114-loop-engine-host-boundary.md).

In 61 Loop records, the sequences the engine chose were, for each Profile, only a single straight path and its repetition. On the other hand, the records show 149 times where holding was confirmed again without making the work again. What actually does the work is the management of holding, more than the derivation of order.

## Decision

1. The contract has only `unit` (repository, goal, acceptance criteria, change scope, limits), `facts` and `executions`. Profiles, skills, judgment knowledge (Knowledge) and placement presets are handled by the side that assembles the contract, and the engine does not know them.
2. A fact declares 1 way of being established.

   | Kind | What the engine looks at |
   |---|---|
   | `given` | The tree at the start. The engine observes it |
   | `report` | That the execution's turn completed |
   | `checks` | The check results in that turn's execution record ([checks decision](adr-20261114-checks-judged-from-execution-record.md)) |
   | `verdict` | The judgment the review execution returned ([review decision](adr-20261114-verdict-computed-from-checkable-findings.md)) |
   | `git` | That the changed files are within scope, and that the agent configuration has not changed |

3. An execution declares the facts it reads and the facts it establishes. This is the only place where the relation between facts and executions is written.
4. A fact carries the basis it held on (the subject of the judgment and the content of the facts that were read). It holds only while the basis is the same as the present. This is the only invalidation rule.
5. The engine chooses an execution whose read facts all hold and whose facts to establish include one that does not hold yet. The unit is complete when every required fact holds.
6. When a `checks`, `verdict` or `git` fact is refuted, the engine sends the work back to the execution that made the subject of that judgment, handing it the findings. Facts that do not depend on the subject are kept. If no execution makes the subject, the unit ends with the findings as its result.
7. The stop reasons are complete, rejected, waiting for an answer, out of rounds, stagnation, unknown result, repeated results that cannot be checked, changed agent configuration, and cancelled; they are returned distinctly.
8. The decision logic is functions that work on values only (pure). State can be saved as JSON, read back, and continued from where it left off.

## Relation to existing decisions

- The rules of the [engine boundary decision](adr-20261114-loop-engine-host-boundary.md) and the [checks decision](adr-20261114-checks-judged-from-execution-record.md) are kept as they are. This decision decides how to express, as contract data, what those 2 call the "writer" and the "reviewer".
- Executions in which the engine runs project commands (running baseline commands, commit and integration, recording documents) cannot be placed in a contract. Confirming that something fails before the change becomes a `checks` fact, and the main session does the commit and integration.

## Required changes

- Rewrite the contract validation, the decision function, the prompt assembly and the running of units in `packages/loop-engine` to fit this contract. The drivers' execution records, state saving, git, the engine process and the entries do not change.
- Place the processing that assembles a contract from a Profile outside the engine (in front of the MCP and CLI entries).

## Alternatives considered

- **Fix 2 roles.** Simple, but the engine would have to change for each Profile. Rejected.
- **Write a sequence of steps in the contract.** Someone would have to write an order that the Profile does not declare, and what to redo when the candidate changes would have to be decided separately. Rejected.
- **Keep using the current decision function.** The overlapping invalidation and the Profile-specific terms remain. Rejected.

## Consequences

- Units of different shapes (implementation, review only, report and review) can be expressed as contract data without changing the engine.
- Among the harnesses referenced and the major frameworks, none could be confirmed to have a mechanism by which a change revokes downstream holding. No measurement was found showing that deriving the order raised reliability either. The gain of this part comes from the management of holding.
- If the contract has an error (a required fact that cannot be established), the unit cannot proceed. This is handled by validation at admission and by a "cannot proceed" stop reason.

## Verification

A PoC (2026-11-14, outside the repository) confirmed the following.

- With the same engine, 3 contracts ran: implementation (investigation, implementation, 2 reviews), review only, and report and review. The engine's code has no role names and no order.
- 13 tests with a fake agent. Review findings are sent back together, the investigation is not redone, a failed check does not proceed to review, an out-of-scope change is sent back without review, stop reasons are distinguished, state is read back and continued, and when the tree changes outside a turn the review stops holding.
- With real agents (codex implementing, claude and pi reviewing), implementation → review → review → complete went through.

After implementation, the same content is confirmed by the tests of `packages/loop-engine`.
