---
id: adr-20261114-loop-engine-host-boundary
kind: adr
title: Loop Engine runs on the host, does not manage a sandbox, and creates no path that runs an LLM or arbitrary code on the host
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations: []
source_paths: []
summary: The engine is fixed code that runs on the host, and the sandbox is left to the environment (the sandbox settings of claude, codex and pi). Project
  code and the LLM run only inside the agent's sandbox. MCP is the interface to agents, and CI uses the same engine too.
updated: '2026-11-14'
---

# Loop Engine runs on the host, does not manage a sandbox, and creates no path that runs an LLM or arbitrary code on the host

## Context

The change of 2026-11-13 (commit `0540f7a5`) moved the Loop to an MCP service on the host and gave it its own bwrap isolation in order to run project commands. The same change rewrote the rules of `DEVELOPMENT.md` §11 without a record of approval. Between November 9 and 13, the way the Loop is controlled changed 5 times.

The sandbox is provided by the sibling repository agent-environments, under a policy common to claude, codex and pi. There is no reason for dev-skills to have a separate sandbox. That repository's decision requires of a path that runs something outside the sandbox that "the program that runs is fixed, and the caller cannot choose a child program" (translated from Japanese) and that "the program itself is not in a place writable from the sandbox".

The facts and measurements of the investigation are in the [investigation record](../note/note-20261114-loop-engine-redesign-investigation.md).

## Requirements the user stated (2026-11-14)

- "dev-skills should not have a sandbox. If it has one, the implementation is breaking the original requirement." (translated from Japanese)
- "I want it to be usable for CI as well. MCP would do as the interface between the agent and the loop engine."
- "The engine can run on the host. The point is not to create a path by which an LLM or arbitrary code is run on the host side."
- "The loop engine does not manage a sandbox. The sandbox is something the environment provides. At present, what provides the sandbox is the sandbox settings of claude, codex, pi."
- "Make claude and codex behave the same and add no complexity. Unify on stopping. The supported agents are the 3: claude, codex, pi"
- "Keep the starting of agents abstracted as it is now (claude, codex, pi, ACP, AppServer)"

## Proposed decision

1. **The engine is fixed code that runs on the host, and it runs as one process separate from the MCP server.** This process runs multiple units and does not create a process per unit. The CLI and MCP entries only pass requests to the engine and read the state records; they hold no processing for progression, judgment or recording. In CI, the CLI starts the engine, waits until the unit it passed stops, and receives the stop reason as an exit code. If the engine stops, it resumes from the records at the next start.
2. **The engine does not create, verify or change a sandbox.** It has no isolation such as bwrap. It passes no sandbox or approval setting to the agents it starts, and starts them with the user's settings as they are. It has no per-agent exceptions and no selection by whether write locations can be enforced.
3. **LLM output and project content do not decide the programs, arguments or settings that run on the host.** The engine starts only git and the agent start commands in the installed catalog. The engine does not run project commands (test, build, script).
4. **Agents are started through the existing abstraction.** The common provider processing and the per-protocol drivers (codex-app-server, acp-v1, pi-rpc) are kept. The engine depends only on a port in its own words: "start of a turn, progress, result, cancellation".
5. **The engine stops for agent configuration in the working directory only when it has been changed.** The targets are `.claude/`, `.codex/`, `.pi/`, `.agents/` and `.mcp.json`. The engine compares the hash of their content right after creating the worktree and right before starting an agent, and stops if they differ. Definitions that were there from the start are left to be read as project content. This treatment is the same for the 3 agents.
6. **State is placed in `~/.dev-skills`, and the engine is its only writer.** Contracts and answers are passed as data, as input to MCP tools.
7. **The engine is an independent package inside the same repository and is started from the installed copy.** It does not load code from the working tree. Because it is expected to be split out into a separate repository in the future, it must not depend on the plugin's code.

The paths by which something can be run on the host are treated as 8: MCP tool input, the unit contract and check configuration, agent output, start arguments, git, agent configuration in the working directory, the engine's own code, and executable resolution. Each has a refusal test.

## Relation to existing decisions

| Decision | Treatment |
|---|---|
| [host MCP service](../../../docs/adr/adr-20261113-loop-host-mcp-service.md) | Running on the host and connecting through purpose-specific MCP tools is kept. The ledger and recovery machinery is reduced |
| [isolated execution](../../../docs/adr/adr-20261113-loop-effect-isolation.md) | Replaced. Because the engine does not run project commands, the isolation port is no longer needed |
| [producer's environment](../../../docs/adr/adr-20261113-producer-development-loop-skill.md) | Kept. The producer runs in the user's environment, and only the skill that opens a Loop is excluded |

The user approved items 6 and 7 on 2026-11-14 ("6. approve", "7. approve it will probably be made independent in the future" (translated from Japanese)). How processes are split in item 1 follows the user's instruction ("Wouldn't it be better to separate the process? Choose the simpler one", "There is no need to make a process per task").

The user approved this ADR as a whole on 2026-11-14 ("1 approve"). With this approval it replaces [isolated execution](../../../docs/adr/adr-20261113-loop-effect-isolation.md). The implementation does not yet follow this ADR. The corresponding parts of `DEVELOPMENT.md` §11 and `AGENTS.md` are synchronized after the user approves the wording. On the agent-environments side, the engine needs to be added to the inventory as a path that is run outside the sandbox.

## Alternatives considered

- **Return the Loop to a CLI inside the sandbox:** From inside the sandbox, git writes, saving state and starting agents are not possible (measured). This dependency repeatedly produced paths that cross the boundary.
- **The engine runs check commands on the host:** Many of the reference harnesses take this form, but code rewritten by the producer runs with the host's privileges.
- **Run the engine inside the MCP server process:** There would be 2 forms of running, one for conversation and one for CI. When the MCP server stops the units stop too, and the MCP entry holds the engine's processing.
- **Create a process per unit:** Not needed. The number of processes grows, and the agent's host process cannot be shared between units.
- **Replace the starting of agents with direct launch of a non-interactive CLI:** Not adopted, because of the instruction to keep the existing abstraction.
- **Stop at the start if the base revision has agent definitions:** Repositories that have definitions, dev-skills included, could no longer be targets.

## Consequences

- The engine's own isolation, the worker that runs inside it, and the ledger for run-once execution are no longer needed.
- When a definition that was there from the start calls a script in the repository, room remains for a script rewritten by the producer to run on the host. This is the same treatment as an ordinary session.
- Whether the agent's sandbox is in effect is the environment's responsibility, and the engine side does not guarantee it.
- Because there is 1 engine process, the agent's host process can be shared between units.
- The means by which an entry passes requests to the engine is decided in the implementation design.
- Running units keep progressing even after the conversation or the MCP server ends.

## Verification

- Neither package mentions `bwrap` or `unshare`.
- In a test that records the executables that were started, 0 are outside the list.
- For each of the 8 paths there is a test that confirms that input with commands or paths mixed in starts nothing and changes no state.
- The modules for progression and judgment do not import file, time or process.

In the PoC, these held in 38 tests that used a fake agent. The same tests also confirmed that the CLI entry returns a different exit code for each stop reason and that the same command can resume after a forced termination. The current implementation has no entry other than the MCP server, so the CLI entry has to be newly built.

A check with real agents through the existing abstraction (2026-11-14, 3 turns per agent) confirmed the following.

- The common provider processing can be assembled from an ordinary process, without the MCP server, and can run turns. The engine can be a separate process.
- The current drivers pass settings that violate item 2. They always send codex no approval (`approvalPolicy: never`) and their own sandbox (`workspace-write`), and in ACP they automatically refuse permission requests from the agent.
- Under the driver's `workspace-write`, `~/.ssh` could be listed. Under the user's permission profile it was not visible. The driver's own setting is looser on reads.
- How configuration in the working directory is loaded differs by agent. claude starts hooks and MCP servers, codex starts only MCP servers, and pi starts neither. The check of item 5 applies to the same targets regardless of this difference.
- The agents and the sandbox themselves create empty files and directories in the working directory. The hash of item 5 has to be computed excluding these.
