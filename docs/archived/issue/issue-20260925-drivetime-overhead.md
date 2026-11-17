---
id: issue-20260925-drivetime-overhead
kind: issue
title: The design Goal run is slow, and repo edits during the run invalidate evidence (drive friction, validity of the tree_sha comparison)
status: rejected
created: '2026-09-25'
issue_type: bug
tags: []
owners:
- conductor
relations: []
subject_paths:
- runtime/development_loop/agent_loop_v2.py
- runtime/development_loop/observe.py
- runtime/development_loop/attempts.py
- runtime/policies/planning/policy.py
- runtime/development_loop/native_producer_v2.py
summary: 'The design Goal run is slow: 14 serial capabilities, 1 subagent per step, and reruns from attempt
  mix-ups, domain rejects and missing anchors. A conductor repo edit mid-run changes tree_sha, voids all prior evidence
  and halts the run. Which surface tree_sha comparison should protect is undecided.'
disposition_reason: The design Goal run (serial driving of capabilities), capture_binding in observe.py, and attempts.py
  do not exist in the implementation.
updated: '2026-11-14'
---

## Symptom / opportunity

The Goal run of the design Profile is slow because it drives a fixed 14 capabilities in series with 1 step = 1 subagent. When subagent reruns and the conductor's observe round trips pile up, one run takes a long time. Furthermore, when the conductor edits a repo file during the run, the tree_sha of the working tree changes, the preceding evidence is invalidated all at once, and the run cannot continue.

Expected outcome: (a) the round-trips between steps are reduced or overlaid, and only the failed step can be rerun, (b) editing a non-governed file (such as a docs operations document) during the run does not invalidate the whole run. At the least, it is declared which surface the tree_sha comparison protects as an invariant.

## Evidence

- **Serial length and round trips**: the capability order in `profiles/design/profile.yaml` is sources → context → constraints → problem → decision → draft-1 → draft-2 → integrate-2 → review → revise → minimality → reassess → package → record (14 nodes). Each producing step requires, in series, the ~5 CLI calls `goal dispatch` → `task` subagent → `goal observe-start` → `goal observe-terminal` → `goal advance` + 1 LLM turn. `goal agent-step` always fails with `agent-loop-producer-not-started` before observe-start (native_producer=null), so the run cannot be interrupted at the level of a step.
- **High-cost output**: the design-publication of design.package is ~106KB of prose + spine + 3 member markdown. Generating this in 1 turn of a very long continue session (`ses_f2bc14…`) is heavy, and it looked as if no response was coming back (it was cancelled twice in this run).
- **Rerun cost**: draft-1 was rerun because the first subagent did not write output.json, minimality took 2 attempts because of a domain reject, and for package new attempts kept being reserved because of missing anchors (`daba73cb` → `a06c42ca` → `dacf007c`).
- **Attempt mix-up (package)**: when the designer session `ses_f2bc14…` was made to run attempt `daba73cb…` (design.package, continue step6), the session reran the old revise attempt `09c174aa…` instead of package and overwrote output.json. It fell back to the wrong attempt under the mistaken belief that "the hash of the path the manifest presents does not exist" (translated from Japanese). The cause is presumed to be the accumulated context of the continue session.
- **Domain reject (minimality)**: attempt `e0179023…` was `rejected / native-output-invalid:production-composition-domain-transition-invalid`. The cause was that `validate_result` in `runtime/policies/planning/policy.py` requires pair/group references to match `classifications[].signal_id` byte for byte, while the LLM wrote descriptive names. It was resolved by the rerun attempt `56cd3254…`. The match of reference IDs is not machine-verified in advance.
- **UNSATISFIED retry loop of package**: the evaluator of design.package requires `<!-- anchor: <id> -->` for every spine identity of design.md, through `PlanningBundle.validate` in `runtime/policies/planning/policy.py`. The producer's design-publication passes the JSON schema (`artifacts/design-publication/schema.json`) but lacks 30 anchors, and a new package attempt is reserved while the evidence stays ACCEPTED with `result: UNSATISFIED`. The schema does not express the anchor requirement, so retries continue in domain validation even when the output is schema-valid. When the anchors are filled in, `validate` gives 0 errors.
- **Repo edits during the run invalidate evidence**: `files_digest` (= tree_sha) of `runtime/development_loop/observe.py::capture_binding` is a hash of the whole working tree (tracked + untracked; `docs/index/` is ignored and has no effect). Because the conductor edited repo files during the run (filing an issue, adding a change.md member, adding consultation-evidence), the tree_sha of the preceding evidence (`ac445407…`) and the current tree (`39d68622…`) no longer matched, `sources.current` became STALE, then all evidence became BLOCKED, and the run could not continue. For a single attempt, `readonly-world-changed` of `runtime/development_loop/attempts.py::_verify_result` fires.
- **Filed earlier**: issue-20260924-debug-goal-observation-gate-verification (the exact-match requirement of the observation gate makes a goal unrecoverable).

## Context needed to start

- Found in: `docs/changes/change-20260924-integrator-host-observation` (the design Goal run itself).
- **Open question: is the comparison of tree_sha (the whole working tree) necessary?** What it protects is "a readonly attempt makes no undeclared repo change" and "evidence points to the same world". However, at the current granularity, the whole run is invalidated even by edits to docs operations documents (`docs/issue/**`, the metadata of `docs/changes/**`) that are not the governed surface of design (`runtime/development_loop/**` and the like). `docs/.docs-config.yaml` already declares `evidence.meta_paths` / `generated_paths` for dev-evidence, which can be a precedent for limiting the comparison surface to governed paths. What must be decided is whether to (a) limit what is compared to the declared source/scope surface of the Profile, (b) provide an explicit re-baseline that lets a run continue after a meta path edit, or (c) stay strict and make "no repo edits during a run" an operating discipline. The conclusion changes with which invariant evidence currency protects.
- Target modules: `agent_loop_v2.py` (agent-step gating), `planning/policy.py` (reference/anchor validation), `native_producer_v2.py` (producer binding), `observe.py` (capture_binding / tree_sha), `attempts.py` (readonly-world-changed).

## 2026-09-25 execution status

- The target run (design Goal `6b38f619…`) could not continue because `sources.current` was STALE (tree_sha mismatch), and was ended with `goal cancel`. The design content (adoption of candidate A', plan, review/minimality) had already been accepted in the runtime, so it was recovered by manually materializing the change `change-20260924-integrator-host-observation` from the accepted package output (design-plan/design.md + spine.yaml + 3 members, `docs lint` green).
- This issue itself remains unresolved (none of the slowness of design, the invalidation of evidence by edits during a run, and the package anchor loop has been fixed on the runtime side).
- Whether the tree_sha comparison is necessary is undetermined, as stated in "Open question" above.


{% transition from="open" to="rejected" date="2026-11-14" %}
Checked against the implementation in the inventory of 2026-11-14 (user instruction "Compare against the implementation and archive what is not implemented" (translated from Japanese)). The design Goal run (serial driving of capabilities), capture_binding in observe.py, and attempts.py do not exist in the implementation.
{% /transition %}
