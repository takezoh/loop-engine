---
id: adr-20261117-provider-transport-is-the-engines-package
kind: adr
title: The provider transport is the engine's own package, a contract names the skills
  to exclude, and the engine installs the Claude adapter, which runs the user's Claude Code
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
  and the engine installs the latest Claude ACP adapter, without the Claude Code
  its SDK bundles, so that it runs the claude on PATH.
updated: '2026-11-21'
---

# The provider transport is the engine's own package, a contract names the skills to exclude, and the engine installs the Claude adapter, which runs the user's Claude Code

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
4. **The engine installs the Claude ACP adapter, and the adapter runs the user's Claude Code.** The engine's `install` places the latest release of `claude-agent-acp` in the transport's runtime directory, without its optional dependencies, the Claude Code binaries its SDK bundles, and applies no patch. The `claude` provider's launcher verifies that installation, starts it with the `node` on `PATH` and has it run the `claude` on `PATH`, the Claude Code the user runs. The agents' own commands, `claude` among them, come from the user's environment. The launcher checks nothing of a caller.
5. **The transport has one selection point for the platform of its own, `host_platform.py`,** for command search and the launcher.

## Relation to existing decisions

- Items 4 and 7 of [the engine boundary decision](adr-20261114-loop-engine-host-boundary.md) keep the existing abstraction for starting agents and make the engine an independent package that does not depend on its caller's code. The abstraction is unchanged; it is now the engine's own, so the engine's code depends on no other repository.
- Item 2 of that decision (the engine passes no sandbox or approval setting to an agent) is unchanged. Excluding skills was already the one thing the transport tells a provider about what to load; the list now comes from the contract instead of the catalog.
- [Completion by facts](adr-20261114-unit-completes-when-facts-hold.md) defines the contract. `unit.excluded_skills` is one more optional key of `unit`; it changes no fact and no rule for choosing an execution.
- [The ports decision](adr-20261117-platform-and-agent-differences-behind-ports.md) applies to the transport as before. It leaves open whether the engine may use the transport's inter-process lock; the lock stays with dev-skills, and the engine's install takes the engine's own process lock on `install.lock` in the runtime directory.
- [The names decision](adr-20261117-engine-names-its-own-home-and-check-file.md) gave the engine its own state home. The installed adapter lives apart from it, in the runtime directory (`LOOP_ENGINE_RUNTIME_DIR`).

## Alternatives considered

- **Make the transport a third repository.** Nothing would need to be split, but the engine would depend on a package that also holds another product's installer, and three repositories would have to move together. The user chose to move the provider sessions into the engine.
- **Leave the transport in dev-skills.** The engine would depend on a caller's package and the caller on the engine. Rejected.
- **Give the excluded skills to the engine in a file written at install time.** The contract would not change, but every caller's units on that machine would get the same exclusions. The user chose the contract.
- **Stop excluding skills and rely on the refusal of the unit entry.** An agent inside a unit cannot open the entry in any case, but it would still load and follow skills that tell it to. The user did not choose it.
- **Let each caller install the adapter and tell the engine where it is.** Every caller would carry an installer, and the engine would start what a caller names. Rejected.
- **Let the engine install the adapter, pinned and patched, with the Claude Code its SDK bundles.** This was item 4 from 2026-11-17 to 2026-11-21. The adapter ran its SDK's own Claude Code (2.1.219) instead of the user's (2.1.329), so units saw an older model list than the user's Claude Code offers. Rejected; the bundled Claude Code is no longer installed and the user's runs.
- **Take the adapter from the user's environment, as the agents' own commands.** This was item 4 on 2026-11-21. Every environment that runs `claude` units would have to install the adapter itself, and an environment that had not did not start `claude` at all. The user chose that the engine installs it.

## Consequences

- The engine's repository contains everything its code needs. A caller depends on the engine for both the engine and the transport.
- A unit's agents load every skill unless the contract says otherwise. A caller that composes contracts decides what its units' agents must not load.
- Starting `claude` needs the engine's `install` to have run once, and `node` and `claude` on `PATH`. Without them the launcher exits and the unit stops with `unknown_result`.
- The adapter's version is its latest release at the time `install` last ran. A newer adapter reaches units when `install` runs again. The engine applies no patch; a defect of the adapter is fixed by the adapter's own release.
- The launcher no longer checks a caller's installation. A caller that wants its own installation verified before its agents start has to do that itself.
- The execution role variable an agent inherits is the engine's, `LOOP_ENGINE_EXECUTION_ROLE`, and the name the transport gives itself to a provider is `loop_engine`. The `subsession-` error codes and the `subsession-python` command base keep their names.

## Verification

`tests/test_facts.py` confirms the admitted form of `unit.excluded_skills`. `tests/test_transport_agents.py` confirms that a contract's exclusion reaches the driver in the provider's form and that each exclusion has its own provider sessions. `tests/test_closed_paths.py` confirms that only `composition.py` names the transport. The tests under `tests/transport/` cover the catalog with and without an exclusion, the launcher and the drivers. `tests/transport/test_claude_adapter.py` covers the runtime directory, the install of the latest release without optional dependencies and its verification. No test starts a real adapter; the installed adapter is observed only by running it.

## User's decision

On 2026-11-17, shown three ways to place the transport, the user chose "A", described as "move the provider session part into the engine's repository and leave the plugin's runtime part in dev-skills" (translated from Japanese). Asked how the skills to exclude are handed over, the user chose "Hand them over in the contract"; asked who owns the Claude ACP adapter, "The engine owns its installation and verification"; asked for the package's name and place, "provider_transport", described as `loop-engine/src/provider_transport/`.

On 2026-11-21, shown that the engine installed, pinned and patched the adapter and that the adapter ran its SDK's own older Claude Code, the user said "use the CLI in the environment" and that the engine and dev-skills are to use the user's environment for the tools they use. Shown that this item 4 had to change for the adapter, the user answered "1. ADR を修正" ("1. Revise the ADR").

On 2026-11-21, shown that the adapter of the user's environment was not on `PATH`, so that `claude` could not start, the user said "claude-agent-acp は loop engine が入れる" ("Loop Engine installs claude-agent-acp"). Asked whether the engine pins the adapter or takes its latest release, the user answered "最新版を入れる" ("Install the latest release").
