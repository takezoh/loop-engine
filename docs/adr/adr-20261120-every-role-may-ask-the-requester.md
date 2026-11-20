---
id: adr-20261120-every-role-may-ask-the-requester
kind: adr
title: Every role may ask the requester, and one protocol text says how
status: accepted
created: '2026-11-20'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-verdict-computed-from-checkable-findings}
- {type: references, target: adr-20261116-answers-reach-every-execution}
source_paths: []
summary: Any turn may end with a question that stops the unit until the requester
  answers. The form of the question is one protocol text that the engine adds to every
  message, so a reviewer can ask as a writer or a reporter can.
updated: '2026-11-20'
---

# Every role may ask the requester, and one protocol text says how

## Context

The engine treats every turn alike when it ends with a question: the unit stops with `waiting_for_answer`, the requester's answer reaches every execution, and a review is judged again against it ([the answers decision](adr-20261116-answers-reach-every-execution.md)). `facts.turn_completion` accepts a reviewer's turn that ends with a question, and a test confirms it.

The protocol texts did not say so alike. The writer's and the reporter's first and follow-up texts each carried their own copy of how to ask. The reviewer's two texts had none and told the reviewer to end with the judgment only. A reviewer that could not judge a criterion without a decision of the requester, for example because the criterion asks for more than the goal's sources state, had to mark it unmet. The finding went to the writer, who could only add to the candidate. In the records of 2026-11-20, a design unit's reviewer refuted the same criterion in two rounds for a bypass that no source required, and the designer answered by adding a registry of expression counts to the plan.

## Decision

1. Every role may end a turn with a question instead of the reply its protocol text describes. The unit stops with `waiting_for_answer`, as for any question.
2. The form of the question is one protocol text, `protocol/question.md`. The engine adds it to every message it composes, first and follow-up, for every role. The role texts do not describe the question themselves.
3. The form in that text is the one `facts.question` accepts. A test composes every role's first and follow-up messages, and checks that each ends with that text and that its example is read as a question.
4. A reviewer's question is not a judgment. The rules for a judgment (every criterion once, evidence for a met criterion, a finding for an unmet one) apply to the reply that judges, and the answer is judged against as [the answers decision](adr-20261116-answers-reach-every-execution.md) says.

## Relation to existing decisions

- Item 1 of [the review decision](adr-20261114-verdict-computed-from-checkable-findings.md) (the reviewer returns a judgment and evidence per acceptance criterion, and findings) is unchanged for a reply that judges. A question replaces that reply for the turn; the criteria stay those of the contract.
- [The answers decision](adr-20261116-answers-reach-every-execution.md) is unchanged: the answer to a reviewer's question reaches every execution, and the verdict fact is judged again with it.

## Alternatives considered

- **Add the question to the reviewer's two texts.** The form would be written in six places, and a role text added later could leave it out again. Rejected.
- **Let a reviewer mark a criterion as not judgeable in its judgment.** The verdict would need a third state and a stop of its own, while the question already stops the unit and carries the answer to every execution. Rejected.

## Consequences

- A reviewer that needs the requester's decision asks instead of refuting, and the writer is not sent back for it.
- A unit may stop for a reviewer's question. Each answer costs one more review turn per review execution, as any answer does.
- How a role asks is written once; a new role text cannot leave it out.

## Verification

`tests/test_sessions.py` composes the first and follow-up messages of a writer, a reviewer and a reporter and checks that each ends with `protocol/question.md` and that `facts.question` reads its example as a question. `tests/test_unit_flow.py` runs a unit whose reviewer asks, and checks that the unit waits for the answer and that the review is judged again after it.

## User's decision

On 2026-11-20, to the proposal "add the question form to the reviewer's protocol texts, record it in an engine ADR, and then align the caller's knowledge", the user answered "方針は承認するが、実装は局所てきではなく、構造的に修正して。" (the direction is approved, but fix it structurally, not locally).
