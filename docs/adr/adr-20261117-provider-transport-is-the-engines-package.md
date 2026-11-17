---
id: adr-20261117-provider-transport-is-the-engines-package
kind: adr
title: The provider transport is the engine's own package, a contract names the skills
  to exclude, and the engine installs the Claude adapter
status: accepted
created: '2026-11-17'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261117-platform-and-agent-differences-behind-ports}
- {type: references, target: adr-20261117-engine-names-its-own-home-and-check-file}
source_paths: []
summary: provider_transport lives in the engine's repository beside loop_engine. Its
  catalog names no caller's skills, a contract's unit.excluded_skills carries them,
  and the engine installs and verifies the Claude ACP adapter in its own runtime directory.
updated: '2026-11-17'
---

# The provider transport is the engine's own package, a contract names the skills to exclude, and the engine installs the Claude adapter

## Context

The engine starts agents through a provider transport: the catalog of providers, a driver per wire protocol, the lifecycle of provider sessions and the admission of child processes. That code was part of `cross_host_transport`, a package of the dev-skills repository, and the engine looked for it at `packages/cross-host-transport` beside its own directory. The engine is being separated from that repository, in which dev-skills is one of its callers, and an engine that depends on a caller's package cannot stand apart from it.

About 28 of that package's 37 modules were what the engine needs. The rest served the dev-skills plugin: its Python runtime, its execution bundle, credentials, placement presets and a lock. The two parts were joined in three places.

- The provider catalog listed the dev-skills skills that an agent inside a unit must not load, under the plugin's name.
- The `claude` provider starts through an npm package with one patch. The dev-skills installer placed it in the plugin's runtime directory, and its launcher verified the plugin's execution bundle together with it.
- Command search took the test for Windows from the plugin's runtime module.

## Decision

1. **The provider transport is a package of the engine's repository.** It is `provider_transport`, beside `loop_engine` under `src/`. It stays a separate package: only `loop_engine/composition.py` touches it, and it imports nothing of the engine. The engine no longer looks for a transport outside its own directory.
2. **The catalog names no caller's skills.** A contract may carry `unit.excluded_skills`, the name of one plugin and the names of its skills that the unit's agents must not load. The engine checks only its form and hands it to the transport, which turns it into each provider's own form. Without it nothing is excluded.
3. **Provider sessions are kept apart per set of excluded skills.** Skills are excluded where a provider's host starts, so two units with different exclusions never share a host.
4. **The engine installs and verifies the Claude ACP adapter.** `install` places the pinned package, with its pinned patch, in the transport's runtime directory (`LOOP_ENGINE_RUNTIME_DIR`, by default `~/.local/share/loop-engine`), and `check` verifies it. The adapter's launcher verifies the adapter before every start, and nothing of a caller.
5. **The transport has one selection point for the platform of its own, `host_platform.py`,** for command search, the adapter's runtime layout and the launcher.

## Relation to existing decisions

- Items 4 and 7 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) keep the existing abstraction for starting agents and make the engine an independent package that does not depend on its caller's code. The abstraction is unchanged; it is now the engine's own, so the engine's code depends on no other repository.
- Item 2 of that decision (the engine passes no sandbox or approval setting to an agent) is unchanged. Excluding skills was already the one thing the transport tells a provider about what to load; the list now comes from the contract instead of the catalog.
- [Completion by facts](adr-20261114-unit-completes-when-facts-hold.md) defines the contract. `unit.excluded_skills` is one more optional key of `unit`; it changes no fact and no rule for choosing an execution.
- [The ports decision](adr-20261117-platform-and-agent-differences-behind-ports.md) applies to the transport as before. It leaves open whether the engine may use the transport's inter-process lock; the lock stays with dev-skills, and `install` uses the engine's own process lock, so the question does not arise here.
- [The names decision](adr-20261117-engine-names-its-own-home-and-check-file.md) gave the engine its own state home. The transport's runtime directory is a second directory, for installed software, apart from records.

## Alternatives considered

- **Make the transport a third repository.** Nothing would need to be split, but the engine would depend on a package that also holds another product's installer, and three repositories would have to move together. The user chose to move the provider sessions into the engine.
- **Leave the transport in dev-skills.** The engine would depend on a caller's package and the caller on the engine. Rejected.
- **Give the excluded skills to the engine in a file written at install time.** The contract would not change, but every caller's units on that machine would get the same exclusions. The user chose the contract.
- **Stop excluding skills and rely on the refusal of the unit entry.** An agent inside a unit cannot open the entry in any case, but it would still load and follow skills that tell it to. The user did not choose it.
- **Let each caller install the adapter and tell the engine where it is.** The engine alone could not start `claude`, and every caller would carry an installer. The user chose the engine.

## Consequences

- The engine's repository contains everything its code needs. A caller depends on the engine for both the engine and the transport.
- A unit's agents load every skill unless the contract says otherwise. A caller that composes contracts decides what its units' agents must not load.
- Starting `claude` needs `install` to have run, with Node.js 22 or later and npm on the machine. Until then the launcher exits and the unit stops with `unknown_result`.
- The launcher no longer checks a caller's installation. A caller that wants its own installation verified before its agents start has to do that itself.
- The execution role variable an agent inherits is the engine's, `LOOP_ENGINE_EXECUTION_ROLE`, and the name the transport gives itself to a provider is `loop_engine`. The `subsession-` error codes and the `subsession-python` command base keep their names.

## Verification

`tests/test_facts.py` confirms the admitted form of `unit.excluded_skills`. `tests/test_transport_agents.py` confirms that a contract's exclusion reaches the driver in the provider's form and that each exclusion has its own provider sessions. `tests/test_closed_paths.py` confirms that only `composition.py` names the transport. The tests under `tests/transport/` cover the catalog with and without an exclusion, the adapter's patch and verification, the launcher and the drivers. No test runs npm; an install into a real runtime directory is observed only by running it.

## User's decision

On 2026-11-17, shown three ways to place the transport, the user chose "A", described as "move the provider session part into the engine's repository and leave the plugin's runtime part in dev-skills" (translated from Japanese). Asked how the skills to exclude are handed over, the user chose "Hand them over in the contract"; asked who owns the Claude ACP adapter, "The engine owns its installation and verification"; asked for the package's name and place, "provider_transport", described as `loop-engine/src/provider_transport/`.
