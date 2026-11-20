# Loop Engine technical reference

The details behind [ARCHITECTURE.md](../ARCHITECTURE.md): contract limits, checks, exit codes, record files and the engine's internals. The whole and its boundaries are in ARCHITECTURE.md; the reasons for each decision and the alternatives considered are in [adr/](adr/).

| If you | Read |
|---|---|
| write a contract or define checks for a project | [Contract](#contract), [Checks](#checks) |
| give a session the unit tools, or mount them in a server of your own | [MCP entry](#mcp-entry) |
| handle a unit's result | [Stop reasons and exit codes](#stop-reasons-and-exit-codes), [Records](#records), [Sending work back and answers](#sending-work-back-and-answers), [Accepting a reviewer's reply](#accepting-a-reviewers-reply) |
| change the engine | [Modules](#modules), [Agent sessions](#agent-sessions), [Prompts](#prompts), [End of a turn and the worktree](#end-of-a-turn-and-the-worktree), [Environment entries](#environment-entries), [Approval requests](#approval-requests), [Base revision](#base-revision) |

## Contract

The structure of a contract (fact kinds, execution rules, references) is in [Unit declaration](../ARCHITECTURE.md#unit-declaration). `validate` in `facts.py` checks a contract on admission and uses `check_structure` for the structure of facts and executions. `check_structure` is a public pure function that can be asked about a part of a contract alone. So that the side assembling a contract uses the engine's names without copying them, `facts.py` also publishes the keys of `unit` (`UNIT_KEYS`, `UNIT_OPTIONAL`), the items a run adds (`FACT_RUN_KEYS`, `EXECUTION_RUN_KEYS`), the values of a check's `expect` (`CHECK_EXPECT`) and the character sets of ids, names and commits (`ID_RE`, `CHECK_NAME_RE`, `COMMIT_RE`). The check adds these limits and character sets.

| Subject | Limit or character set |
|---|---|
| `unit_id`, the `id` of a fact, an execution and an acceptance criterion, `agent` (each character set below applies to the whole value, and a trailing newline is not accepted) | `^[a-z0-9][a-z0-9-]{0,62}$` |
| `model`, `effort` | `null` or `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,95}$` |
| Check name | `^[a-z0-9][a-z0-9_-]{0,31}$` |
| `goal`, the `text` of an acceptance criterion, `instructions` | A non-empty string without NUL, of at most 200,000 bytes |
| `criteria`, `scope`, `facts`, `executions`, checks | At most 32 each. `facts` and `executions` have at least one |
| `unit.base` | A 40-digit commit id |
| `unit.excluded_skills` | Exactly `plugin` and `names`. `plugin` and each name match `^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$`; `names` has 1 to 32 distinct names |
| `repository`, `scope` paths | `repository` is a normalized absolute path in the form of the machine the engine runs on (`path_style`). A `scope` path is a relative path separated by `/` with no `.`, `..` or empty component. On Windows each component must be a name a file can have: it contains none of `\`, `:`, `*?"<>|` or control characters, does not end with `.` or a space, and is not `CON`, `PRN`, `AUX`, `NUL`, `COM1` to `COM9` or `LPT1` to `LPT9`, in any letter case and with or without an extension. The same holds for `test_paths` and `ignore` in `.loop-engine/checks.json` |

The keys of a contract must match exactly, with none missing and none extra (a fact has only the items of its `by`). A different contract under the same `unit_id` is not accepted.

## Checks

A project defines its checks in `.loop-engine/checks.json`. The engine reads only the file at the base revision.

```json
{"checks": {"unit": {"argv": ["python3", "-m", "unittest"], "test_paths": ["tests"]}}, "ignore": ["__pycache__"]}
```

List the caches a check writes (such as `__pycache__`) in `ignore`. Without that, the tree digest differs before and after the check, and the check is judged as "not run on the present tree".

Every time it starts, the engine copies its own `check_runner.py` to `check/<sha256 of the content>/check_runner.py` in the published root. It writes under a temporary name in the same directory and then gives the file its final name in a way that does not replace an existing file (`place_new`), so neither an engine started at the same time nor a half-written file replaces an existing file. When that path holds a file (a link included) whose content sha256 does not match the directory name, the engine does not start. After starting it checks the content again every time before handing out a command; on a mismatch it stops the unit with `unknown_result` and starts no agent.

The command handed to an agent is `<the engine's Python> -I <published root>/check/<sha256>/check_runner.py <published root>/units/<unit_id>/checks.json <check name> [--base]`, with paths quoted by the platform's `quote`. The Python is the interpreter that runs the engine (`sys.executable`). The engine assumes that the agent's sandbox can read the published root and the engine's Python, cannot write the published root and cannot read the records root. It does not verify this.

The agent runs the command the engine gave. On the last line of its output, the check program prints the check name, the tree it ran on (candidate or base revision), the digest of the candidate tree and the exit code. For a check on the base revision it also prints the digest of the copied base revision tree (`base=`). The engine judges pass or fail from this line of the execution record alone. The engine and the check program compute the tree digest with the same function, and it does not include environment entries.

## Stop reasons and exit codes

| Stop reason | Exit code | Continued by |
|---|---|---|
| `done` | 0 | |
| `waiting_for_answer` | 10 | `answer` |
| `stagnation` | 12 | |
| `unknown_result` | 13 | `retry` |
| `cancelled` | 14 | |
| `unsafe_agent_config` | 15 | |
| `rejected` | 16 | |
| `invalid_result` | 17 | `retry` |
| `stuck` | 18 | |

The CLI returns 2 when a request is not accepted and 3 when the engine cannot be started. Failing to take the lock of the records root (permissions, a directory that cannot be made, a filesystem without locks) also returns 3 and names the cause on stderr. An engine that the entry started and that stopped with a failure before responding also returns 3, without waiting for the timeout, and shows its exit status and the tail of the engine's error output. The error output of an engine started detached stays in `engine.stderr` in the records root.

What each stop reason means for the caller is in [How a unit stops](../PRODUCT.md#how-a-unit-stops).

## MCP entry

`python -I src/loop_engine/__main__.py mcp` is a stdio MCP server for the session connected at the directory it is started in. It serves the unit tools of `unit_tools.py`.

| Tool | Arguments | What it does |
|---|---|---|
| `unit_start` | `contract` | Hands the contract to the engine and returns the unit's status. The unit runs on |
| `unit_status` | `unit_id` | Reads the unit's current status |
| `unit_wait` | `unit_id`, `timeout_ms` (0 to 30000), optional `after_revision` | Waits until the unit stops or its revision passes `after_revision`. Timing out does not cancel the unit |
| `unit_answer` | `unit_id`, `question_id`, `answer` | Answers the question a unit is waiting on |
| `unit_retry` | `unit_id` | Runs again the turn whose result was unknown or could not be checked |
| `unit_cancel` | `unit_id` | Cancels the unit. A unit that already ended keeps how it ended |
| `unit_result` | `unit_id`, `name` (`stop.json` or `patch.diff`) | Reads a stopped unit's result, cut to 200,000 bytes with `truncated` set when it was longer |

- Every tool has a closed input schema, and a call outside it is refused before anything is sent. No tool takes a program, an argument list, an environment, a path or the state home.
- A start is accepted only when the contract's `repository` is an existing directory inside the connection's directory, written as its resolved absolute path.
- A unit that another connection started is answered as "no such unit" by every tool, and a start under its id is refused with "this unit_id cannot be used".
- The entry does not open (exit code 2, the reason on stderr) for an agent the engine started, which carries the transport's execution role (`LOOP_ENGINE_EXECUTION_ROLE`) in its environment, or when the records root lies inside the connection's directory.
- A refusal by the entry or the engine is a tool error (`isError`), distinct from a unit's stop. Reading a unit starts no engine; a request does, as with the CLI.
- The server answers `initialize`, `ping`, `tools/list` and `tools/call` for the protocol revisions 2024-11-05, 2025-03-26 and 2025-06-18, one JSON-RPC message per line. It offers no resources or prompts and does not cancel a running call.

A caller with a server of its own mounts the same tools. `composition.unit_entry(environ, connection)` returns the tool group (`tools`, `instructions`, `invoke(name, arguments)`). A caller that composes contracts replaces `unit_start` with its own tool, calls `admit(repository, unit_id)` before composing and hands the contract to `start(contract)`.

## Records

The engine makes one directory per unit in the records root (`records/` under `LOOP_ENGINE_HOME`). Agents do not read this place. What makes it unreadable is the environment's sandbox settings.

| File | Content |
|---|---|
| `contract.json` / `admission.json` | The accepted contract, and the base revision and check definitions fixed on admission |
| `state.json` | The decision state (facts, reviews, the finding counts of refutations `refuted`, the requester's answers `answers`, the files reviewers read `examined`), and per execution the session token, the number of answers delivered and the number of refused requests. When the engine stops, it continues from here |
| `events.jsonl` | The record of the start, each turn's start (new session or continued session) and end, sessions that could not be continued, requests, closed sessions, and the stop |
| `status.jsonl` | The unit's status, one line per change; the last complete line is the current status |
| `result/stops/<n>/patch.diff` / `result/stops/<n>/stop.json` | The diff against the base revision, and the stop reason, which facts hold, the findings, the refusal count per execution, and the environment entries that are in the worktree and not in the diff. Each stop gets the next number `<n>`; `stop.json` is written last, and a caller reads the stop with the highest number that has it |
| `result/turns/NN-<execution>.json` | Each turn's reply, execution record, session token and number of refusals |

A record another process reads is never replaced, because on Windows an open reader makes a replacement fail: the status is appended to, each stop's results get a directory of their own, and the engine's threads read `state.json` under the lock it is saved under.

The records root also holds requests and responses (`inbox/`, `outbox/`), `engine.lock` and the transport's state (`agents/`).

A request the engine refuses is answered with its reason and changes nothing. An unexpected error while handling a request is answered with `engine error: <type>: <message>`; the engine writes the traceback to its error output, keeps serving, and drives every unit recorded as running. A request that continues a unit (`answer`, `retry`) records that the unit runs before it records the event and the new state, so a unit whose request failed part way is driven from the state it has. A `start` sent again for a unit that exists with the same contract completes what an earlier one left undone.

What agents read goes to the published root (`published/` under `LOOP_ENGINE_HOME`).

| File | Content |
|---|---|
| `check/<sha256>/check_runner.py` | The check program the engine published (see [Checks](#checks)) |
| `units/<unit_id>/checks.json` | The configuration the check program reads (the paths of the worktree and of the copy of the base revision, the check definitions, the names to ignore, the top-level names of the two commits) |
| `units/<unit_id>/base-<unguessable value>/` | The copy of the base revision. A check on the base revision runs on this copy with the candidate's tests added, and reviewers read the original version here. It is removed when the unit ends with a stop that cannot be continued |

Paths handed to a role (the check commands, the place of the base revision) point only into the published root. Neither root's path is a prefix of the other's, so a setting that allows or denies one by path prefix does not match the other.

Worktrees are made in a third place, one that the agent's sandbox can write (`LOOP_ENGINE_WORKTREES`). The copy of the base revision is not placed next to the worktree.

When a unit ends with a stop that cannot be continued (any other than `waiting_for_answer`, `unknown_result` and `invalid_result`), the engine removes the worktree and the copy of the base revision. The change stays in the stop's `patch.diff`.

Every agent can write the worktrees root, so nothing in it is trusted.

- The name of a worktree directory is the unit's id with an unguessable value added. A directory prepared in advance under the same name is not used.
- The engine checks that the worktree it made is at the very place the engine made, with no link on the way.
- The directories above a worktree hold only directories the engine makes (one per repository, and the units' worktrees). When anything else is there, the engine stops without starting an agent. It checks both before and after a turn.
- git is started with the git directory that was read when the worktree was made, given explicitly. When an agent rewrites the worktree's `.git`, the engine does not follow it.
- When an agent configuration is a link, the digest includes the content it points to. The amount read has a limit, and a unit whose configuration exceeds it does not proceed.
- A link in the worktree, a symbolic link or a junction (the link to a directory any Windows user can make), is a file of the tree known by its text (`check_runner.is_link`). The tree, the changed paths and the patch do not follow it, and the patch records each link by its text itself, because git would follow a junction into what it points at.

A gap remains in which another agent places a file between the check and the start of an agent. The means to make this place unwritable to agents belongs to the environment's sandbox settings, not to the engine.

## Sending work back and answers

- A refuted fact (review, checks, scope) goes back, with its findings, to the execution that made the subject. It goes back only when the number of findings is smaller than at the previous refutation of the same fact. What is counted is the blocking findings the engine computed for a review, the failed checks for checks, and the out-of-scope paths for the scope. The first refutation goes back. When the number is not smaller, the unit stops with `stagnation` and leaves the fact, the findings and a `detail` (the number this time and the previous time) in `stop` of `stop.json`. The previous number per fact is in `refuted` of `state.json`.
- When a writer that received findings ends its turn without changing the candidate, or a reporter returns the same report, the unit stops with `stagnation` (the detail is `candidate unchanged` or `report unchanged`). When an answer arrived after the fact of those findings was judged, the unit does not stop and the fact is judged again.
- Answers stay in order in `answers` of `state.json` (`id`, `question`, `answer`, `asked_by`). A session holds the number of answers it has received, and a follow-up message carries only the answers not yet received. The basis of a verdict fact has the digest of the answers under the key `@answers`, so that verdict stops holding when an answer is added. The bases of reports, checks and the scope do not include answers.
- The number of rounds is shown in `round` of the status and `rounds` of `stop.json`. It is not used as a stop condition.

## Accepting a reviewer's reply

- When the engine does not count a reviewer's reply, it joins every reason it found with `; ` and returns them as the feedback of the follow-up message. When the form of the reply cannot be read (no final object, `criteria` is not a list, and so on), it returns only that reason.
- The trace of having read the changed files counts, besides that turn's execution record, the files the same execution read in earlier turns on the same basis (`examined` of `state.json`). When the basis changed, earlier turns are not counted. Files with no trace of being read are all named in the reason (up to 32, and the number of the rest).
- An evidence command counts only when it is in that turn's execution record. A quote of a file counts in any turn as long as it is in the present file.

## Modules

| Module | Responsibility |
|---|---|
| `facts.py` | Checking a contract on admission, choosing the next execution, establishing and invalidating facts, judging checks and reviews. Works on values only |
| `prompts.py` | Assembling the prompt handed to an execution (a session's first message and its follow-up messages). Works on values only. It receives the names of the agent configuration a writer must not change as a value, from the list `worktree.py` observes |
| `unit.py` | Carries one unit from its saved state to its next stop |
| `service.py` | The engine process. Holds one thread per unit and takes requests |
| `store.py` | A unit's records, publishing what agents read (the check program, the unit's `checks.json`), request and response files, and the process lock. Taking the lock tells three results apart: taken, held by another process, and cannot be taken (`LockFailed`, which names the cause). A lock that cannot be taken is not treated as another engine running |
| `git.py` | Starting git. It always adds the arguments that disable hooks, filters and external diff |
| `worktree.py` | Observing the worktree (the agent configuration digest, the tree digest, the changed paths compared with git's record) |
| `check_runner.py` | The fixed check program an agent runs inside its sandbox. It owns how a tree is read (it fails when something cannot be read), the tree digest and the test for an environment entry; `worktree.py` uses the same code |
| `agents.py` | The port that runs an agent's turn. A session is a token whose content is not interpreted |
| `composition.py` | Connects the port to the provider transport and opens the unit tools for a connection. The only module that touches the transport |
| `client.py` / `cli.py` | The entry's client and the command line. Hand over requests and read records. Have no progress or judgment logic |
| `unit_tools.py` | The unit tools and the boundary of a connection: closed schemas, the check of a call's arguments, whose a unit is, and the port for the entry's client (`EngineClient`). Entry code under the same rule as the client |
| `mcp_stdio.py` | The stdio MCP server for a tool group, on the standard library alone. Decides nothing a tool decides |
| `testing.py` | The fake of the entry's client, for a caller's tests. It runs the client's contract tests |
| `platform_facts.py` | The platform facts of the machine the engine runs on (table below). The only module that chooses between POSIX and Windows by `os.name` |

A platform's facts are a `Platform` value, and `CURRENT` is that of the machine the engine runs on. `composition.py` hands it to `Engine`, and `Engine` hands the path form to `facts.validate` and `facts.checks_config` and the quoting to `Unit`. `git.py`, `store.py` and `client.py` use the values of `CURRENT`. `place_new` and `lock` are adapters for effects that differ by platform, and they receive the OS functions they call as arguments. The Windows adapters get the same contract test, against fake Windows functions, as the adapters of the machine the engine runs on.

| Fact | POSIX | Windows | Used by |
|---|---|---|---|
| `path_style` (the form of an absolute path) | `/a/b` | `C:\a\b` or `\\server\share\a` (separated only by `\`) | `facts.py` |
| `null_device` | `/dev/null` | `nul` | `git.py` (hooks, attributes, global configuration) |
| `home` (the value handed to git when the environment has no `HOME`) | `/` | not handed | `git.py` |
| `quote` (the check command line handed to an agent) | POSIX shell quoting | The form the Microsoft C runtime splits again (the same as `subprocess.list2cmdline`) | `unit.py` |
| `detached` (starting the engine detached from the caller) | A new session | A new process group, no console | `client.py` |
| `place_new` (creating a file without replacing an existing one) | hard link | rename | `store.py` (publishing the check program) |
| `lock` (the process lock) | `flock` | `msvcrt.locking` | `store.py` |
| `lock_held` (the errno when another process holds the lock; any other errno means the lock cannot be taken) | `EAGAIN`, `EWOULDBLOCK` | `EACCES` | `store.py` (`lock_contended`) |

## Agent sessions

Each execution has one agent session.

- An execution's first turn starts a new session and sends the role's protocol text with the full instructions in it. The port returns the token of the session the turn ran in, and the engine saves it per execution in `state.json`.
- When the same execution runs again (work sent back, an answer, a retry), the engine continues the session with that token and sends the role's follow-up protocol text (`protocol/<role>-follow-up.md`). A follow-up message has the findings and the reason for the retry, the reports and the requester's answers that the session has not yet received, for a reviewer the present list of changed files, and the form of the reply. The instructions, the goal and the acceptance criteria are not sent again.
- A protocol text hands a role the reports it is given as what another role found about this work, to be weighed as evidence, and states that the rules that role received for its own turn bind neither the reader nor the work. The protocol texts of non-writing roles (reporter, reviewer) state that the rules in the section that fixes the form of the reply are rules for that turn only and are not a subject of reports or findings. A role's reply reaches another role only as a non-writing role's report or as a reviewer's findings; a writer's report is handed to no role (`prompts.inputs`).
- Different executions do not share a session. A reviewer does not see the writer's session.
- When the port reports that "the session cannot be continued", the engine starts that turn once more, as a new session with the full instructions. A failure of that turn is treated like any other failed turn.
- The token is in `state.json`, so the same session continues after the engine restarts.
- When the unit ends with a stop that cannot be continued, the engine closes every session it kept. On a stop that can be continued it keeps them. Stopping the engine releases every session and then ends every agent process it started.
- The check of the agent configuration, the check of what is above the worktree, and cancellation apply to a continued turn alike. Only that turn's execution record is used to judge facts.

In the connection to the transport (`composition.py`), a token is the provider's session identifier (a Codex thread, an ACP or Pi session). The transport releases (unloads) the session at the end of every turn and loads it again by the identifier to continue. Keeping a session means that the engine has the token in `state.json`; closing it means discarding the token, and the transport discards its mapping for continuing that session. The conversation the provider saved is not deleted.

`composition.py` uses the transport's `ProviderSessions` in the engine's own process.

- An execution's first turn calls `start` with the provider, model, effort, cwd and prompt; later turns of the same execution call `continue` on the same session. After an engine restart, the saved identifier is bound with `recover_binding` before `continue`.
- Either way it waits with `wait_turn`, reads the result with `observe`, and waits for the session's release with `wait_release` before returning the turn. The `provider_thread_id` of the terminal view becomes the session token.
- When `continue` returns `session_lost` or `recovery_pending`, or binding the session fails, the port reports that "the session cannot be continued".
- `close` is used when the unit ends with a stop that cannot be continued, and for a session that could not be continued. Cancellation is `cancel`.

The meaning of each transport operation and the wire procedure of each driver are in [Provider transport](transport.md#provider-sessions).

## Prompts

`prompts.py` assembles the message of a turn for each execution from the protocol texts in `protocol/`. There are three role texts: writer, reviewer and reporter. An execution with `writes: true` uses the writer's, a non-writing execution that establishes a `verdict` fact uses the reviewer's, and any other uses the reporter's (`prompts.kind`). Every message of every role ends with `question.md`, the one text that says how to end a turn with a question instead of the reply; the role texts do not describe the question themselves.

- The first message puts the execution's `instructions` into the protocol text as they are, `unit.goal` under the `## Goal` section and `criteria` under the acceptance criteria section, and adds the reports given, the requester's answers so far and the findings of the previous round. A reviewer also gets the list of changed files and the place of the base revision; a writer gets the scope (`scope`), the check commands and the names of the agent configuration it must not change. The role text fixes the form of the reply (a report, or a judgment per criterion with findings), and any role may reply with a question instead.
- The content of a follow-up message is in [Agent sessions](#agent-sessions).
- The engine does not interpret `instructions`. The caller writes a role's behavior and judgment standards in `instructions`; the protocol text adds the goal, the acceptance criteria, the scope, the checks and the form of the reply.

## End of a turn and the worktree

The port returns a turn after the turn's agent session is released. Until the release, the agent's process and its sandbox may still be using the worktree. The engine reads the worktree (the agent configuration digest, the candidate's diff, the changed paths, the tree digest) only after the port returned the turn.

- When the release does not finish within the provider's time limit, or fails, the port returns a turn that did not complete (`released: false`, with the detail `agent-session-not-released: <reason>`). The engine judges nothing of the worktree for that turn and stops with `unknown_result`. It keeps the session token.
- The check before the next turn applies to everything in the worktree at that time. The engine neither removes nor changes anything inside the worktree. The only things left out of a judgment are the environment entries of the next section.
- After a turn, whether or not the role writes, the engine reads the whole worktree before judging. When even one directory or file cannot be read (cannot be listed, traversed or opened), the turn does not complete (`unknown_result`, with the detail `worktree-not-readable: <path>`). Leaving what cannot be read out of the tree would shrink the candidate, the changed paths and `patch.diff` (git, too, silently leaves out what it cannot read). When something inside the agent configuration cannot be read, the unit stops with `unsafe_agent_config`. The check program also gives the digest `none` for a tree it cannot read, and such a status line passes no judgment.

## Environment entries

An agent's host may place empty files or directories in the working directory. The engine knows neither their names nor the sandbox policy and tells them apart only by this form.

An environment entry is a name in the top-level directory of the worktree that is at the top level of neither the base revision nor the commit the worktree was made from, is a regular file (not a link), has size 0, and cannot be written by the judging process.

- Environment entries are not included in the candidate's tree and its digest, the changed paths, the scope judgment, the before-and-after comparison of a non-writing role's turn, the stop's `patch.diff`, the digest in a check's status line, or the agent configuration digest. They are also left out of git's index.
- The names left out are recorded. `environment_entries` in each turn's `turn_finished` has the names left out of that turn's judgment; `environment_entries` in the stop's `stop.json` has the names that were in the worktree at the stop and are not in `patch.diff`. Both are read from the worktree at the stop or after the turn, in the form `{"count": the total number, "names": [...]}`, with at most 32 names, each cut to 1000 characters.
- Everything else (a writable empty file, a non-empty file, a name in either commit, a link, a file below the top level) counts as a change. Once an entry has content or becomes writable, it counts from then on.
- The test is one function in `check_runner.py`, and the engine (`worktree.py`, and the exclusions `git.py` gives for the patch) and the check program use the same one. The engine reads the top-level names of the two commits from git when the unit starts and writes them to the `unit_started` record and to `committed` in `checks.json`.
- Neither git, the tree digest nor the agent configuration digest counts an empty directory. The names of the agent configuration (`.claude`, `.codex`, `.pi`, `.agents`, `.mcp.json`) are included in the digest whatever their kind: directory, file, link or anything else. Only environment entries are left out; any other empty file is included.
- Stopping the engine stops the running turns, releases the sessions and then ends the host processes. Ending a process with a signal is the last resort, for when the release did not finish within the time limit.

## Approval requests

When an agent asks for approval, permission or user input, the transport answers with an explicit refusal and the turn continues. No approval setting is sent to Codex. How each of the three drivers answers is in [Approval requests](transport.md#approval-requests) of the provider transport.

A refused request stays in the turn's execution record (`actions.refused`) with its kind (`command`, `file_change`, `permissions`, `input`) and a short description. The engine adds each turn's count per execution and shows it in `refused` of the status and of the stop's `stop.json`. A refusal does not stop the unit. A check that could not run because it was refused leaves no passing line, so its fact does not hold.

## Base revision

The base revision that judgments compare against is never read from a tree agents can write.

- Changed paths (the scope judgment, the list of changed files handed to reviewers, the share of changed files a reviewer read) come from comparing the worktree's files with the base revision's record (`git ls-tree`) in the git directory read when the worktree was made, by git blob id. The before-and-after comparison of a non-writing role's turn and `patch.diff` are git diffs against the base revision.
- A check on the base revision (`fails_on_base`) is run by the agent inside its sandbox, so what the engine can guarantee is limited. The engine guarantees that the check program reads the copy the engine placed; that a run whose status line's `base=` does not match the digest of the copy that the engine computed when the unit started does not pass (the finding is `not run on the base revision the engine placed`); that a run that cannot make the copy or cannot start the check does not pass; and that `digest=` matches the candidate tree at the start of the run. A status line shows no more than that the check program read that candidate tree at the start of the run and at that moment copied the content the engine placed. The temporary tree after the copy, what the tests do while running, and a process that writes to the same output after the check program's output are inside the agent's sandbox, and the engine cannot check them.
