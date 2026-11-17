# Loop Engine — Product

Loop Engine carries one delegated unit of work on a host until every required fact in its contract holds, or until there is a named reason to stop. Agents make, check and review inside their own sandbox. The engine holds only the rules for choosing the next execution and judging facts, and the records.

## Outcome and acceptance

Success means that a caller hands over a unit and gets back one of two things: a change whose required facts all hold, or a stop with a reason, the findings that remain and enough record to decide what to do next. Neither rests on an agent's own statement that it finished, passed or approved.

A started agent, a completed turn and a reviewer's reply are intermediate. Acceptance also requires that a fact holds only for the inputs it was judged against, that a result the engine cannot confirm is not counted, that every unit stops after a finite number of rounds, and that the caller can tell from the stop reason alone whether to answer, retry or accept the result.

`done` means that the facts the contract names hold. Whether that makes the change correct depends on the acceptance criteria, the checks and the reviewers the caller chose; the engine does not judge the work itself. [What is not established](#what-is-not-established) lists the limits.

## Who it is for

| Caller | Why the engine fits |
|---|---|
| A session that delegates work to agents and keeps working meanwhile | The engine's MCP entry gives it tools to start a unit from a contract, follow it, answer it and read its result. The unit goes on when the session's connection or conversation ends |
| A product that builds contracts for its users | It mounts the engine's unit tools in its own server and replaces only the start, so its sessions never write a contract themselves |
| CI that must turn agent work into pass or fail | `run` waits until the unit stops and returns an exit code per stop reason |
| Someone designing a new kind of delegated work | A contract that follows the rules runs as it is. The engine knows no role names, skills or judgment knowledge of the side that wrote the contract |

The engine has no preferred caller. Anything that writes a contract and reads the records is one, and several can use the same engine.

## What a caller can do

| Need | Current behavior |
|---|---|
| Delegate a unit without scripting its steps | The contract lists facts and the executions that can establish them, with no order. The engine picks the first execution whose inputs hold and whose facts are still open |
| Know that the checks ran on the final candidate | The writing agent runs a check program that the engine placed. The engine reads only that program's status line from the execution record and accepts it when its digest is the present tree |
| Know that a new test fails without the change | A check can be expected to fail on the base revision: the candidate's tests run against a copy of the base revision that the engine placed |
| Get a review that does not rest on the writer's account | A reviewing execution has its own session and never receives the writer's report. Unless the contract allows otherwise, its agent and model pair differs from the executions it is declared independent of |
| Get a verdict that can be checked | A reviewer judges each acceptance criterion and never states approval. The engine counts a reply only when its quotes are in the present files, its evidence commands ran in that turn and the changed files were read. The engine computes pass or fail |
| Keep a change inside agreed paths | A `git` fact compares the changed paths with the scope, against the base revision recorded when the worktree was made |
| Have an existing change reviewed | With `unit.base`, the unit covers the commits from that base to the repository's current commit |
| Let rework continue only while it helps | A refuted fact goes back to the execution that made its subject only while each refutation leaves fewer findings than the one before. There is no round limit to tune |
| Decide a question an agent cannot | A turn can end with a question. The unit waits, and the answer reaches every execution; reviews judged before it are judged again |
| Keep one session's units apart from another's | The unit tools serve the session's directory: a start is accepted only for a repository inside it, and a unit another connection started does not exist for it |
| Keep an agent inside a unit from loading chosen skills | The contract can name skills of one plugin to exclude. Everything else of the user's settings, plugins and skills loads as usual |
| Leave and come back | The engine process is separate from the entry. It continues from `state.json` after it restarts, and each execution continues its own agent session |
| Read what happened | Each unit has a status, a stop record with facts and findings, the patch against the base revision, and the reply and execution record of every turn |

## Concepts

| Concept | Meaning |
|---|---|
| Unit | One delegated piece of work: a goal, acceptance criteria, and a scope of paths that may change |
| Contract | The unit, the facts that must hold and the executions that can establish them. It is data the caller hands over, and the only thing the engine receives |
| Fact | Something that must hold for the unit to be complete. A fact holds only while what it was judged against stays the same |
| Execution | An agent, a model, an effort and instructions, with the facts it reads and the facts it establishes. One execution can run many turns |
| Turn | One run of an execution's agent, from a message to a reply, with the record of what the agent did |
| Role | How the engine addresses an execution: a writer may change the worktree, a reviewer judges criteria, a reporter writes a report |
| Finding | What a refuted fact names as wrong: a failed check, a path outside the scope, or a reviewer's finding at or above the blocking severity |
| Stop reason | Why the unit is no longer running |

The five kinds of fact differ in who establishes them.

| Kind | Holds when |
|---|---|
| `given` | From the start. It stands for the tree as it is, so facts judged against it stop holding when the tree changes |
| `report` | The one execution that establishes it completes a turn that does not end in a question |
| `checks` | The execution record shows each named check run on the present tree with the expected result |
| `verdict` | Every reviewing execution judged the present subject and no criterion is unmet and no finding reaches the blocking severity |
| `git` | The subject holds and every changed path is inside the scope |

A fact marked `required: false` does not count toward completion; other facts can still be judged against it.

## How a unit stops

| Stop reason | What happened | What the caller can do |
|---|---|---|
| `done` | Every required fact holds | Take the patch |
| `waiting_for_answer` | A turn ended with a question for the requester | `answer`; the unit continues |
| `stagnation` | A refutation left no fewer findings than the previous refutation of the same fact, or the writer or reporter returned the same candidate or report after findings | Final. The stop record has the fact, its findings and both counts |
| `unknown_result` | A turn did not complete, its session was not released, the worktree could not be read in full, or the agent could not be started | `retry`; the stop record's detail names the cause |
| `invalid_result` | A reviewer's reply could not be confirmed twice | `retry`; the reasons name what the reply lacked |
| `rejected` | A fact was refuted and no execution makes its subject, as when reviewing an existing change | Final. The findings are the result |
| `stuck` | Required facts are open and no execution can run | Final. The contract cannot be completed as written |
| `unsafe_agent_config` | Agent configuration in the worktree changed after the worktree was made, or something the engine did not create is above the worktree | Final. No agent was started on that tree |
| `cancelled` | The caller cancelled | Final |

After a final stop the engine removes the worktree and the copy of the base revision; the change stays as `result/patch.diff`. The engine does not retry by itself. Exit codes are in the [technical reference](docs/architecture.md#stop-reasons-and-exit-codes).

## Who is responsible for what

| Party | Responsibility |
|---|---|
| Caller | Writes the contract: goal, criteria, scope, facts, executions, and each execution's instructions and judgment standards. Decides what to do with a result, answers questions, asks for a retry or cancels |
| Engine | Validates the contract, chooses the next execution, judges facts from the check program's status line, quotes, the execution record and git's record, sends refuted work back while it converges, and writes the records |
| Agent | Makes the change, runs the checks and reviews, inside its own sandbox and with the user's own settings |
| Environment | Provides the sandbox, and keeps what agents can read and write apart: agents write only the worktree, read the published root and cannot read the records |
| Project | Defines its checks in `.loop-engine/checks.json` |

The engine does not interpret an execution's `instructions`. Goal, criteria, scope, check commands and the form of the reply come from the engine's protocol texts; behavior and judgment standards come from the caller.

## What is not established

- **Correctness of the work.** The engine establishes that the named facts hold. A weak criterion, a missing check or a careless reviewer yields `done` on weak evidence.
- **The sandbox.** The engine does not create, check or change it. If an agent's sandbox is off, can write the published root or can read the records, the engine does not notice, and checks and independent review lose their meaning.
- **What happens inside a check run.** The status line shows that the check program read the present candidate and, for a check on the base revision, copied the base the engine placed. What the tests do after that happens inside the agent's sandbox.
- **Who may write a contract.** A session connected to the engine's own MCP entry can start a unit from any contract, with any agent, model and instructions, for a repository inside its directory. The engine validates the contract and does not restrict its author. A caller that wants roles fixed offers its own start.
- **Approvals.** Approval, permission and user-input requests from an agent are refused and the turn continues. There is no path to grant one. A check that could not run because of a refusal leaves no passing line, so its fact does not hold.
- **Recovery without the caller.** A result that cannot be confirmed stops the unit. Only the caller's `retry` runs it again; the one exception is a session that cannot be continued, which the engine restarts once with the full instructions.
- **A quiet worktree root.** Another agent can place files in the worktrees root between the engine's check and the start of an agent. Closing that gap belongs to the environment's sandbox settings.
- **Behavior of real agents.** The engine's tests use real git repositories and a real engine process with scripted agents. How claude, codex and pi behave under a contract is observed only by running [scenarios](tools/README.md), which use agent quota and need the user's approval.
- **Other agents and platforms.** The supported agents are claude, codex and pi. The engine runs on Linux and native Windows.

[Architecture](ARCHITECTURE.md) owns the boundaries and the rules that keep them. The [technical reference](docs/architecture.md) owns contract limits, exit codes and record files. [Decisions](docs/adr/) own the reasons.
