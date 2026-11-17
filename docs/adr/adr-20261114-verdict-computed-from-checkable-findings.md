---
id: adr-20261114-verdict-computed-from-checkable-findings
kind: adr
title: The engine computes the verdict of a review and counts only results that can be checked
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261114-checks-judged-from-execution-record}
source_paths: []
summary: The reviewer does not declare approval; it returns a judgment for each acceptance criterion and findings with quotes. The engine computes the verdict after checking the quotes and the execution record.
updated: '2026-11-14'
---

# The engine computes the verdict of a review and counts only results that can be checked

## Context

Even with a reviewer, a review can stop at a superficial confirmation. A study that classified the failures of multiple agents (MAST) reports that "a verifier is not a cure-all. Many stop at a superficial confirmation" (translated from Japanese).

The Loop records of 3 machines also showed the following.

- The output of an approval is only a judgment and a summary, with no record of what was examined.
- In the implementation placement, the reviewer was the same model as the writer.
- Of 77 cases of work sent back, 54 had only 1 finding. In a series where the same change was reviewed 5 times, each approval came with a minor finding that had not been there before.
- In a series that went 5 rounds with 3 viewpoints, in a round where 2 viewpoints approved with no findings, only the remaining 1 viewpoint raised a serious finding.
- There were records where the judgment and the findings disagreed (4 approvals with findings, 5 cases of work sent back with no findings).

What research has measured as effective is measures that tie the judgment to execution results and to evidence that can be checked. Instructions of the "examine thoroughly" kind have no consistent effect.

## Decision

1. The reviewer does not declare approval or sending back. What it returns is a judgment and evidence for each acceptance criterion, and findings. The acceptance criteria are fixed by the contract at the start.
2. Evidence is either a quote from a present file or a command run in that turn. A finding carries a quote of the line it is about and a severity (minor, major, blocker).
3. The engine checks all of the following. A result that does not satisfy them is counted neither as holding nor as refutation; the engine states the reason concretely and has it redone. If this happens 2 times in a row, the engine stops the unit.
   - Every acceptance criterion has a judgment.
   - A "met" has evidence, and a "not met" has a finding that names that criterion.
   - The quoted text exists in a file of the present candidate. The line number is a guide, and a match is not required.
   - The evidence command is contained in a command in that turn's execution record.
   - The execution record has a trace of the changed files having been examined.
4. The engine makes the fact hold only when every criterion is "met" and there is no finding at or above the severity the contract sets. Lighter findings are only recorded and do not block. A criterion that is not met blocks regardless of the severity of the finding.
5. The reviewer is given only the goal, the acceptance criteria, the list of changed files, and the location of the original versions. The writer's report and a diff prepared by the caller are not given. The engine records the tree that is the subject of the review.
6. As a reviewer, the engine accepts only an execution whose agent and model pair differs from that of the execution that made the subject. Whether the same pair is allowed is decided by contract data and checked at admission.
7. A form in which multiple review executions establish 1 fact is allowed. The engine collects the results of all of them and then sends the findings back together.
8. The check program prints the digest of the tree at the time it ran on the result line. The engine compares it with the tree after the turn. This is because an agent can write files through the shell, so "a check after the last change" cannot be judged from the order of the execution record.
9. For each check, it can be required that "it fails in the state where only the candidate's tests are placed on the base revision". The fixed program inside the agent's sandbox runs it, and the engine looks at the record.

## Relation to existing decisions

- This decision adds 8 and 9 to "judge by exact match of the check command and by the last line" of the [checks decision](adr-20261114-checks-judged-from-execution-record.md). The rule that work does not proceed to review if the checks do not pass is unchanged.
- How facts and executions are expressed follows the [decision on completion by facts](adr-20261114-unit-completes-when-facts-hold.md).

## Alternatives considered

- **The reviewer declares approval (current).** This cannot prevent records where the judgment and the findings disagree. Rejected.
- **Check command evidence by exact match.** With a real agent, claude ran `… -v 2>&1 | tail -20` and wrote `… -v` as the evidence. Exact match rejects a review whose content is correct, so being contained was made sufficient.
- **Require the line number of a quote to match.** With a real agent, a reviewer that read the file without line numbers was off by 4 lines. The check was made on the existence of the text instead.
- **Measure the reviewer by planting known defects.** The evidence for an effect on reviewers is weak. Not adopted.

## Consequences

- "Approval with findings" and "work sent back with no findings" do not occur.
- With multiple reviewers, the number of review turns grows.
- Limits remain. In research, even reviewers that examine the repository passed 39 to 54% of wrong patches, and 29.6% of patches passed the tests while their behavior differed from the correct answer. The engine keeps in the result which facts were established by mechanical checks and which by judgment. The main session judges overall acceptance.
- The matching of examination traces counts a file if its name appears in a command. A reviewer that only writes the name without reading the file cannot be rejected.

## Verification

A PoC (2026-11-14, outside the repository) confirmed the following.

- Applying the rule of 4 (does not hold if there is a finding of major or above) to 211 past review outputs matched the recorded judgment in 203. The 8 that disagreed were only records where the judgment and the findings did not fit together.
- With a fake agent, that 6 kinds of results that cannot be checked (a reply with approval only, a "met" without evidence, a quote not in the file, a judgment with no trace of examination, a missing criterion, a "not met" without a finding) are not counted.
- With real agents, claude, pi and codex, all 3, raised a finding on a candidate with a planted defect, quoting the line in question. 2 reviewers passed a candidate without the defect. All 3 replied in the decided form, and the quotes existed in the files.
- With a real agent, the check program ran inside codex's sandbox, passed on the candidate, and failed in the state where the candidate's tests were placed on the original code.

The material was 1 function and 3 acceptance criteria. Whether a large change gives the same result has not been confirmed.
