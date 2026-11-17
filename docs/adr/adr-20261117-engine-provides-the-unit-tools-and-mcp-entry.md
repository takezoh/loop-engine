---
id: adr-20261117-engine-provides-the-unit-tools-and-mcp-entry
kind: adr
title: The engine provides the unit tools and a stdio MCP entry, and a caller adds
  only its own start
status: accepted
created: '2026-11-17'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261117-engine-names-its-own-home-and-check-file}
source_paths: []
summary: The engine owns the unit tools (start from a contract, status, wait, answer,
  retry, cancel, result), the boundary of a connection and a stdio MCP server on the
  standard library. A caller that builds contracts mounts the tools and replaces only
  the start.
updated: '2026-11-17'
---

# The engine provides the unit tools and a stdio MCP entry, and a caller adds only its own start

## Context

[The engine boundary decision](adr-20261114-loop-engine-host-boundary.md) makes MCP the interface between a session and the engine, and its item 1 says that the CLI and MCP entries only pass requests to the engine and read its records. The engine had the CLI entry and the client that both entries use. The MCP entry itself was in dev-skills, the caller in whose repository the engine was developed: the tool schemas, the rules that bound a connection, and the refusal of an agent running inside a unit. It sat next to the one part that belongs to that caller, the composition of a contract from a Profile.

So a caller other than dev-skills had no MCP entry, and rules that protect the engine's units from another connection were kept by a caller. The engine is being separated from the dev-skills repository.

## Decision

1. **The engine owns the unit tools.** They are `unit_start` (which takes a contract), `unit_status`, `unit_wait`, `unit_answer`, `unit_retry`, `unit_cancel` and `unit_result`. Each has one purpose and a closed input schema. No tool takes a program, an argument list, an environment, a path or the state home.
2. **The engine owns the boundary of a connection.** A connection is a session's directory. A start is accepted only for a repository that is a directory inside it. A unit that another connection started does not exist as far as this connection can tell, and a start under that unit's id is refused without describing it. The entry does not open for an agent the engine started, and not when the engine's records lie inside the connection's directory.
3. **The engine ships a stdio MCP server for these tools on the standard library alone.** `mcp` on the engine's main starts it for the connection at the current directory. The transport stays the engine's only package dependency.
4. **A caller that builds contracts mounts the engine's tools in its own server and replaces only the start.** It checks the start against the engine's boundary, composes the contract and hands it to the engine. Whether such a caller also offers the start that takes a contract as written is the caller's decision.
5. **The tools and the server are entries.** They hand requests to the engine as files and read its records, hold no progress, judgment or recording logic, and import no decision module. The entry checks a tool's arguments; the engine validates the contract.

## Relation to existing decisions

- Items 1 and 6 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) (entries only pass requests and read records; contracts and answers are passed as data, as input to MCP tools) are unchanged. The engine now keeps them for the MCP entry itself.
- Items 4 and 7 of the same decision (the engine is an independent package whose only dependency is the transport) are kept by item 3: the server uses no MCP library.
- That decision counts MCP tool input among the paths by which something could be run on the host, each with a refusal test. The refusal tests for tool input are now the engine's.

## Alternatives considered

- **Move the tools and the boundary to the engine and leave the server to callers.** The engine would still not be usable over MCP by itself. The user did not choose it.
- **Leave the MCP entry with the caller.** Each caller would write the same tools and the same boundary, and the engine's units would depend on every caller keeping the rules. The user proposed the move.
- **Build the server on an MCP library.** The engine would gain a package dependency for four requests of the protocol. The option the user chose keeps the standard library.

## Consequences

- Any caller can give a session the unit tools by starting the engine's entry. A caller with a server of its own reuses the tools and the boundary.
- A session connected to the engine's own server can start a unit from any contract, with any agent, model and instructions the contract names, for a repository inside its connection. The engine validates the contract; it does not restrict who writes one. A caller that wants a role's instructions and placement fixed offers its own start and does not offer this one.
- The server implements the part of the protocol a tool server needs: `initialize`, `ping`, `tools/list` and `tools/call`, for the protocol revisions 2024-11-05, 2025-03-26 and 2025-06-18. It offers no resources or prompts, sends no notifications and does not cancel a call that is running. A later revision of the protocol needs a change to the server.
- The entry's argument check reads only the schema keywords the tools use. A tool that needs another keyword needs the check extended.

## Verification

`tests/test_unit_tools.py` confirms that a start hands the contract to the engine unchanged, that a repository outside the connection and an id held by another connection are refused before anything is sent, that a unit of another connection does not exist for this one, that anything outside a tool's schema is refused, that no tool takes a program or a place, and that the entry does not open for an agent running inside a unit or when the records lie inside the connection. The same file drives the server with protocol messages, in process and as the `mcp` process, and confirms that reading a unit starts no engine. `tests/test_closed_paths.py` confirms that the two entry modules import no decision module and start no process.

On 2026-11-17 the official MCP client library (`mcp` 1.13.1) connected to the entry, negotiated revision 2025-06-18, listed the seven tools and received a refusal as a tool error. This is an observation of that day, not a test in the suite.

## User's decision

On 2026-11-17 the user asked "Wouldn't it be better for the engine to provide MCP?" (translated from Japanese). Asked how far to implement it, the user chose "Move the tools and the server to the engine", whose explanation named the start that takes a contract, the other six tools, the boundary of a connection, a stdio MCP server on the standard library alone with no added dependency, and dev-skills keeping only the start that composes a contract from a Profile. Asked whether the dev-skills server should also show a session the start that takes a contract as written, the user chose "Do not show it".
