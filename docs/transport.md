# Provider transport

`src/provider_transport/` starts, observes, continues and stops an agent's session over each provider's wire protocol. It owns the provider catalog, the protocol drivers, the lifecycle of provider sessions, the admission of child processes and the Claude ACP adapter. The engine is its caller and reaches it only through `loop_engine/composition.py`; which operation the engine uses in which order is in [Agent sessions](architecture.md#agent-sessions). The transport imports nothing of the engine.

## Layers

| Layer | Modules | Owns |
|---|---|---|
| Provider lifecycle | `provider_port.py`, `provider_lifecycle.py`, `provider_sessions.py` | The provider contract a caller uses, and the lifecycle of turns, sessions and hosts common to every transport |
| Protocol driver | `codex_driver.py`, `acp_driver.py`, `pi_driver.py` | Per wire protocol: starting a host, binding or loading a session, running a turn, progress, cancel, unload |
| Wire | `codex_app_server.py`, `jsonl_rpc.py`, `acp_router.py`, `acp_client.py`, `pi_rpc.py`, `turn_actions.py`, `turn_result.py`, `provider_request.py` | Processes, wire records, sessions and threads, provider bindings, bounded turn output, action requests to a provider session |
| Process admission | `cwd_authority.py`, `admitted_process.py`, `cell_membership.py`, `directory_authority.py`, `linux_process.py`, `posix_filesystem.py`, `windows_process.py`, `windows_filesystem.py`, `windows_security.py`, `command_search.py` | Reserving a child process and proving its ownership; the ports for the process cell and the working-directory authority, and their adapters per platform |
| Catalog | `providers.json`, `provider_catalog.py`, `provider_drivers.py` | Each provider's command and bindings, the form in which it excludes skills, and the choice of driver per transport |
| Claude adapter | `claude_adapter.py`, `launchers/claude_agent_acp.py`, `patches/` | Installing and verifying the Claude ACP adapter, and starting it |
| Platform | `host_platform.py` | The one selection point of command search, the adapter's runtime layout and the launcher |
| Doubles | `testing/` | The fakes of the process cell and of the working-directory authority, for the tests of whoever uses those ports. They run the ports' contract tests |

## Provider sessions

`ProviderSessions` is the one provider implementation. The engine makes one per agent and per set of excluded skills, and it reaches the wire protocol only through a `ProtocolDriver`.

- `start(request, admission)` runs the turn on its own background worker. Once the driver has bound the session and reported `started`, it returns a startup receipt of `running` (the session id and the durable identity) without waiting for the whole model turn. When no host exists the driver starts one with the spawn admission and hands it over at once. A turn that fails before its session is bound closes the host that turn started and then returns the failure. A host that could not be closed is not reused and is closed again at shutdown; a turn whose end cannot be proven is quarantined.
- `observe` (an `action`) returns `running`, the bounded streamed text and the time the provider last reported an event while the turn runs; at the end it returns `completed`, `post_start_failed` or `cleanup_failed` with the turn result. The outer outcome follows the provider's status: when the provider completed, the outcome is `completed` even if the communication evidence is incomplete, and the incompleteness stays inside the turn result.
- `wait_turn(execution_id, timeout)` waits for the turn to end and returns true when there is no turn or it has ended.
- `wait_release(execution_id)` waits, up to the driver's unload limit, for the session of an ended turn to be unloaded and returns `unload_status` (`completed`, `pending` or `failed`) and, when it is not `completed`, the reason (`cleanup`). While a turn runs it is `pending` (`turn-running`). When the shared host has died, the session counts as released.
- `recover_binding(execution_id, session_id)` binds a durable identity that the caller saved to an execution of this process. A caller that restarted its process uses it before `continue`.
- `continue` sends the next prompt to the same session. While the previous turn is running it returns `recovery_pending` and never starts the same session twice.
- `cancel` interrupts the turn and does not end the process. `interrupt` also stops only the running turn and returns a receipt. `close` stops the turn and closes the session.
- The pure functions of `provider_lifecycle.py` decide the views, the evidence and the unload state returned, whether a continue is refused, and whether a spawn admission is needed.
- The prompt of a model turn has no deadline on the turn's length. Only startup and RPC control operations have bounded timeouts.

A session is loaded per turn and unloaded at the end of that turn, in the background. A failed unload does not count as a completed one, and a session is not loaded again while its unload is undetermined. A continuation waits up to the driver's unload limit (Codex 65 seconds, ACP 5 seconds, Pi 10 seconds) and then loads the session by the saved durable identity (a Codex thread id, an ACP or Pi session id). When the unload is still undetermined past the limit, it returns `recovery_pending`. An explicit close does not wait for the unload and returns `completed` with the present unload state.

`shutdown` stops the running turns, unloads every loaded session, waits up to the driver's unload limit and then closes the hosts. Closing a host closes its stdin, sends terminate and kills after a grace of 2 seconds. Releasing a session differs per driver: Codex confirms `thread/closed`; ACP receives the response to `session/close` (when the agent did not declare close, nothing is sent and the session counts as released); Pi ends the process.

Codex makes threads inside one app-server host. An ACP provider makes several sessions inside one host per argv and per process authority binding. Both run turns of different sessions on the same host in parallel; a continuation reuses a live host, and after a host has died a new host loads the durable identity again. A dead host is collected and its spawn reservation returned. Pi has one process per session: each turn starts a process with `--session-id` and `--session-dir`, and the unload ends it. Every transport binds the exact identity and generation and normalizes the turn's output into the bounded snapshot of `turn_result.py`. No transcript is stored.

The wire procedure per driver:

- Codex (`codex_driver.py`): binds with `thread/start` or `thread/resume` on the shared app-server host and waits for the terminal notification of `turn/start`. Cancel is `turn/interrupt`; unload is `thread/unsubscribe` and the confirmation of `thread/closed`. A turn that ends right after binding is returned as a terminal view within the startup grace of 1 second.
- ACP (`acp_driver.py`): `acp_router.run_acp_request` runs `session/new` or `session/load`, sets model and effort with `session/set_config_option` and reads them back, and runs `session/prompt`. The values are sent as the contract gives them, so a model must be one of the values the provider offers; when the provider reads back another value, the turn fails with `acp-config-readback-mismatch:<key>:requested=…:bound=…`, naming the value it bound instead. Cancel is `session/cancel`; unload is `session/close`.
- Pi (`pi_driver.py`): starts `pi --mode rpc` in the admitted directory and binds after `get_state` shows the session id, model and thinking level that were asked for. A turn runs from `prompt` to `agent_settled`; it is incomplete when the final assistant message's `stopReason` is `error` or `aborted`, on an abort, or when the transport is cut. Cancel is `abort`; unload is the end of the process.

The transport passes an agent neither a sandbox nor an approval setting. Codex's `thread/start` and `thread/resume` send only the directory, the model and the effort, and the agent runs under the approvals and sandbox of the user's Codex settings. ACP and Pi start with the user's settings as well.

### Approval requests

When an agent asks for approval, permission or user input, the driver answers with an explicit refusal and the turn continues. A pure function per driver decides the answer; the driver only wires it.

| Driver | Request | Answer |
|---|---|---|
| Codex (`codex_app_server.approval_reply`) | `item/commandExecution/requestApproval`, `item/fileChange/requestApproval` | `{"decision": "decline"}` |
| | `item/permissions/requestApproval` | `{"permissions": {}}` (nothing is granted for the turn) |
| | `mcpServer/elicitation/request` | `{"action": "decline", "content": null, "_meta": null}` |
| | anything else | the error for an unsupported method (`-32601`) |
| ACP (`acp_client.permission_refusal`) | `session/request_permission` | The offered option whose kind is `reject_once`; `cancelled` when there is none |
| Pi (`pi_driver.dialog_refusal`) | `extension_ui_request` of `select`, `confirm`, `input`, `editor` | `{"type": "extension_ui_response", "id": <the request's id>, "cancelled": true}`, sent by `PiRpcClient.cancel_ui_request` |

The Codex answer is returned by the server request handler that the adapter sets on the shared app-server's JSON-RPC client. Pi's other UI requests (`notify`, `setStatus` and so on) get no answer. A refused request stays in that turn's `ActionLog` with its kind (`command`, `file_change`, `permissions`, `input`) and a short description from the title or reason the provider gave, and appears in `actions.refused` of the terminal view. An ACP request is recorded with the prompt running in that session.

### ACP router and permissions

`acp_router.py` receives a provider, a directory, a prompt, a generation, a model, an effort and a session id and runs one turn. It receives no executable, argv or shell fragment and starts only the command fixed in `providers.json`. It does not switch providers to continue.

The ACP client refuses every permission request and never selects an option that allows. An agent's `fs/*` and `terminal/*` requests are refused with an error as well (the client's capabilities declare that it does not offer them). These refusals stay in the terminal view as bounded `tool_failures` without paths, tool input or credentials. `completed` for a turn does not mean that the work succeeded in its meaning.

## Process admission

A provider's child process starts only by consuming a `SpawnAdmission` (`cwd_authority.py`). An admission ties together a reservation of the `ChildLedger`, a lease on the working directory and the process cell. `admitted_process.launch_admitted` is where every wire protocol client starts its process; there is no start without an admission. The admission and the ledger touch two ports only and do not branch on the platform. Effects that differ by platform stay in the ports' adapters, each chosen at one selection point.

| Port | Operations | Selection point | Adapters | Fake |
|---|---|---|---|---|
| Process cell `CellMembership` (`cell_membership.py`) | `spawn(command, cwd=, options=)`, `confirm(pid)`, `process_identity(pid)`, `process_exists(pid)`, `release_certain(pid, identity)`, `terminate_roots(((pid, identity), …))`, `terminate_root(pid)`, `terminate_all()`, `resolve_unconfirmed_empty(timeout)`, `wait_empty(timeout)`, `close()` | `create_cell_membership()`: `WindowsJobCell` on Windows, otherwise `ProcSubreaperCell` | Linux `linux_process.ProcSubreaperCell` (makes its own process the child subreaper, counts members by ancestry in `/proc`, gives each root its own session and process group, and ends processes through a pidfd); Windows `windows_process.WindowsJobCell` (a no-breakaway Job Object per root) | `provider_transport/testing/cell_fake.py` |
| Working directory `CwdAuthority` (`directory_authority.py`) | `admit(requested)` returns a `CwdLease`. A lease has `admitted_path()` (the absolute path, with its identity confirmed again, handed to a shared host), `consume_for_spawn()` (once: the path a child process starts in) and `close()`. The authority has `close()` | `create_cwd_authority(cwd, allow_tmp=)`: `WindowsDirectoryAuthority` on Windows, otherwise `PosixDirectoryAuthority` | POSIX `posix_filesystem.PosixDirectoryAuthority` (walks from a directory descriptor with `O_NOFOLLOW` and starts the child at `/proc/self/fd/<fd>`); Windows `windows_filesystem.WindowsDirectoryAuthority` (a chain of handles that refuse delete sharing, and never follows a reparse point) | `provider_transport/testing/directory_fake.py` |

`CwdLease` is the same type on both platforms. It wraps the directory the adapter holds (a descriptor on POSIX, a handle chain on Windows) and the state of having been consumed or closed, and it holds the directory until the child's start is confirmed. The authority holds only the origin and, when allowed, the temporary root (`/tmp` on POSIX, `tempfile.gettempdir()` on Windows), not their ancestors. A working directory must be under the authority's root; moving to a parent and leaving through a link are refused with `CwdDenied`.

`terminate_roots` and `terminate_root` end only roots the cell itself started, and only when the identity recorded at the start still matches. A root with no identity or a different one gets no signal.

- **Linux.** Right after the start, while the root is still an uncollected child, the cell opens the root's pidfd and records it with the identity; every signal goes to a pidfd. A root is started with `setsid`, so it leads its own session and process group, and a session leader cannot move to another group: the group whose id is the pidfd's process is the group the cell made. The group is ended with the kernel's `PIDFD_SIGNAL_PROCESS_GROUP` (Linux 6.9 and later) on the pidfd. On a kernel without that flag the group is not signalled by number, because the root is collected outside the cell's lock by the `poll()` or `wait()` of the caller that holds its `Popen`, and the number could be reused between the check and `killpg`. Instead the cell finds the group's members in `/proc`, opens a pidfd for each before reading its group, confirms through the root's pidfd that the root is still uncollected and then signals the member's pidfd. The root is ended last. When the root has already been collected, `terminate_root` signals none of that group's members, and `terminate_all` ends them as members of the cell. `terminate_all` opens a pidfd for each member found in `/proc`, reads the identity again and signals the pidfd only when it matches.
- **Collecting on Linux.** The cell collects only the ended processes it adopted, those that descended from a root. A child that other code of the same process started (the engine's `git`, for example) is collected by that code: if the cell collected it, that code's `wait` would fail with ECHILD and `subprocess` would report an exit status of 0. A child the process forked is in the process's own session or leads its own; it is never in another session it does not lead. The cell tells adopted children apart by this rule, collects a child that leads its own session only when it saw it in a root's lineage, and does not count an ended child it does not collect as a member.
- **Windows.** The cell resolves the program to start from the child environment's `PATH` and `PATHEXT` with the pure functions of `command_search.py` and does not start a program that resolves to a batch shim (`.cmd`, `.bat`): a shim runs only through cmd.exe, which reads every argument again as a command line. The cell holds the root's process handle and the handle of its Job Object and ends only the Job Object of a root whose recorded identity matches.

`SpawnAdmission` is also a context manager. When it ends without being consumed, `release_unused` returns the reservation and the lease and refuses any later consumption.

The pure functions of `cwd_authority.py` (`owned_roots`, `process_gone`, `after_gone`, `failed_start_release_certain`) decide, from what the cell observed, which roots the ledger owns, what a failed start leaves behind and whether a release is certain. A started child is recorded as owned only after it is confirmed as a root of the cell, and a child whose end cannot be proven is quarantined. When a start fails, the root that was started is ended with `terminate_root`; a start that made no root returns its reservation as certain only when `resolve_unconfirmed_empty` shows that no unconfirmed root remains. `terminate_owned` ends, with `terminate_roots`, only the roots the ledger recorded.

## Catalog

`providers.json` declares each provider's transport, command, bindings and the form in which it excludes skills, and `provider_catalog.py` reads it as a `ProviderSpec`. `provider_drivers.driver_for` chooses the driver for the transport (`codex-app-server`, `acp-v1`, `pi-rpc`). The name and version of the Claude ACP adapter are in `claude_adapter.py`, matching `package.json` and `package-lock.json` at the repository root; the catalog does not hold them.

A provider runs in the user's environment: settings, sandbox, plugins and skills load as they do when the user runs the agent. A caller may name skills of one plugin that the provider must not load. The value is the contract's `unit.excluded_skills` (`{"plugin": ..., "names": [...]}`); `load_provider(provider, exclude=...)` turns it into the provider's own form, and without it nothing is excluded. The catalog names no caller's skills.

| `skill_exclusion` | What the provider receives |
|---|---|
| `codex-skills-config` | `-c skills.config=[{name="<plugin>:<skill>",enabled=false},…]` added to the start command |
| `claude-disallowed-skills` | `disallowedTools` with `Skill(<plugin>:<skill>)` in the `_meta` of `session/new` and `session/load`, so that calling the skill is refused. Claude does not apply `skillOverrides` to a plugin's skills, so the names stay in the model's skill list |
| `pi-exclude-skills` | `--exclude-skills <skill>,…`, read by the skill exclusion extension that the user's Pi launcher loads |

A provider that declares no form is not loaded (`malformed-provider-skill-exclusion`). Skills are excluded where a provider's host starts, so the engine keeps separate provider sessions, and separate hosts, per set of excluded skills. A model or effort that does not fit the provider's bindings is an error before anything starts. Earlier schemas, aliases, old state and falling back to another transport are not interpreted.

## Claude ACP adapter

The `claude` provider speaks ACP through the npm package `@agentclientprotocol/claude-agent-acp`, pinned to one version with one pinned patch. `claude_adapter.py` owns its installation and verification.

- `python -I src/loop_engine/__main__.py install` copies `package.json` and `package-lock.json` into the transport's runtime directory, runs `npm ci --omit=dev --omit=optional` there, applies the patch and verifies the result. The optional dependencies are the Claude Code binaries that the adapter's SDK bundles, so none is installed. It needs Node.js 22 or later and npm. One install runs at a time, under the engine's process lock on `install.lock` in that directory. `check` verifies without installing.
- The runtime directory is `LOOP_ENGINE_RUNTIME_DIR`, by default `~/.local/share/loop-engine` (`%LOCALAPPDATA%\loop-engine` on Windows). It holds only what the transport installs.
- `verify` passes only when the installed package has the pinned name and version and the patched file has the pinned digest. The patch replaces fixed anchors in the package's `dist/acp-agent.js`; a package whose file is neither the pinned original nor the pinned result is refused.
- The catalog's `command_base: "subsession-python"` makes the transport's own interpreter start `launchers/claude_agent_acp.py`. The launcher verifies the adapter and then starts the package's entry script (`dist/index.js`) with the `node` on `PATH`, setting `CLAUDE_CODE_EXECUTABLE` to the `claude` on `PATH`, so the adapter runs the Claude Code the user runs and offers that version's models. It uses neither the `.bin` link nor a Windows `.cmd` shim, for node or for claude, and it exits with 127 when the adapter is not installed or no runnable node or claude is found.

## Verification

- `tests/test_port_contracts.py` lists, per port, every adapter and every fake with the contract test class that runs it, and fails when a class anywhere in the source or the tests implements a port without being listed.
- The tests under `tests/transport/` use fakes of the wire protocols and of the platform APIs. `test_approval_refusal.py` (Codex, Pi) and `test_acp_permission_boundary.py` (ACP, over real stdio) confirm the refusal of approval requests per driver.
- Driver contract: `ProtocolDriverContract` in `test_protocol_drivers.py` applies to the three drivers and to `protocol_driver_fake.py`.
- Process cell contract: `CellMembershipContract` in `test_cell_membership.py` applies to the Linux cell (real processes of this machine), the Windows cell (the fake Win32 API of `windows_api_fake.py`) and the fake in `provider_transport/testing/`. It includes not ending a root whose recorded identity does not match; the Linux tests reproduce a reused pid by placing an ended root's record under the pid of another group leader and confirm that that process stays.
- Working-directory contract: `CwdAuthorityContract` in `test_cwd_authority.py` applies to the POSIX authority (real directories), the Windows authority (fake Win32 API) and the fake in `provider_transport/testing/`.
- The real Windows API is confirmed by running `windows_native_checks.py` on Windows.
- No test starts a real agent or runs npm. An install into a real runtime directory, and a turn with a real agent, are observed only by running them.
