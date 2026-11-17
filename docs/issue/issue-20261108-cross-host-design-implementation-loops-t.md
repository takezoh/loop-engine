---
id: issue-20261108-cross-host-design-implementation-loops-t
kind: issue
title: Cross-host design/implementation Loops take 45-60 min per unit and serialize
  on one checkout
status: open
created: '2026-11-08'
issue_type: plan
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/observe.py
- runtime/development_loop/production_dispatch_v2.py
- runtime/development_loop/instructions.md
- profiles/agent-placement-presets.yaml
summary: Design/implementation Loops via Codex took 45-60 min per unit, needed main-session
  interventions and could not run in parallel on one checkout.
---

## Symptom / opportunity

On 2026-11-08, one transport rework (1 design unit + 4 implementation units) was carried out with the design / implement Loops of dev-skills. Design took about 40 minutes and each implementation unit took 45 to 60 minutes, and after 2 hours 40 minutes only 2 of the 4 units were complete. The user pointed out that it was "taking too long" (translated from Japanese). Implementation units also cannot run in parallel on the same checkout.

## Evidence

- Each unit ran "implementation → independent review → sending work back → re-review" in series with a Codex producer (`gpt-6-luna` / xhigh for implement, `gpt-6.1-sol` / low for design / review; `profiles/agent-placement-presets.yaml`), and one turn took 15 to 30 minutes.
- The first implementation candidate was submitted without running the ordinary tests (the producer's self-report was "The ordinary tests were not run" (translated from Japanese)), and the subsession tests had 28 failures + 7 errors. The tests were run only after the independent review sent the work back, which cost one extra round trip.
- The design producer (`gpt-6.1-sol` / low) returned `BLOCKED` (pursuit-unresolved: insufficient inspection) without investigating the repository. It did not proceed until the main session recorded a pursuit-resolution that named the files to investigate.
- The independent review missed a close / continue defect caused by the delay of Codex `thread/closed` on a real machine (about 60 seconds). The main session found it. The implementation Loop has no entry for adding the main session's evidence or findings, so the Loop was rebuilt with `export-pursuit` → `cancel` → a successor `open`.
- When two implementation Loops were opened in parallel on the same checkout, the one opened later failed with `world-observation-unconfirmed`. `capture_binding` in `runtime/development_loop/observe.py` compares the digest of the whole worktree before and after, so the other producer's writes cause a mismatch. Creating a separate git worktree avoids this, but the procedure document (`runtime/development_loop/instructions.md`) does not mention it.
- Inside the sandbox the producer uses, the cell tests of `tests/runtime/subsession` fail with `Read-only file system: /run/user/1000/...` (the same on an unchanged HEAD). The producer and the reviewer could not tell this from a regression caused by the candidate, and work was sent back once.

## Investigation result: serialization or the pipeline within a unit (2026-11-08)

Conclusion: the main cause is the pipeline within a unit, and serialization between units is secondary.

Measurement sources (observed facts): the mtime of `invocation.json` / `receipt.json` of each execution in the composition, the transport call times in the main session transcript, and the tool call times, `Wall time` and token usage in the Codex rollout (`~/.codex/sessions/2026/11/08/rollout-*-<thread>.jsonl`). The classification of tool calls (Loop procedure / tests / code) is an approximation by regular expressions over the input strings.

1. Time breakdown (about 160 minutes from the design dispatch at 11:27 to the candidate of the receipt unit at 14:07):
   - The working time of the Codex producers (dispatch → receipt) was about 128 minutes (80%). Of this, the implementer took about 114 minutes, the reviewer about 9 minutes in total over 4 runs, and design (2 designer turns, 1 reviewer turn) about 5 minutes.
   - The gaps on the main session side (the producers' post-processing, and the main session's verification, waiting and dispatch) were about 32 minutes (20%).
2. Contents of the implementer turns (Codex rollout):
   - 87 to 91% of the turn time was model-side time, and the tool execution time (the total of tests, Loop procedure and code operations) was 2 to 5 minutes per turn.
   - One turn had 100 to 230 model round trips, and one round trip averaged 9 to 15 seconds (`gpt-6-luna` / xhigh). Input tokens were 12 to 35 million per turn (cache rate 98 to 99%).
   - Distribution of model time: reading and writing code 56 to 71%, Loop procedure (reading the manifest, `agent-step`, checkpoint and so on) 12 to 29%, test-related 9 to 15%. For the reviewer, about 74% of the model time was Loop procedure.
3. Round trips from sending work back: the second implementer turn of chunk-host (about 38 minutes) occurred because the first candidate was submitted without running tests, and the second turn of chunk-lifecycle (about 16 minutes) occurred because the main session found a defect the reviewer had missed. About 54 minutes in total (about 34% of the whole) was rework.
4. Effect of serialization: the chunk dependencies of the approved design were a straight line, host → lifecycle → receipt → loop, and the 3 transport chunks changed the same `mcp_application.py` and also depended on each other in content. Only chunk-loop on the Loop side could have been parallelized, and the time that could have been saved is limited to that one unit (about 15 to 20% of the whole, estimated). In addition, on the same checkout the Loop's world observation refuses parallel execution (see the Evidence section).

Inference: what most affects the elapsed time is, in this order, (a) the many short model round trips that the xhigh implementer makes in one turn, (b) the rework caused by test execution and the confirmation of real-machine behavior being left until after candidate submission and after review, and (c) the Loop procedure round trips within a producer turn. All of these are measurements of one session, and differences in the model's response time by time of day are not considered.

## Status of improvements (2026-11-08)

Breakdown of the Loop procedure (chunk-host implementer, classified by the tool inputs in the rollout): reading the pursuit and candidate state, 30 times, about 6.2 minutes of model time; pursuit/candidate operations through `loop agent`, 16 times, about 3.3 minutes; `agent-step`, 7 times, about 2.1 minutes; reading the prompt and inputs, 7 times, about 1.4 minutes; reading the manifest, 2 times, about 0.9 minutes. The execution cost of `agent-step` itself is small.

Changes made:

- Two tests in `tests/runtime/subsession/test_cell_membership.py` used the default cell path (under `XDG_RUNTIME_DIR`) and failed in the read-only sandbox. They were fixed to use the test's temporary directory as `XDG_RUNTIME_DIR`, which removed the false failures that the producer and the reviewer mistook for regressions. The multiprocessing Manager failure in `test_installed_package_authority` is specific to the environment, caused by the restriction on creating sockets, and was not changed.
- An instruction was added to the bootstrap of `agents/definitions/{repository,readonly}-agent.yaml` to combine independent reads, searches and confirmations into one tool call and to request only the needed range and amount of output. The effect on the number of round trips has not been measured.
- The implementer instructions in `profiles/implementation/profile.yaml` said "submit the overall checks to the delegate, and run broad checks only when they are needed to resolve uncertainty" (translated from Japanese). The implementation Profile had no Execution that runs the overall checks, which left room for the producer to submit without tests. The instruction was replaced with one to use the unit, contract and counterexample tests that cover the unit's responsibility while making the change, to run them on the assembled candidate before submission, and to resolve the failures the candidate caused. Checks beyond the unit's responsibility stay with their owner.

Changes not made, and the reasons:

- A machine gate that makes attaching test results mandatory at candidate submission is not adopted. Following the accepted ADR `adr-20261010-outcome-evidence-boundaries` (which abolishes the fixed verify stage and judges the sufficiency of evidence by independent acceptance), the sufficiency of test evidence is judged by the reviewer of implementation (the evaluation of regression evidence and the `test-discipline` Knowledge). A hook at the end of a turn (the Stop hook of Codex 0.167 / Claude Code) fires after `agent-step` has recorded the candidate, so making the turn continue would make the recorded candidate and the worktree disagree. The option of running the tests inside `agent-step` would relocate the fixed gate that the ADR abolished.
- The option of moving the handling of `agent-step` and `CONTINUE` to the runtime side was deferred, because the `agent-step` round trips are small, as the breakdown above shows.
- An entry for adding the main session's evidence and findings to a running Loop is a design change that involves changing the Loop's instruction contract and the review input, and has not been started.
- A comparative measurement of the effort of the placement presets (xhigh / high / medium) has not been done.

## Context needed to start

- The target is the Development Loop execution path of dev-skills (`runtime/development_loop/`), the placement presets, and the operation of `skills/implement` / `skills/design`.
- Work at the time of discovery: the implementation of shared host / turn capacity / start receipt classification for the cross-host transport (design Loop `ba6b9545...`, implementation Loops `7cbd88bf...`, `769d706e...`, `6d705bb5...`, `e3dadf28...`).
- Constraints: keep the Loop's independent review and the acceptance decision by the main session. For the elapsed time, there are measurements of only one session.
