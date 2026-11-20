# Loop Engine development and verification

For people and agents who change the engine. Read [README.md](../README.md), [PRODUCT.md](../PRODUCT.md) and [ARCHITECTURE.md](../ARCHITECTURE.md) first. The rules a change must keep are in [AGENTS.md](../AGENTS.md), the [design principles](../ARCHITECTURE.md#design-principles) and the accepted ADRs in [adr/](adr/).

## Which document owns what

| Concern | Owner |
|---|---|
| What the engine is for and how to run it | [README.md](../README.md) |
| What a caller can rely on, how a unit stops, what is not established | [PRODUCT.md](../PRODUCT.md) |
| Boundaries, design principles, the structure of a contract, how a unit is carried | [ARCHITECTURE.md](../ARCHITECTURE.md) |
| How to work on the engine: authority, change discipline, what to verify and report | [AGENTS.md](../AGENTS.md) |
| Contract limits, checks, exit codes, record files, module responsibilities and internals | [architecture.md](architecture.md) |
| How agents are started, observed and stopped; the catalog; the Claude ACP adapter | [transport.md](transport.md) |
| Why a rule exists and what was considered | An ADR in [adr/](adr/) |
| What was observed or investigated at a point in time | A note in [note/](note/) |
| The text a role receives | `src/loop_engine/protocol/` |
| How a caller assembles contracts and what it does with a result | The caller's own documents. The engine's documents do not describe callers |

Architecture documents describe the current state only. History, earlier structures and rejected alternatives stay in ADRs and in git history. Documents under `changes/`, `issue/` and `archived/`, the notes and the context sections of ADRs describe the situation at the time they were written. The engine was first developed inside the dev-skills repository, one of its callers, so these records name dev-skills, its documents and its earlier Loop designs; some predate the current engine.

Documents, code comments and protocol texts under `loop-engine/` are written in English.

## Layout

| Path | Content |
|---|---|
| `src/loop_engine/` | The engine. `facts.py` and `prompts.py` work on values only, and only `platform_facts.py` tells the platforms (Linux and native Windows) apart |
| `src/provider_transport/` | The provider transport: the catalog of agents, a driver per wire protocol, provider sessions, the admission of child processes, and the launcher of the Claude ACP adapter |
| `src/loop_engine/protocol/` | The protocol texts handed to agents (each role's first message of a session and its follow-up message, for the writer, the reviewer and the reporter, and the question every message ends with) |
| `tests/` | Tests that use real git repositories and a real engine process. Agents are scripted fakes |
| `tests/transport/` | Tests of the provider transport, with fakes of the wire protocols and of the platform APIs |
| `tools/` | Aggregating records, and running scenarios with real agents |
| `docs/` | Decisions (ADRs) and structure, kept as a dev-docs docs root (frontmatter, schemas and relations checked by the dev-docs CLI) |

Only `src/loop_engine/composition.py` connects the engine to the provider transport, and the transport imports nothing of the engine. Both use only the Python standard library.

## Checks to run

From the repository root:

```bash
python -m unittest discover -s tests          # the engine's tests
python tools/unit_report.py                   # aggregate the recorded units
```

After changing a document under `docs/`, run the lint of the dev-docs CLI from the repository root. dev-docs is a tool of dev-skills, so the command comes from a dev-skills installation:

```bash
<dev-skills>/runtime/python @/runtime/document_graph/docs_cli.py lint
```

Running a scenario starts real agents and uses their quota. Run it only with the user's approval, after showing what will run ([tools/README.md](../tools/README.md)).

```bash
python tools/scenarios.py run tools/scenarios/median-fix
```

## Verification map

| What is verified | Test |
|---|---|
| The decision rules, the judgment rules, whether a finished turn counts, admission of a contract, the structure-only check, sending work back only while findings decrease, answers and the basis of a review, counting files read in earlier turns | `tests/test_facts.py` |
| The flow with a real repository and engine, stop reasons, resuming, cancellation, that paths handed to a role point only into the published root, that a reviewer's question waits for its answer and the review is judged again | `tests/test_unit_flow.py` |
| Continuing a session, follow-up messages, a session that cannot be continued, closing sessions, a turn that was not released, refusal counts, that every message of every role ends with the one question form | `tests/test_sessions.py` |
| Admission of requests, request files, the CLI, starting the engine process, the names of the state home and the check file, that an error in a request is answered and a request that failed part way can be completed, that a record another process reads is never replaced | `tests/test_service.py` |
| The unit tools (closed schemas, the boundary of a connection, passing requests and reading records), the MCP server's messages, the `mcp` process, refusing an agent running inside a unit | `tests/test_unit_tools.py` |
| The check program | `tests/test_check_runner.py` |
| Publishing the check program (the directory named by the content digest, engines started at the same time, not using a rewritten copy), that the command handed to a role names the published copy | `tests/test_check_program.py` |
| That environment entries are not counted and everything else is, matching digests, the digests of empty directories and of the agent configuration, recording the names left out | `tests/test_environment_entries.py` |
| Reading the base revision from git's record and the engine's copy, judging `fails_on_base`, that judgment fails on a tree that cannot be read, that the kind of an agent configuration name does not matter | `tests/test_judged_trees.py` |
| That git is the only process started, the extent of the pure modules, that entries import no decision module, that no engine module decides containment of paths other than through `facts.inside`, that a repository's hooks and filters do not run | `tests/test_closed_paths.py` |
| Decisions given Windows facts as values (absolute paths, paths inside the repository, whether a path lies inside a root when either is on another drive or share, quoting of the check command), that only `platform_facts.py` tells platforms apart, the contracts of the `place_new` and `lock` adapters, that identifiers do not accept a trailing newline, the reasons a reviewer's reply is not accepted | `tests/test_platform_facts.py`, `tests/test_facts.py` |
| The contract of the agent port (applied to both the connection to the transport and the scripted fake), returning a turn after the session is released, the order of release and process end on stopping, judging the worktree after the release | `tests/test_transport_agents.py` |
| That every adapter and every fake of a port runs that port's contract tests, and that no class implements a port without them | `tests/test_port_contracts.py` |
| The verification tools | `tests/test_tools.py` |
| The provider transport: drivers, provider sessions, process admission, the catalog with and without excluded skills, the Claude adapter and its launcher | `tests/transport/` ([what they cover](transport.md#verification)) |
