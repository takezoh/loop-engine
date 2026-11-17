---
id: adr-20261116-work-goes-back-while-it-converges
kind: adr
title: Work is sent back only while it converges, and there is no limit on the number of rounds
status: accepted
created: '2026-11-16'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261114-checks-judged-from-execution-record}
- {type: references, target: adr-20261114-verdict-computed-from-checkable-findings}
source_paths: []
summary: Work is sent back only when the findings of a refuted fact are fewer than in the previous refutation of the same fact. If they do not become fewer, the unit stops with stagnation. The contract has no limit on the number of rounds.
---

# Work is sent back only while it converges, and there is no limit on the number of rounds

## Context

A unit stopped with `rounds_exhausted` when the number of rounds reached the contract's `limits.rounds`, and stopped with `stagnation` when the text of a refutation's findings matched the previous one.

On 2026-11-16, the records of 14 implementation units of syntoria-trace (6 completed, 8 failed) were examined.

- The limit on the number of rounds stopped units that were converging. The number of blocking findings was falling, 4→2→1 in relay-gate-corrected and 6→4→3→2 in messenger-gated-send-adapter, but the units stopped at the limit. relay-common-send-gate used 1 of its limit of 2 rounds on cleaning up generated files and stopped without a chance to fix 8 findings.
- Stagnation by matching text does not work when the reviewer writes the same defect in a different text. The reviewers in the records wrote the remaining defects in new text that began with "The previous finding is unresolved" (translated from Japanese).
- In the completed units, the blocking findings fell with each refutation of the same fact (2→1→0, 3→1→0, 1→0). The unit that did not progress went 2→2→3.

## Decision

1. The contract has no limit on the number of rounds. `unit.limits` and the stop reason `rounds_exhausted` are removed.
2. A refuted fact goes back to the execution that makes its subject only when the findings of that refutation are fewer than the findings of the previous refutation of the same fact. If they are not fewer, the unit stops with `stagnation`, and the findings and the current and previous counts are kept in the record. The first refutation has nothing to compare with, so it goes back. The number of findings is, for a review, the blocking findings the engine computed; for a check, the checks that failed; for scope, the number of out-of-scope paths.
3. A turn after which the candidate or the report is unchanged even though it received feedback is `stagnation`. However, when the requester answered after the fact of that feedback was judged, the unit is not stopped, because the answer alone may resolve it ([the answers decision](adr-20261116-answers-reach-every-execution.md)).
4. A refuted fact has at least one finding, and the findings become fewer with each refutation of the same fact, so every unit stops after a finite number of rounds. The number of rounds is kept in the record but is not used as a stop condition.

## Relation to existing decisions

- This ADR replaces "limits" in item 1 and "out of rounds" in item 7 of [the decision on completion by facts](adr-20261114-unit-completes-when-facts-hold.md), and "out of rounds" and "the same finding repeated" in item 7 of [the checks decision](adr-20261114-checks-judged-from-execution-record.md). "Stagnation" takes the meaning of items 2 and 3 of this ADR. The other items of both ADRs are unchanged.
- The rule of [the review decision](adr-20261114-verdict-computed-from-checkable-findings.md) that stops the unit when a result that cannot be confirmed occurs twice in a row is not a subject of this ADR and is unchanged.

## Alternatives considered

- **Raise the limit for each Profile.** The problem of stopping converging units by a count remains. Rejected.
- **Treat a decrease in unmet acceptance criteria as progress.** In the records, a unit that had several findings on one criterion (relay-gate-fresh-approval had one criterion and its findings went 2→1→0) would stop as not progressing. Rejected.
- **Recognize the same finding by matching the text of the findings (the former stagnation).** It does not match when the reviewer changes the text. Rejected.
- **Treat the appearance of a finding that was not in the previous round as progress.** A reviewer can keep finding new counterexamples, so there is no guarantee that the unit stops. Rejected.

## Consequences

- Of the units that the limit stopped in the records, those whose findings kept falling (relay-gate-corrected, messenger-gated-send-adapter, messenger-personal-implementation, messenger-runtime-guards, messenger-browser-read, relay-common-send-gate) continue. messenger-personal-api-analysis, whose findings did not fall, stops at the second refutation.
- The unit also stops when the reviewer finds the same number of new defects as the previous time. It is treated as not converging.
- The unit stops when the same number of checks fail twice. The protocol text asks the writer not to settle a check that its own changes cannot make pass with a report, and to stop with a question.
- The number of findings depends on how the reviewer writes (for example, splitting one defect into two). The reviewer is not shown the stop condition.

## Verification

- Item 2 was applied to the review sequences of 23 units in the records of 2026-11-16, and it was confirmed that every completed unit falls on the side that continues and that the units whose findings did not fall stop (the numbers in Context).
- `ConvergenceTest` in `tests/test_facts.py` and `tests/test_unit_flow.py` confirm that work goes back while the findings fall, that the unit stops when they are the same or more, and that the comparison with the previous time is made per fact.

## User's decision

On 2026-11-16, in response to item 6 of the improvements listed in the investigation of the Loop records (the granularity of units and the limit on the number of rounds), the user instructed "For 6., define a stop condition instead of a limit by number of attempts" (translated from Japanese). The comparison in item 2 (fewer findings than in the previous refutation of the same fact) was set after this instruction by applying it to the records. To the report that showed this comparison, the user answered on the same day "Approval of the stop condition of 6: I approve".
