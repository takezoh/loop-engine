---
id: adr-20261120-an-engine-error-in-a-request-is-answered
kind: adr
title: An engine error in a request is answered, and the engine keeps serving
status: accepted
created: '2026-11-20'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
source_paths: []
summary: An unexpected error while handling a request becomes an error response, and
  the engine keeps driving its units. A request records that its unit runs before
  it changes the unit's state, so a request that failed part way can be sent again.
updated: '2026-11-20'
---

# An engine error in a request is answered, and the engine keeps serving

## Context

`Engine.handle` turned only `Rejected` and `InvalidContract` into a response. Any other exception left the serving loop and ended the engine process. On 2026-11-20 a `start` for a repository on another Windows drive raised `ValueError` in this way. The effects were:

- The request got no response, and the caller reported only that the engine did not answer in time. The cause was in the engine's error output alone.
- The other requests taken in the same poll had already left the inbox and got no response either.
- Every unit the engine was driving had its turn interrupted. The units continued when the next engine started, and the interrupted turns ran again.

A unit's own driving already contains such an error: the unit stops with `unknown_result` and its detail, and a retry continues it. The requests had no such boundary.

Containing the error alone is not enough when a request fails after it has changed something. `start` creates the unit, records it as running and then drives it. `answer` and `retry` recorded the event, then the new state, then that the unit runs. A failure between these steps could leave a unit created but never driven, or a state that had moved on while the status still said the unit waits. A repeated request was then refused, and no request continued the unit.

## Decision

1. An exception other than `Rejected` and `InvalidContract` while handling a request becomes the response `{"ok": false, "error": "engine error: <type>: <message>"}`. The engine writes the traceback to its error output and keeps serving.
2. After such an error, the engine drives every unit whose record says it runs, as it does when it starts (`resume`), so no unit is left recorded as running but not driven.
3. A request that continues a unit (`answer`, `retry`) records that the unit runs before it records the event and the new state. If it fails part way, the unit is driven from the state it has. When that state still holds the earlier stop, the unit reaches the same stop again, and the same request can be sent again.
4. A `start` for a unit that already exists with the same contract completes what an earlier `start` left undone: it records the unit as running if nothing records its status, and drives a unit recorded as running that is not being driven.

## Relation to existing decisions

- [The decision that a unit completes when its facts hold](adr-20261114-unit-completes-when-facts-hold.md) is unchanged: a request does not decide how a unit ends. A unit driven again from an unchanged state stops where the facts put it.
- The behavior of `Rejected` and `InvalidContract` is unchanged: a refused request changes nothing and is answered with its reason.

## Alternatives considered

- **End the engine after answering the failed request.** The caller would learn the cause, but every unit's turn would still be interrupted and the other requests of the poll would still go unanswered. Rejected.
- **Contain the error without ordering the writes.** A request that failed part way could leave a unit that no request can continue. Rejected.
- **Stop the affected unit with `unknown_result`.** A unit whose state still holds a question would then be neither answerable nor retryable. Rejected.

## Consequences

- A defect in request handling reaches the caller as an error response with its type and message, and other units are not interrupted.
- A unit whose request failed part way is driven again from its recorded state, and may reach the stop it had before. Sending the request again continues it.
- A `start` may be sent again after an engine error without creating a second unit.

## Verification

`tests/test_service.py` checks that an unexpected error in a request is answered with its type and message while the engine keeps answering, that a `start` that failed after creating the unit still drives it to its end, and that an `answer` that failed before its state was saved leaves the unit waiting for the same question, which the repeated answer then continues.

## User's decision

On 2026-11-20, to the options for handling an unexpected exception in a request (a: answer it with an error and keep serving; b: answer, then end the engine; c: keep the current behavior), the user answered "2 a で進めて" (go ahead with a).
