---
id: note-20260905-loop-engineering-research
kind: note
title: Loop Engineering research note - external feedback, verification, long-running harness
status: draft
created: '2026-09-05'
topic: loop-engineering
tags:
- research-corpus
- loop-engineering
- agent-harness
- verification
- autonomous-development
owners:
- take.gn
relations: []
source_paths: []
summary: Surveys the literature on Loop Engineering, self-correction, long-running coding harness and software-engineering agent interface, and maps it to the design principles that the Development Loop of dev-skills needs
---

## Purpose

In adding a goal-level Development Loop to `dev-skills`, this note extracts the conditions under which such a loop holds from the research and practical findings of 2023-2026, instead of adding a simple retry / reflection loop.

The topics covered are:

- external loop specification
- self-correction / feedback
- independent verification
- long-running agent harness
- context reset / durable memory
- software-engineering agent interface
- simple deterministic workflows vs. heavyweight orchestration

The conclusion comes first.

> The source of truth of the Development Loop should be placed not in the Agent's introspection but in **sealed goal + external evidence + deterministic stop policy + durable state**.

---

## 1. Loop Engineering: external loop specification

### Macedo 2026 — Stop Hand-Holding Your Coding Agent

Source:

- https://arxiv.org/abs/2607.00038

This preprint distinguishes the subject of Loop Engineering from an ordinary programming loop and from the perceive-act-observe cycle inside an agent harness, and defines it as an external **loop specification**.

Main components:

- trigger
- goal
- verification
- stopping rule
- memory

The important point is not to "prompt the Agent many times" but to turn the contract of the iteration itself into an artifact, so that a human does not have to do step-by-step prompting.

Its descriptive analysis of a corpus of 50 loops reports that verification and named terminal states are relatively mature, and that automated trigger and durable memory are still weak.

### Implications for dev-skills

The current Playbook Runtime already owns L2 workflow feedback, so there is no need to mix Macedo's external loop into the playbook step DSL.

The new L3:

```text
sealed goal
  -> initial verify
  -> bounded episode
  -> independent verify
  -> deterministic stop/continue
```

It is natural for it to own only this.

Macedo is a preprint that organizes a new practical concept, and it is not to be overrated as a mature academic discipline. On the other hand, the way it puts verification burden / reward hacking / named terminals in the foreground agrees strongly with the existing dev-skills design.

---

## 2. Self-correction is governed by the quality of external feedback

### Kamoi et al., TACL 2024

Source:

- https://aclanthology.org/2024.tacl-1.78/

Main conclusions of the critical survey:

1. No prior work can be confirmed that shows general self-correction success with only feedback from the prompted LLM itself, except on tasks that are exceptionally suited to self-correction.
2. Self-correction works well on tasks where reliable external feedback is available.
3. Self-correction that involves fine-tuning is effective, as a separate category.

### Pan et al., TACL 2024

Source:

- https://aclanthology.org/2024.tacl-1.27/

It organizes automated correction into training-time / generation-time / post-hoc. It shows the importance of thinking about the feedback source and the correction mechanism separately.

### Reflexion / Self-Refine

Sources:

- Reflexion: https://papers.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html
- Self-Refine: https://arxiv.org/abs/2303.17651

Both are important earlier examples that showed the effectiveness of iterative feedback.

Reflexion carries external/internal feedback over into verbal memory, and Self-Refine showed improvement through feedback/refine iterations by the same LLM.

However, in light of the later critical survey in TACL, these must not be used as grounds to generalize to "make the Agent's own reflection the success verifier".

### Implications for dev-skills

The feedback packet of the Development Loop:

```text
failed check
failure code
expected / observed
artifact/evidence ref
protected binding
```

It consists mainly of these, and free-form introspection is not made a correctness authority.

Even if the reflection that the Executor generates can be used as an aid in searching for a solution, the external verifier takes priority as the input to the continue/success judgment.

---

## 3. Verification binds to the current postcondition

The strongest design principle in Loop Engineering is "completion text != proof".

In software engineering in particular:

- test
- build
- lint
- schema
- exact git tree / HEAD
- runtime readback

Environment-backed signals like these are available.

Dev-skills already has:

- verified tree
- consumer verification
- acceptance commit
- treatment of stale evidence as FAIL/UNKNOWN
- plan revision / authority binding

Therefore, lifting this external verification chain to the goal level is safer than inventing a new "judge" in the new Loop.

### Recommended verification ladder

```text
Level 1 deterministic static
  schema / file existence / digest / lint

Level 2 executable
  unit/integration/e2e/build

Level 3 environment readback
  runtime state / GitHub check / external system observation

Level 4 independent semantic review
  mechanically undecidable contract only

Level 5 executor self-report
  diagnostic only, not proof
```

Prefer the lower numbers, and do not make semantic review an all-purpose judge.

---

## 4. Anthropic long-running harness: durable artifacts and evaluator separation

### Harness design for long-running application development — 2026-03-24

Source:

- https://www.anthropic.com/engineering/harness-design-long-running-apps

Anthropic separates planner / generator / evaluator in long-running coding and examines the value of structured handoff, context reset and an external evaluator.

Important observations:

- With older/weaker agents, context management and a separate evaluator help a lot
- The model's own self-evaluation tends to be overly positive
- With strong models, there are also cases where the fixed cost of the full harness is instead wasted
- The harness should be simplified to match model capability

### Implications for dev-skills

1. Do not send the whole transcript to the next iteration.
2. Use canonical artifacts on disk + compact feedback for the handoff.
3. A fresh subsession is a valid execution policy, but it is not made a condition of semantic correctness.
4. Do not add a reviewer/evaluator unconditionally on every iteration. Reuse the mechanical gate + cross-task review of the existing plan-delivery.
5. The Development Loop itself does not keep "one more planner/evaluator Agent" permanently in place.

---

## 5. SWE-agent: agent-computer interface matters

Source:

- https://arxiv.org/abs/2405.15793

SWE-agent showed that the design of the Agent-Computer Interface (ACI), not the model alone, strongly affects software engineering agent performance.

Examples:

- a succinct file viewer
- search interface
- lint at edit time
- clear signals in tool results

### Implications for dev-skills

Do not improve the Loop through prompt sophistication alone.

`dev-skills` already has:

- typed requests
- bounded file scope
- verification commands
- host capabilities
- receipts

The Development Loop should therefore reuse these interface contracts.

Feedback is also made a tool/evidence-friendly packet, not "it failed last time, try harder".

---

## 6. Agentless: more complexity is not necessarily better

Source:

- https://arxiv.org/abs/2407.01489

With a simple three-stage structure of localization -> repair -> validation, Agentless showed competitiveness and low cost against complex agent frameworks on SWE-bench Lite at the time.

The benchmark generation and the absolute scores are not used to judge current model performance, but the architecture lesson is important:

> The complexity of dynamic autonomous planning/orchestration is not a value in itself.

### Implications for dev-skills

Do not put the following into the v1 Development Loop:

- dynamic skill chooser
- arbitrary planner
- multi-agent voting
- scheduler
- generic todo graph

Fix the Executor profile, and continue/stop on the external verification result only.

---

## 7. Fresh context vs. durable memory

What a long-running agent needs is not to "extend the same chat forever".

Recommended structure:

```text
canonical disk state
+ current sealed contract
+ current failed evidence
+ bounded repository context
```

On a host where fresh context is available, each iteration can be a new subsession, but resume correctness is not made to depend on conversation memory.

Dev-skills already materializes document/state/evidence to disk, which is consistent with this direction.

---

## 8. Make the stop condition named + machine-readable

A typical example of a Loop becoming dangerous:

```text
while not agent_says_done:
    ask_agent_again()
```

This does not prevent:

- infinite loop
- token burn
- reward hacking
- fake completion
- regression after temporary success

Recommended terminals:

- `SUCCESS`
- `NO_OP`
- `BLOCKED`
- `STALLED`
- `EXHAUSTED`
- `ABORTED`

By not collapsing these into the single word `FAILED`, the caller can correctly choose the next owner/action.

---

## 9. Stall detection uses the failure frontier, not repository activity

Looking only at the Git SHA and the changed files makes even a meaningless change look like progress.

A better signal:

```text
verification fingerprint
= acceptance digest
+ verifier digest
+ sorted(required check id, status, failure code)
```

### same-failure stall

```text
A -> A
```

Stop at a threshold.

### oscillation stall

```text
A -> B -> A -> B
```

This also stops.

The diff is diagnostic information, not a progress authority.

---

## 10. Do not double-count the retry budget across layers

Dev-skills already has:

- implement single repair
- playbook retry/back-to
- plan-delivery integration remediation

If the new Development Loop counts internal attempts directly, it breaks the existing ownership.

Therefore one iteration of L3 is defined as:

> one bounded terminal episode returned by an existing skill/playbook

When measuring cost amplification, the internal attempt telemetry may be consulted, but control ownership does not change.

---

## 11. Authority and reward hacking

The most important things to protect in an autonomous loop:

- user intent
- requirements
- acceptance criteria
- protected invariants
- verifier contract
- permission / authority

If an Agent that runs into a difficult implementation can rewrite the success condition, the loop breaks.

Therefore:

```text
before loop:
  seal goal / acceptance / verifier / authority binding

autonomous zone:
  implementation may change
  protected contract may not
```

A premise conflict results in `BLOCKED` + upstream hint, not automatic scope expansion.

This extends the authority separation of the current recovery / implement as it is.

---

## 12. Research-to-architecture mapping

| Evidence | Architecture implication |
|---|---|
| Macedo external loop spec | Separate L3 from the Playbook/Agent internal loop |
| TACL self-correction survey | Make reliable external feedback the correctness authority |
| Reflexion / Self-Refine | Iterating on feedback is useful, but reflection itself is not made proof |
| Anthropic long-running harness | Structured disk handoff, evaluator separation, avoiding harness over-complexity |
| SWE-agent | Emphasize a typed/tool-friendly interface |
| Agentless | Do not put dynamic autonomous orchestration in from v1 |
| dev-skills existing runtime | Reuse append-only ledger / exact binding / typed receipt |

---

## 13. Adopted principles

1. **Evidence-driven, not reflection-driven.**
2. **Verify before first execution.** Already-green goal is `NO_OP`.
3. **Stop immediately on current GREEN.** Do not continue improvement for its own sake.
4. **Exact-state verification.** Stale GREEN is prohibited.
5. **Sealed goal/verifier.** The success condition is protected from the executor.
6. **Deterministic controller.** The LLM is a semantic worker.
7. **Bounded retries.** Include the budget in the contract.
8. **Machine-visible stall detection.** activity != progress.
9. **Structured feedback only.** Do not make the raw transcript the iteration memory.
10. **Keep it smaller than a general agent control plane.** scheduler/portfolio are an outer layer.

---

## 14. Sources

### Loop Engineering / harness

- https://arxiv.org/abs/2607.00038
- https://www.anthropic.com/engineering/harness-design-long-running-apps

### Self-correction / refinement

- https://aclanthology.org/2024.tacl-1.78/
- https://aclanthology.org/2024.tacl-1.27/
- https://papers.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html
- https://arxiv.org/abs/2303.17651

### Software engineering agents

- https://arxiv.org/abs/2405.15793
- https://arxiv.org/abs/2407.01489

### Local architecture surfaces reviewed

- `runtime/workflow/playbook_runtime.py`
- `skills/_shared/schemas/playbook.schema.json`
- `docs/architecture/playbook.md`
- `docs/architecture/implement.md`
- `docs/architecture/plan-delivery.md`
- `docs/architecture/recovery.md`
- `docs/architecture/dev-evidence.md`
