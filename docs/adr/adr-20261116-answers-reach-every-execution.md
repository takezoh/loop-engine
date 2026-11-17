---
id: adr-20261116-answers-reach-every-execution
kind: adr
title: The requester's answers are given to every execution, and reviews are judged against those answers
status: accepted
created: '2026-11-16'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-verdict-computed-from-checkable-findings}
- {type: references, target: adr-20261114-agent-sessions-continue-and-approvals-are-refused}
- {type: references, target: adr-20261116-work-goes-back-while-it-converges}
source_paths: []
summary: Answers stay in the unit's record and every execution receives them. A verdict fact has the answers it was judged with in its basis and is judged again when an answer is added.
---

# The requester's answers are given to every execution, and reviews are judged against those answers

## Context

When a role ends its turn with a question, the unit stops with `waiting_for_answer` and continues on the requester's answer. The answer was passed as feedback to the execution that asked, and did not reach the other executions.

In the records of 2026-11-16, the design role of messenger-browser-gate-design asked "May the selection of the approval route for practical use be deferred?" (translated from Japanese) and revised the plan according to the requester's answer. The reviewer had not received that answer, raised a blocker stating "the latest answer that permits deferring the selection does not exist in the current input or reports", and the unit stopped. The arrangement was one in which the requester's decision did not reach the independent review.

## Decision

1. The engine keeps each answer, in order, in the unit's state (`answers` in `state.json`), together with the question's id, the question, the answer and the execution that asked. It does not put the answer into the feedback for the execution that asked.
2. Every execution receives the answers. The first message of a session carries all the answers so far, and a follow-up message carries the answers that session has not yet received. The protocol text presents the answers as the requester's decisions, to be applied to the work and to the judgment of the acceptance criteria.
3. The basis of a review (`verdict`) fact includes the answers. When an answer is added, the judgments made before it stop holding, and the reviewer judges the same subject again against that answer. The answers are not included in the basis of report, check or scope facts.
4. Even when a writer that received feedback ends its turn without changing the candidate, the unit is not stopped for stagnation if there was an answer after the fact of that feedback was judged. The fact is judged again against the answer.

## Relation to existing decisions

- The requester's answers are added to item 5 of [the review decision](adr-20261114-verdict-computed-from-checkable-findings.md) (the reviewer is given only the goal, the acceptance criteria, the list of changed files and the location of the original versions). The rule that the writer's report and the caller's diff are not given is unchanged.
- Item 3 of [the session decision](adr-20261114-agent-sessions-continue-and-approvals-are-refused.md) (a follow-up message carries what is new, namely feedback, answers and changed facts) is unchanged. The destination of the answers changes from the execution that asked to every execution.
- The contract's scope (`scope`) does not change with an answer. An answer that widens the scope is handled in a new unit.

## Alternatives considered

- **Make the answer a report fact that the contract declares.** An answer does not exist when the contract is made, and each Profile would need declarations of the fact and of the read relations. Rejected.
- **Add the answers only to the reviewer's message and leave them out of the basis.** A judgment that refuted before the answer stays in effect, and there is no path to judge the same candidate again. When the writer decides that the answer alone is enough and does not change the candidate, the unit stops for stagnation. Rejected.

## Consequences

- The reviewer receives what the requester decided (the interpretation of the scope, the deferral of a decision, how to read the acceptance criteria) and judges with it.
- On every answer, a reviewer that has already judged judges once more. Answers are few, so the added turns stay at the product of the number of answers and the number of review executions.
- An answer is not the writer's report, so the rule that the reviewer does not read the writer's explanation is kept.

## Verification

`AnswerTest` in `tests/test_facts.py` confirms that a review made before an answer stops holding, that a candidate returned unchanged is judged again when this comes after an answer, and that reports and checks are not invalidated by an answer. `tests/test_sessions.py` confirms that the answers also go into the first message of an execution that did not ask.

## User's decision

On 2026-11-16, in response to item 3 of the improvements listed in the investigation of the Loop records, "The user's answer does not reach the independent review role. The answer needs to be recorded as a fact that every role can read. This is an engine design decision, so it needs approval" (translated from Japanese), the user instructed "Address 1,2,3,4,5,6".
