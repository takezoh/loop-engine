---
id: note-20261111-loop-mcp-access-boundary
kind: note
title: Loop MCP operation boundary and sandbox access design proposal
status: draft
created: '2026-11-11'
tags: []
owners: []
relations: []
source_paths: []
summary: The settled boundary that keeps the Loop / driver inside the sandbox and limits host resource access to the path through MCP's check and authorization. The earlier consolidation proposal is withdrawn.
---

## Settled boundary

User instruction: "The loop runs inside the sandbox. Starting a sub agent (subsession) requires crossing the sandbox. MCP provides that by inspecting it on the host side. Access to worktrees and the state directory is presumably the same. Never break this boundary again." (translated from Japanese)

The design principle for development was recorded in DEVELOPMENT.md §11. The Loop / driver stays inside the sandbox, and operations on host resources go through an MCP tool call and the check and authorization behind it. A custom connection that calls the same handler does not satisfy this boundary. Embedding an executor in the driver is not allowed, and neither is moving the driver to the host side.

## Withdrawn proposal

The proposal made earlier in this note, "consolidate Loop operations into MCP", is withdrawn. It confused control of the Loop with access to host resources. The proposal to give the CLI direct access, running everything outside the sandbox across the board, and continued use of the existing embedded executor path are not adopted either.

## Implementation observed at the time of the decision

_production_host in runtime/development_loop/driver_process.py calls open_execution_host. packages/cross-host-transport/src/cross_host_transport/subsession/execution_host.py embeds the MCP handler in the caller's process. This path, and the across-the-board outside-the-sandbox designation in runtime/development_loop/instructions.md, must be made consistent with the boundary above. At the time of this record, the implementation fix and the behavior verification had not been done.

The follow-up design makes concrete the contract by which the driver inside the sandbox calls an independent host-side MCP. It covers startup, state and worktree operations, reconnection, and prevention of side effects on refusal, and it does not reimplement the business rules of the existing ledger and lease.

For the record of the implementation fix and its verification, see the [change package](../../../docs/changes/change-20261111-loop-sandbox-mcp-boundary/change.md); for the current structure, see [subsession architecture](../../../docs/architecture/subsession.md).
