---
id: adr-20261114-agent-sessions-continue-and-approvals-are-refused
kind: adr
title: An execution continues its own agent session, and approval requests are explicitly refused while the turn continues
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
source_paths: []
summary: When the same execution of the same unit runs again, a follow-up message with only the difference is sent to that execution's previous session. Agents' approval requests are explicitly refused,
  the same for the 3 agents; the turn continues, and the number of refusals is kept in the unit's records.
updated: '2026-11-14'
---

# An execution continues its own agent session, and approval requests are explicitly refused while the turn continues

## Context

The Loop Engine started a new agent session for each turn and closed it. Each time the same execution ran again because work was sent back, a question was answered or a retry was made, it sent the full instructions again.

The Loop before 2026-11-13 continued the agent session that an execution observed in the next execution as well, and sent only the content that session had not yet received (`mode: start | continue` and `delivered_prompts` in `runtime/development_loop/agent_assignment_v2.py`). It did not continue a lost session. The transport still has the means to continue: `continue` of a provider session, Codex's `turn/start` on the same thread and `thread/resume` after the process is gone, ACP's `session/load` and `session/prompt`, and Pi's `--session-id`.

The handling of an agent's approval requests differed between the 3 drivers.

- To Codex, the driver always sent a no-approval setting in `thread/start` and `thread/resume`, so that no request arose at all.
- ACP (claude) returned `cancelled` to a permission request, or chose `allow_once` if permission had been given through an environment variable. Nothing in the repository sets this environment variable.
- Pi did not answer the extension's dialog, and the turn sometimes did not return.

Item 2 of the [engine boundary decision](adr-20261114-loop-engine-host-boundary.md) and `DEVELOPMENT.md` §11 state that neither the engine nor the transport passes a sandbox or approval setting to agents, and that agents run with the user's settings as they are. The no-approval setting violated this rule.

## Decision

The user decided the following on 2026-11-14.

### Continuing sessions

1. **When the same execution of the same unit runs again (findings sent back, an answer to a question, a retry), the engine sends a follow-up message to that execution's previous session.**
2. **Different executions do not share a session.** A reviewer never sees the writer's session.
3. **A follow-up message carries only what is new (findings, answers, changed facts) and the reply format.** Only the first message of a session sends the full instructions.
4. **When a session cannot be continued (the engine restarted and cannot resume the session, or the provider lost the session), the engine starts a new session with the full instructions.**
5. **The engine keeps the token for resuming each execution's session until the unit finally stops.** The session on the agent side is released each time a turn ends, and is resumed with that token when the same execution runs again. The engine confirms the release before it reads the working copy. When the unit finally stops, the engine discards the tokens and closes the transport's bindings. Conversations the agent saved are not deleted.
6. **Reviewers follow the same rules.** A reviewer's later review continues the reviewer's own session (user: "Reviewer: continue" (translated from Japanese)).

### Approval requests

7. **An agent's approval request is answered with an explicit refusal, and the turn continues.** This is the same for claude, codex and pi (user: align the 3 agents on "return the refusal explicitly, and the turn continues"). No approval setting is passed to agents. The engine and the transport have no path that approves. The number of refusals is kept in the unit's records.

## Alternatives considered

- **Start a new session for each turn (the engine until now):** Not adopted, because of rules 1 and 3. Each time an execution runs again the full instructions are sent again, and the agent loses the context of its work so far.
- **The reviewer reviews in a new session every time:** The user decided "Reviewer: continue" (translated from Japanese). Independence is kept by rule 2 (a separate session per execution).
- **Stop the unit when there is an approval request:** The user's decision is "the turn continues". A check that could not be run because its request was refused leaves no passing line in the execution record, so that fact does not hold. Even without stopping, this can be handled as it is under the [decision on completion by facts](adr-20261114-unit-completes-when-facts-hold.md), which judges by facts holding.
- **Raise the request to the main session and wait for a decision:** This becomes a path that decides from outside the engine whether to approve. The [engine boundary decision](adr-20261114-loop-engine-host-boundary.md) requires that the engine not be involved in the sandbox and approval, and the user's settings decide the agent's approvals. The engine only refuses the request, records the count and makes it visible.

## Consequences

- A turn for work sent back, an answer or a retry receives only the difference instead of the full instructions. Because a follow-up message also includes the reply format, the engine can check every reply by the same rules.
- The engine stores each execution's session token in the unit's records and does not interpret its content. Even if the engine restarts, it can continue the same session from the records.
- Between one turn and the next, the agent's session is released. To continue, Codex resumes the same thread, ACP reloads the session, and Pi starts with the same session id. A turn whose release could not be confirmed is not treated as completed, and the working copy is not judged in that turn.
- When the transport reports that a session cannot be continued, the engine retries that turn only once, in a new session. A failure of the new session is treated the same as any other failed turn.
- The execution record is delimited per turn. Even in a continued session, a check line of an earlier turn never establishes a fact of a later turn.
- An operation that requires approval is not run inside the agent unless the user's settings allow that operation. For an operation that runs without approval under the user's settings, no request arises at all.
- The number and content (kind and short description) of refused requests are kept in the turn's record, and appear as a count per execution in the unit's status, the record of the stop reason and the aggregation tool.
- How agents interpret a follow-up message, and how far each provider keeps a session, need to be confirmed with real agents.

## Verification

- The engine's tests confirm that sending work back continues the writer's session with a message carrying only the difference, that an answer continues the session of the execution that asked the question, that a reviewer's second run continues the reviewer's own session, that 2 executions do not receive the same token, that a session that cannot be continued is replaced by 1 new session with the full text, that tokens remain across an engine restart, that a final stop discards the tokens and closes the bindings while a stop that can be continued keeps the tokens, that the release of the session is awaited before a turn's result is returned, that a check line of an earlier turn does not establish a fact, and that cancellation interrupts a continued turn.
- The transport's tests confirm, for each of the 3 drivers, that the answer the agent receives is an explicit refusal, that the turn then ends normally, and that the refusal is kept in the turn's record. They also confirm that Codex's `thread/start` and `thread/resume` have no approval setting, and that ACP does not choose to allow under any environment variable.
- skill-lint's `producer_environment` (C16) refuses the placing of an approval setting.


{% transition from="proposed" to="accepted" date="2026-11-14" %}
The user approved on 2026-11-14. For continuing sessions, the reply to the presented rules 1-5: "Reviewer: continue" (translated from Japanese). For approval requests: align the 3 agents on "1. return the refusal explicitly, and the turn continues".
{% /transition %}
