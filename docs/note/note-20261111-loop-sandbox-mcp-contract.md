---
id: note-20261111-loop-sandbox-mcp-contract
kind: note
title: Implementation contract proposal for the Loop inside the sandbox and the host MCP
status: draft
created: '2026-11-11'
tags: []
owners: []
relations: []
source_paths: []
summary: Resource operations go through MCP tools. Withdraws the custom HTTP / socket connection proposal and
  revises the design so that the conductor runs a sandbox command's tool request as an MCP tool and returns the result.
---

## Withdrawal of the connection proposal (2026-11-12)

The custom HTTP / Unix socket connection and the injection of connection information that this note specified are withdrawn. The note had wrongly reinterpreted the user's instruction to call MCP tools as meaning that reaching the same host handler was enough. The separation of responsibilities for resource operations below does not justify that custom path. The connection implementation in eb331b40 is nonconforming, and the follow-up change-20261112-loop-mcp-tool-dispatch handles its removal and the correction to MCP tool calls.

## Outcome and structure

Implementation contract. The settled boundary follows ADR adr-20261111-loop-sandbox-mcp-boundary. The CLI, the kernel, the Unit Driver and the decision logic are placed inside the sandbox. Authorization, persistence, worktree operations and the existing cross-host executor are placed in the host-side MCP. The placement and operating permissions of the producer follow the existing transport contract.

```text
sandbox: CLI → kernel / driver → resource ports + execution port
                                      ↓ MCP
host: admission → state / worktree adapters + cross-host executor
```

## Operation contract

| port | Input and result | Host-side responsibility |
|---|---|---|
| LoopState | handle bound to a repository, snapshot revision, update content → revision or conflict | state read and write, mutual exclusion, atomic commit, check of the permitted scope |
| ArtifactStore | handle, content and digest of a managed artifact → stored handle / content | managed storage and retrieval of inputs, outputs and outcomes. Does not expose arbitrary absolute paths |
| RepositoryWorkspace | repository/worktree handle and the required read and change operations → snapshot / change result | check of paths and symlinks, Git/worktree side effects, authorization of the operation scope |
| ProducerExecution | existing subsession start/continue/observe/recover/cancel and so on | provider startup, receipt, revision, observation of the real process |

Ports are defined in the vocabulary of the using side and do not expose a general-purpose shell or arbitrary file read/write. The kernel in the sandbox owns the meaning of the state and the decision on the next Execution. The host guarantees the atomicity of storage and authorization, and does not choose the next Execution. A change commit is a compare-and-swap with an expected revision, and a ledger update across multiple files is stored as one transaction. The update that the kernel computed from the snapshot is applied; on a conflict, the kernel reloads and decides again.

The first repository handle is issued from the origin at server startup and the existing CwdAuthority. Worktree creation and registration of a reference repository are explicit operations within that authority. A handle is checked against the server's scope, and possession alone is not authorization. The state root is determined by the server configuration and the caller cannot change it. The input goal/source is sent as content and stored, without assuming shared access. Outcomes are also retrieved as content or as a resource, and an arbitrary output destination on the host is not accepted.

## Startup and communication

MCP tools are defined, added and changed as the use requires. They are not limited to the existing tools.

MCP tools are called from inside the sandbox. A custom HTTP / socket connection, or injection of a connection URL / bearer, does not replace the tool call. The driver does not start the server, and the Loop's decisions are not moved to the host. The execution form of a single standalone Python process is not fixed; it is revised into a form that uses MCP tools. The sandbox command returns the tool name, arguments and request id on standard output. When the conductor runs the MCP tool and returns its structured result on standard input, the same command continues. This standard input/output is the handoff between the sandbox command and the conductor, and only MCP tools access host resources. The driver is not split off into a separate process.

## Mutual exclusion, retry and lifecycle

The current filesystem lease is replaced with LoopState lease operations on the MCP side. A lease is tied to a driver session and a fencing token. When a lease expires because of a disconnection, that is not treated as the producer having ended. A successor driver uses a new token to reconcile the snapshot with the existing operation receipts, and reuses the same operation_id. An update that carries an expired token is refused. The idempotency of executor start and continue is reused, and a communication timeout does not start a new producer.

The MCP server owns the provider process and the durable receipts, and the end of the driver does not take the provider down with it. Child cleanup when the server itself exits follows the existing transport contract. If the end cannot be confirmed on reconnection, recovery-unknown is returned, and neither success nor stop is guessed. An explicit cancel distinguishes the stop request from the confirmation that the writer has ended.

## Implementation units and order

1. Enumerate the places that access state, artifacts, worktrees and processes directly, from runtime/development_loop and its dependencies, and define the using-side ports and the snapshot/commit contract. Keep the pure kernel decisions.
2. Add resource operation adapters and contract tests to the host MCP. Share the existing authorization and executor, and verify that a request outside the permitted scope does not reach a side effect.
3. Define the execution form that calls MCP tools, and connect all CLI operations and the driver's resource operations to that path. Delete the path that exposes open_execution_host to the runtime, and the calls to it. Keep the executor implementation for the independent MCP server.
4. Remove the custom bridge and the injection of connection information, and connect observation and resumption through MCP tools. Delete the across-the-board outside-the-sandbox designation in instructions.md, and if generated files are affected, synchronize them from the generator.

Completion of each unit is judged by the following contract tests. Production use is not switched over partway through the order, and the producer/consumer of the current contract are kept in sync. No embedded fallback for compatibility is created.

## Verification plan

- lint/static: check the changed schemas, prompts, generators and docs. Check for dependencies from the runtime on the embedded host and on provider startup.
- Regular tests: apply the same contract tests to the fake and to the production resource adapters. Confirm revision conflict, transaction applied in full / not applied at all, expired token, handle of a different repository, path traversal, symlink, operation refusal, and absence of side effects. Using a local test MCP, confirm the receipts for disconnection, reconnection, duplicate request and server restart.
- Acceptance E2E: complete open/drive/status/instruction/resume/export/cancel while the sandbox's direct access to host directories and provider startup stay refused. Even with concurrent drives the producer starts only once, and a refused request does not change the state or the worktree. Because it uses an external provider and a real host, separate explicit approval is required.

## Specific points not yet verified

The transaction scope of the existing ledger, the handoff between MCP tools and the decision processing inside the sandbox, and the behavior of the existing server under multiple connections and its cleanup on exit are to be confirmed before implementation. These are not a reason to change the boundary above. Where something is not supported, stop explicitly and provide the corresponding host adapter.

## Operation boundary settled by the implementation

The host effect handles the fixed document record of a stored Execution and the delivery commit / land. The baseline, arbitrary verification commands and the delivery postflight stay in the caller sandbox. No general-purpose command executor is placed on the host. Git child processes on the host are bound to cwd admission and a lifetime cell. The injection boundary of regular tests and the demonstration on a real host sandbox are distinguished.
