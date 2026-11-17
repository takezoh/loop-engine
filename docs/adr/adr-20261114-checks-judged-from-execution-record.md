---
id: adr-20261114-checks-judged-from-execution-record
kind: adr
title: Agents run checks inside the sandbox, and the engine judges pass or fail from the execution record
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-loop-engine-host-boundary}
source_paths: []
summary: A person places check commands in the project's configuration, and the producer runs them inside the sandbox. The engine judges pass or fail from the execution record, not from the agent's report text, and does not call the reviewer if the checks do not pass.
updated: '2026-11-14'
---

# Agents run checks inside the sandbox, and the engine judges pass or fail from the execution record

## Terms

A **check** is a confirmation whose pass or fail is decided by the command's exit status alone, such as test, lint, type check or build. A **review** is the judgment a reviewer agent makes by reading the contract and the diff.

## Context

In the records of 2026-11-08, the first implementation candidate was submitted without the regular tests having been run, and sending it back added 1 round trip. There is also a case where independent review missed a defect that appears in a real run. Acceptance currently leans on the reviewer's judgment, and deterministic checks are not a condition of acceptance.

On the other hand, if the engine runs the check commands itself, code rewritten by the producer runs with the host's privileges. The [proposed engine boundary decision](adr-20261114-loop-engine-host-boundary.md) does not permit this.

## Proposed decision

1. **A person places the check commands in the project's configuration file.** The unit contract only selects them by name.
2. **The producer runs them, inside the agent's sandbox.** The engine does not run them.
3. **The engine judges.** It uses the agent's execution record, not the agent's report text. It confirms that the configured commands succeeded and that no other command and no edit comes after them.
4. **If the checks do not pass, the engine sends the work back without calling the reviewer.**
5. **Checks are run through a fixed program in the installed copy.** From the configuration in the base revision, the engine builds the command string that should be run and includes it in the instructions to the producer. The fixed program runs the check command and prints the exit status on the last line. The engine confirms that the command in the record matches the expected string exactly, and confirms the exit status on the last line of the output. The success or failure and the exit code that the agent reports are not used for the judgment.
6. **When the execution record cannot be obtained, the result is "unverified" and is not approved.** Output that cannot be read is not passed.
7. **Stop reasons are recorded distinctly.** There are 7: complete, waiting for an answer, out of rounds, the same finding repeated, execution result unknown, cancelled, and agent configuration unsafe. Nothing other than complete is turned into complete.

## Relation to existing decisions

[Integration into independent acceptance of outcomes](../../../docs/adr/adr-20261010-outcome-evidence-boundaries.md) abolished the fixed verification stage and left the sufficiency of evidence to independent acceptance. This ADR keeps independent acceptance and places 1 deterministic condition before it. Of that ADR's Decision, this ADR changes the part "without making the running of observations a required intermediate step" (translated from Japanese), for units whose project has configured checks. It does not change that ADR's other decisions.

The user approved this ADR as a whole on 2026-11-14 ("2 approve"). The implementation does not yet follow this ADR.

## Required changes

In the current contract for starting agents, a turn's result is only the body text and the communication status, and has no record of the commands that were run. The contract has to be widened so that each protocol's driver returns an execution record.

## Alternatives considered

- **The engine runs the checks on the host:** The judgment is the most certain, but arbitrary code runs on the host.
- **Trust the agent's report text:** This cannot prevent a submission whose tests were not run.
- **Start a separate turn dedicated to checks:** This prevents the producer from skipping them, but adds round trips. It can be added if it becomes necessary.

## Consequences

- A submission whose tests were not run is stopped mechanically before the reviewer is called.
- Room to cheat the checks (such as rewriting the tests themselves) remains. Because it appears in the diff, it is something the reviewer looks at.
- Inside the sandbox, tests can fail for environmental reasons. A treatment is needed that compares with the original tree and fails only what got worse.

## Verification

- A test that confirms that the reviewer is not started for a candidate whose checks do not pass.
- A test that confirms that a check run before the last edit, and an edit after the checks, do not pass.
- A test that confirms that a turn without an execution record becomes "unknown".

In the PoC, these held with a fake agent. With real agents, the commands and numeric exit codes could be extracted from the output of `codex exec`. The output of `claude -p` had no number, and the result was read from the success flag and the body text. The results of the check with real agents through the existing abstraction (2026-11-14, 3 turns per agent) are as follows.

| protocol | Execution record | Current driver |
|---|---|---|
| codex-app-server | Has the command, success or failure, and a numeric exit code. However, 1 command that failed in the sandbox did not appear in the record | Discards it |
| pi-rpc | Has the command, success or failure, and a numeric exit code | Discards it |
| acp-v1 (claude) | Has no exit code. A failing command (`false`) also arrived as completed | Discards it |

Item 5 was revised on 2026-11-14 in response to this result (the user's approval: "accept"). The success or failure that the agent reports cannot be read for claude over ACP and can have gaps for codex, so it is not used for the judgment. The premises of the revised method were confirmed by the following facts.

- For all 3 protocols, the command string and the output body can be extracted from the record. Even for claude over ACP, the output of `echo ssh_rc=$?` arrived together with the command string.
- The installed copy (`~/.local/share/agent-module/plugins/dev-skills`) is read-only for all 3 agents under the common sandbox policy of agent-environments, and the producer cannot rewrite the fixed program.

An end-to-end confirmation of having the 3 agents run the fixed program has not been done yet. A check missing from the record becomes "unverified" under item 6.
