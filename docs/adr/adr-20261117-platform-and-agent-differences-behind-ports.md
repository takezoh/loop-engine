---
id: adr-20261117-platform-and-agent-differences-behind-ports
kind: adr
title: Effects that differ by platform or agent sit behind ports, and decisions receive
  those facts as values
status: accepted
created: '2026-11-17'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261115-prompts-are-pure-and-unconfirmed-results-stop}
source_paths: []
summary: The engine runs on Linux and native Windows. Only effects that differ by
  platform or agent get a port with an adapter and a fake under one contract test;
  the choice is made in one place, and decision code receives the facts as values.
  git, saving state and time get no port.
updated: '2026-11-17'
---

# Effects that differ by platform or agent sit behind ports, and decisions receive those facts as values

## Context

Two of the engine's design principles, "ports and fakes" and "platform facts", had no decision in the engine's own documents. They were decided on 2026-11-15 for the dev-skills repository, in which the engine was developed, by that repository's decision `adr-20261115-platform-agent-differences-behind-ports`. That decision covered the plugin and the engine together. The engine is being separated from that repository, so the part that binds the engine is recorded here.

When the rule was decided, the engine assumed POSIX: its process lock chose between `fcntl` and `msvcrt` by `os.name` inside the store and treated every `OSError` as "another process holds the lock". The names of the agent configuration directories were written separately in `worktree.py` and in the prompt text.

## Decision

1. **The engine runs on Linux and native Windows.** macOS is not a target.
2. **An effect that differs by platform or agent sits behind a port named for what its caller needs.** A port has one adapter per platform or agent and one fake, and the same registered contract test applies to all of them.
3. **The choice of platform or agent is made at one place per port, never inside decision code.** Decision code does not branch on `os.name`, `sys.platform` or an agent's name, and does not look for a method the port does not declare (`getattr` and `hasattr` included).
4. **Decisions are pure functions that receive platform and agent facts as values.**
5. **An effect that does not differ by platform or agent gets no port under this rule.** git, saving state and time have none. An agent's start command stays a value of the transport's catalog.

In the engine this takes the following form.

- `platform_facts.py` is the only module that chooses by `os.name`. Facts that differ by platform (the form of an absolute path, the null device, the quoting of the check command line, starting a process detached from the caller, the errno that means another process holds the lock) are values of a `Platform`. Effects that differ by platform (creating a file without replacing an existing one, the process lock) are adapters that receive the OS functions they call.
- `agents.py` is the port for agent turns. `composition.py` connects it to the transport, and the tests use a scripted fake. Differences between agents stay in the transport's catalog and protocol drivers.
- `facts.py`, `prompts.py` and the judgment of lock contention receive what they need as values.

Not decided here: whether the engine may use the transport's inter-process lock instead of its own lock adapter. The decision for the dev-skills repository left that to the user, and it is still open.

## Relation to existing decisions

- Item 4 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) (agents are started through the existing abstraction, and the engine depends only on a port in its own words) is unchanged. This decision adds that the port's adapter and its fake share one contract test.
- Item 8 of [completion by facts](adr-20261114-unit-completes-when-facts-hold.md) and item 1 of [pure prompts and unconfirmed results](adr-20261115-prompts-are-pure-and-unconfirmed-results-stop.md) make `facts.py` and `prompts.py` work on values only. This decision adds that platform and agent facts reach them as values too.

## Alternatives considered

These are the alternatives the decision for the dev-skills repository rejected; they apply to the engine in the same way.

- **Keep the branches and add tests.** The branches stay inside decision code, and a difference between the method sets of two platforms is caught neither by a type nor by a contract test. Tests grow with the branches, and mocks hide the difference. Rejected.
- **Put one abstraction layer over every OS call.** Effects that do not differ by platform (git, saving state, time) would gain an indirection, and the layer would follow the shape of the OS API instead of what the caller needs. Rejected.
- **Duplicate modules per platform.** The decisions would exist twice, and a fix to one would not reach the other. Rejected.

## Consequences

- One body of decision code serves both platforms, and a fix reaches both.
- Most of a Windows adapter can be checked on Linux against fake Windows functions with the same contract test.
- Each port costs a fake and a contract test shared with its adapters.
- A contract test against a fake does not establish how a real Windows kernel behaves. That needs a run on a Windows machine.

## Verification

`tests/test_transport_agents.py` applies the agent port's contract to both the connection to the transport and the scripted fake. `tests/test_platform_facts.py` applies the contracts of the `place_new` and `lock` adapters to the adapters of the machine it runs on, to the Windows adapters over fake Windows functions and to the fakes, and checks that `platform_facts.py` is the only module that tells platforms apart. `tests/test_closed_paths.py` checks that `facts.py` and `prompts.py` import only computation modules.

## User's decision

On 2026-11-15, after an audit and a design proposal, the user stated the policy for the dev-skills repository: "Aim for platform neutral and agent neutral. Separate side effects from pure functions, and make ports and adapters only for the side effects that differ by platform or agent." (translated from Japanese). The user set the target platforms as "Linux and native Windows" and approved the rules with "Approve ADRs 1, 2, 3, 4, 5". On 2026-11-17, told that these two engine principles had no engine decision once the engine leaves that repository, the user instructed: "Copy the ADR to the engine side or create one".
