---
id: issue-20260916-development-loop-fact-capability
kind: issue
title: Development Loop retries blindly without passing the reason for an unsatisfied fact to the next Capability
status: done
created: '2026-09-16'
issue_type: bug
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/kernel_v2.py
- runtime/development_loop/production_implementation_v2.py
- runtime/development_loop/production_diagnostic_v2.py
- runtime/development_loop/single_change_v2.py
summary: The Evaluator's rejection reason is not passed to the next Capability input, and the same unsatisfied fact is rerun blindly.
disposition_reason: Passed the rejection conditions of the production single-change frame evaluator as typed findings
  to the next input of the same Capability, and added a regression test that also handles the re-evaluation of snapshot
  evidence without recursion. Verified with 12 production implementation tests and all 684 Development Loop tests.
updated: '2026-09-16'
---

## Symptom / opportunity

Even when the Development Loop evaluates a Capability's outcome as `UNSATISFIED`, it does not pass the reason to the input of the same Capability that it reserves next. As a result, even though the fact the Goal needs is selected correctly, the producer is rerun with only the same information as the previous time and repeats the same kind of rejection.

## Evidence

- In the single-change child Goal of `issue-20260916-skills-suite-imports-removed-managed-child-entry-test`, the result was `premise.current: UNSATISFIED` even after `implementation.frame` returned `disposition: ready`, and the same `implementation.frame` was reserved again.
- The first time, the result was rejected because `constraint_checks[].constraint_ref` did not exactly match the string in the request. The second time, it was rejected because a range locator such as `:44-45` in `evidence_refs` did not satisfy the assessor's single-line locator contract.
- Only the request was passed to the next frame handoff, and it did not include which check condition had led to `UNSATISFIED`. Only when the conductor separately notified the producer of the rejection reason did the Goal advance to `premise.current: SATISFIED` on the third time.
- The frame assessor at `runtime/development_loop/production_implementation_v2.py:96` folds multiple conditions into a single `bool`.
- `runtime/development_loop/single_change_v2.py:178` converts the assessor's `bool` to `SATISFIED` / `UNSATISFIED` and does not save the rejection reason to Evidence or to the next input.
- `runtime/development_loop/single_change_v2.py:257` reselects the Capability that corresponds to the unsatisfied fact, but does not add the immediately preceding evaluator rejection to the input.

## Context needed to start

- `change-20260913-development-loop-first-class-integration` makes the Development Loop the single semantic controller and has a contract to reach the selected outcome through the Profile and direct Capability / Child Goal.
- Keep one operation per tick of `GoalKernel.tick()`, immutable Evidence, and the exclusive lifecycle of the native producer.
- Keep the boundary in which the producer's self-report is not the basis of success and an independent evaluator decides acceptance.
- Whether the same defect exists in debug, requirements, design, test-harness and single-change is unconfirmed. What had been reproduced when this issue was filed is the frame retry of production single-change.
