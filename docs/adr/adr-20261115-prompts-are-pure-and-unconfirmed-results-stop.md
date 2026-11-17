---
id: adr-20261115-prompts-are-pure-and-unconfirmed-results-stop
kind: adr
title: Prompt composition works on values only, and on a result it cannot confirm the engine stops and does not retry by itself
status: accepted
created: '2026-11-15'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261114-agent-sessions-continue-and-approvals-are-refused}
source_paths: []
summary: The engine's prompt composition (prompts.py) works on values only, like the decision logic (facts.py). A unit
  whose turn result cannot be confirmed stops with a reason; the engine does not retry by itself and retries only on
  the caller's retry request.
updated: '2026-11-15'
---

# Prompt composition works on values only, and on a result it cannot confirm the engine stops and does not retry by itself

## Context

Two of the engine's rules were not recorded as decisions of the engine.

- "The decision logic works on values only" is set by item 8 of [the decision on completion by facts](adr-20261114-unit-completes-when-facts-hold.md), but that item covers the decision logic (`facts.py`). `prompts.py`, which composes the prompts given to roles, is written under the same rule, and `tests/test_closed_paths.py` checks that it imports only computation-only modules, but the only text that set this was in the development guidelines of the side that uses the engine.
- "When the result of a turn cannot be confirmed, the engine stops and does not retry by itself" is implemented and documented as the engine's behavior (`unknown_result`, `invalid_result`, and the retry request), but there was no record of it as a decision.

The engine's rules belong in the engine's documents. These rules were removed from the documents of the side that uses the engine, so they are recorded here as decisions of the engine.

## Decision

1. **Prompt composition (`prompts.py`) works on values only, like the decision logic (`facts.py`).** It has no I/O, clock, randomness, process or global execution context. The body of the protocol texts and the values that go into a prompt (the contract, the state, reports, feedback, check commands, the names of the agent configuration) are passed in as values by its caller.
2. **A unit whose turn result cannot be confirmed stops with a reason.** It stops with `unknown_result` in the following cases: the agent's turn did not complete, the release of the session could not be confirmed, the working copy could not be read, a turn that is subject to a check or a judgment had no execution record, the diff had changed after a turn of a role that does not write, the engine itself failed before the judgment. When a judgment reply cannot be confirmed to have the prescribed form, the engine has the same role submit it again exactly once, with the reason it was not accepted, and stops with `invalid_result` when this happens twice in a row for the same judgment.
3. **The engine does not resume a stopped unit by itself.** It retries only when the caller requests `retry`. The engine does not guess a result and does not make a substitute result.
4. **Starting a turn whose session cannot be continued again in a new session, exactly once, is outside this rule.** That is the redelivery of the same turn, which rule 4 of [the session decision](adr-20261114-agent-sessions-continue-and-approvals-are-refused.md) sets.

## Alternatives considered

- **The engine automatically retries a turn that cannot be confirmed:** Not adopted. It would repeat the same operation without knowing the cause, use the agent's quota, and move on to the next judgment without knowing the state of the working copy. When the caller looks at the reason for the stop and decides, what happened is not lost.
- **Treat a result that cannot be confirmed as a failure and count it as sending work back:** Not adopted. Sending work back is the mechanism that returns the findings on a fact that did not hold to the writer. An unknown result is not a finding, and returning it to a role cannot fix it.
- **Allow prompt composition to read files:** Not adopted. When only the wiring reads the protocol texts, composition is determined by its input values alone, and a test can confirm it by comparing input and output.

## Consequences

- A prompt is always the same text for the same values. The effect of a change to a protocol text can be confirmed by tests of the composition's input and output.
- For a unit in `unknown_result` or `invalid_result`, the caller reads the reason for the stop (the detail in `stop.json`) and then decides on `retry`. No quota is used while the engine is stopped.
- A unit also stops on a transient fault (such as a temporary failure of the agent). Continuing requires the caller's request.

## Verification

- `tests/test_closed_paths.py` checks that the imports of `facts.py` and `prompts.py` are limited to computation-only modules (`re`, `json`, `shlex`, `hashlib`) and that they do not call `open`, `print`, `exec` and the like.
- The engine's tests (`tests/test_unit_flow.py`, `tests/test_sessions.py`, `tests/test_judged_trees.py`, `tests/test_environment_entries.py`, `tests/test_facts.py`) confirm that in each case of decision 2 the unit stops with `unknown_result` or `invalid_result`, and that the next turn does not start until `retry` is requested.
