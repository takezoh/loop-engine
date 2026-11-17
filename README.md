# Loop Engine

## Delegate one unit of work and get back evidence, not a claim

Loop Engine carries one delegated unit of work on a host until every required fact in its contract holds, or until there is a named reason to stop. Agents make the change, run the checks and review the result inside their own sandbox. The engine chooses the next execution, judges each fact from what it can check itself, and records what happened.

`done` therefore means that the facts you asked for hold: the checks you named ran on the final candidate, the reviewers you declared independent judged every acceptance criterion, and the changed paths stay inside the scope. It does not mean that an agent said so. It is also not a guarantee of correctness beyond what your criteria, checks and reviewers cover; [Product](PRODUCT.md) says what is and is not established.

## Say what must hold, not which steps to run

A contract is data: a goal, acceptance criteria, the paths that may change, the facts that must hold, and the executions (agent, model, instructions) that can establish them. It has no step order. The engine derives the next execution from which facts hold, and a fact stops holding when what it was judged against changes. A candidate that changed after a review is checked and reviewed again without anyone scheduling it.

A new kind of unit needs no change to the engine: write a contract that follows the [rules of a unit declaration](ARCHITECTURE.md#unit-declaration).

## Get a result you can act on

A unit ends as `done`, with the change as a patch against the base revision, or it stops with a reason: a question for you, findings that stopped decreasing, a result the engine could not confirm, an existing change that the review rejected. Every stop leaves which facts hold, the findings that remain and the record of each turn. You answer the question, ask for a retry, or cancel. [How a unit stops](PRODUCT.md#how-a-unit-stops) lists each stop reason and what you can do next.

## Keep project code off the host

The engine starts two kinds of process: git, and the agent commands of the installed catalog (claude, codex, pi). It never runs a project's tests, builds or scripts. Agents run them inside their own sandbox, and the engine judges from the record of what they ran. The engine does not create, check or change a sandbox, and it refuses an agent's approval requests instead of granting them. The sandbox your agents already run in stays the only boundary around project code, and the engine depends on it.

## Run a unit

From the repository root:

```bash
# Hand over a contract and wait until the unit stops. The exit code tells how it stopped.
python -I src/loop_engine/__main__.py run <contract.json>

# Query, wait, answer, retry, cancel
python -I src/loop_engine/__main__.py status <unit_id>
python -I src/loop_engine/__main__.py wait <unit_id>
python -I src/loop_engine/__main__.py answer <unit_id> <question_id> <answer-file>
python -I src/loop_engine/__main__.py retry <unit_id>
python -I src/loop_engine/__main__.py cancel <unit_id>
```

To give a session the same operations as tools, start the engine's MCP entry in the session's directory. It is a stdio MCP server with `unit_start`, `unit_status`, `unit_wait`, `unit_answer`, `unit_retry`, `unit_cancel` and `unit_result`, and it accepts a start only for a repository inside that directory.

```bash
python -I <engine root>/src/loop_engine/__main__.py mcp
```

`run --no-wait` returns once the engine accepted the contract. The engine keeps a running unit going after the command or the calling conversation ends, and continues from its records after a restart. The engine starts agents, so it cannot be run from inside an agent's sandbox.

The contract's shape is in [Unit declaration](ARCHITECTURE.md#unit-declaration); limits, exit codes and the files of a result are in the [technical reference](docs/architecture.md).

## What the environment must provide

| Requirement | Why |
|---|---|
| git, and at least one of the agents claude, codex or pi, installed and configured for the user | They are the only processes the engine starts. Agents start with the user's own settings |
| The `cross-host-transport` package at `packages/cross-host-transport` in the directory that contains the engine's root | The engine starts agents through it. It is the engine's only package dependency; everything else is the Python standard library |
| An agent sandbox that can read but not write `LOOP_ENGINE_HOME/published/`, cannot read `LOOP_ENGINE_HOME/records/`, and can write `LOOP_ENGINE_WORKTREES` | Agents read the check program and the copy of the base revision there. What a judgment relies on must not be writable by the agent being judged. The engine assumes this and does not verify it |
| Checks defined in `.loop-engine/checks.json` at the base revision, when the contract names checks | The engine reads check definitions only from the base revision, never from the tree an agent can write |
| Linux or native Windows | The platforms the engine runs on |

| Environment variable | Meaning | Default |
|---|---|---|
| `LOOP_ENGINE_HOME` | The engine's state home. Records go under `records/`; the check program and the copy of the base revision that agents read go under `published/` | `~/.loop-engine` |
| `LOOP_ENGINE_WORKTREES` | Where unit worktrees are made | `~/.cache/loop-engine/worktrees` |

## Callers

The engine knows nothing about who calls it. A caller is anything that writes a contract and reads the result: a session over the MCP entry, a CI job over the CLI, or a product with a server of its own that mounts the engine's unit tools and replaces the start with one that composes its contracts. Callers own what the engine deliberately does not: where contracts come from, what the roles are told, and what to do with a result.

## Documents

- [Product](PRODUCT.md): the outcome, what a caller can rely on, how a unit stops, and what is not established
- [Architecture](ARCHITECTURE.md): boundaries, design principles and how a unit is carried
- [Technical reference](docs/architecture.md): contract limits, checks, exit codes, records and the engine's internals
- [Development and verification](docs/development.md): layout, tests, tools and which document owns what
- [Development guidance](AGENTS.md): the rules a change to the engine keeps
- [Decisions](docs/adr/): why each rule exists and what was considered

## License

No license has been selected yet, so this repository does not grant terms for public use.
