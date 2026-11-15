---
id: note-20260905-loopx-dev-skills-fit
kind: note
title: LoopX と dev-skills の責務比較 — Optional L4 outer control plane 境界
status: draft
created: '2026-09-05'
topic: loop-engineering
tags:
- research-corpus
- loopx
- control-plane
- dev-skills
- architecture
owners:
- take.gn
relations: []
source_paths: []
summary: LoopX の generic long-horizon control plane と dev-skills の reactive development control kernel を比較し、Native Development Loop を L3、LoopX を optional L4 とする責務境界を定義
---

## 目的

LoopXを `dev-skills` のloop runtimeとして直接採用する場合と、Native Development Loopを実装してLoopXを上位control planeとして接続する場合を比較する。

本質は製品比較ではなく、**どのstate authorityを誰が所有するか**。

---

## 1. LoopX boundary

Reviewed sources:

- https://github.com/huangruiteng/loopx
- https://huangruiteng.github.io/loopx/docs/guides/minimal-custom-runtime-example/
- https://huangruiteng.github.io/loopx/docs/capabilities/issue-fix/

LoopXは provider-neutral / stateful control plane for long-horizon agents。

主なdurable control state:

- goal/objective
- todo/claim
- gate
- evidence
- quota
- scheduling hints
- handoff/recovery

Host runtimeはbounded turnを実行するworker。

LoopXは単なるretry helperではなく、generic long-horizon state kernelである。

---

## 2. LoopX is suitable for development

Issue-Fix capabilityは issue -> reproduce -> patch -> validation -> PR -> CI/review -> correction -> merge/close のlong-running lifecycleを扱う。

したがってLoopXはsoftware developmentにも十分適している。

特に強い領域:

- multi-day objective
- PR/CI/review wait
- wake/resume
- cross-agent claim/handoff
- quota
- operator attention/dashboard

ただしこれは `dev-skills` が最も差別化している development correctness semantics と同じ責務ではない。

---

## 3. Revised Native L3 model

Native Development Loopは fixed executor loopではなく、次のreactive kernelとする。

```text
bind goal
 -> observe live world
 -> reconcile durable history
 -> deterministically derive next bounded capability
 -> execute
 -> independently verify postcondition
 -> record
 -> terminal / wait / next tick
```

Native L3は generic Todo managerを持たない。

次のactionは canonical docs / repository / evidence / control ledger から導出する。

```text
L4 LoopX
  When/which goal should wake? How is quota/attention allocated?

L3 dev-skills Native Development Loop
  What is the current development world state, and what one bounded capability is justified next?

L2 Playbook Runtime
  What bounded procedural step/gate/effect happens inside that capability?

L1 local skill/repair
  How is the bounded transformation performed?

L0 host agent loop
  How does one model execution act/tool/observe?
```

---

## 4. Why LoopX should not be the L3 kernel

`dev-skills` already owns:

- requirements/design authority
- canonical plan/change artifacts
- playbook effects/gates
- exact repository verification
- verified tree/HEAD binding
- integration evidence
- CAS land
- append-only run ledgers
- host/provider routing

If LoopX also owns the internal development task/gate/evidence progression, questions arise:

```text
Goal source: LoopX objective or canonical change/requirements?
Task source: LoopX Todo or plan/task graph?
Gate source: LoopX Gate or Playbook/referee?
Evidence acceptance: LoopX evidence or dev verifier receipt?
Recovery/replan authority: LoopX or requirements/design owner?
```

Mirroring both sides creates dual control planes.

LoopX's own custom runtime guidance warns not to implement a second Todo/gate/evidence/quota/scheduler store.

The same principle should be applied in reverse: dev-skills should not make LoopX a second authority for development correctness.

---

## 5. Options

### A. LoopX as core L3

Pros:

- durable long-horizon state
- scheduler/quota/claims ready
- monitoring/dashboard ready

Cons:

- major authority/state redesign
- duplicate plan/task/Todo semantics unless deeply migrated
- core dependency on third-party control plane
- development correctness contracts become coupled to LoopX lifecycle

Reject for L3.

### B. All-native generic L4 + L3

Pros:

- total control

Cons:

- reimplements scheduler/Todo/claims/dashboard/quota
- expands dev-skills beyond mission

Reject.

### C. Native reactive L3 + Optional LoopX L4

Pros:

- preserves dev-specific authority/evidence
- no duplicate task source of truth
- native behavior works with/without LoopX
- LoopX can later add exactly the long-horizon capabilities it is good at

Cons:

- dev-skills must implement observation/reconciliation/policy/verification control
- adapter needed later

Adopt.

---

## 6. Ownership matrix

### dev-skills L3 / existing authorities

| Concern | Owner |
|---|---|
| user intent / requirement | requirements authority |
| design / plan / change artifacts | existing canonical docs owners |
| live repo/docs status observation | Native L3 observation |
| history vs live-world reconciliation | Native L3 |
| next bounded development action | Native L3 deterministic policy |
| bounded workflow internals | Playbook Runtime |
| repository mutation | owning Skill/Part/tool |
| exact completion verification | dev-skills verifier |
| development terminal receipt | Native L3 |

### LoopX L4 when enabled

| Concern | Owner |
|---|---|
| multiple-goal priority | LoopX |
| wake/schedule | LoopX/host |
| quota across goals | LoopX |
| claim/handoff across workers | LoopX |
| PR/CI/external wait monitoring | LoopX |
| operator attention/dashboard | LoopX |

### Never dual-own

- LoopX Todo <-> L3 action mirror
- LoopX Todo <-> plan task graph mirror
- LoopX Gate <-> Playbook gate mirror
- mutable LoopX text <-> acceptance/verifier definition
- LoopX evidence prose <-> dev verifier PASS authority

---

## 7. Correct integration shape

```text
LoopX Goal / wake
  -> adapter invokes or re-enters one dev goal
  -> Native L3 observes/reconciles
  -> runs a bounded development slice or reaches wait/terminal
  -> development-receipt@1
  -> LoopX stores one compact result/evidence reference
```

LoopX does not need to know:

- current plan-delivery cursor
- task-execution lane
- implement repair count
- remediation attempt
- exact internal reviewer role
- L3 internal action history

These remain behind the development capability boundary.

---

## 8. Why L3 is still needed if LoopX exists

Without Native L3, LoopX would need a domain capability that understands:

- canonical development authority
- requirements/design maturity
- plan/task readiness
- exact candidate/base state
- verifier freshness
- premise conflict
- current evidence frontier
- safe refresh/land conditions

That domain pack would effectively become a dev-skills-specific L3 anyway.

Keeping L3 native gives:

1. identical development semantics with/without LoopX;
2. tests next to canonical development runtime;
3. no third-party dependency for correctness;
4. a thin L4 adapter;
5. compatibility with other future outer controllers.

---

## 9. LoopX patterns worth borrowing

- bounded slices, not open-ended resident agent
- durable writeback before continuation
- independent validation of real postcondition
- explicit wait/human gate
- projection rather than duplicate UI state
- capability does not imply authority

These align with existing dev-skills principles.

---

## 10. What not to copy into Native L3

- generic Todo system
- multi-goal registry
- scheduling hints
- peer leases/claims
- quota allocator
- dashboard
- generic monitor framework

Those belong to L4.

---

## 11. When to add LoopX

Integrate only when a concrete workflow needs several of:

- multiple concurrent long-running goals
- wake after CI/review/external event
- recurring unattended continuation
- quota arbitration
- agent claim/handoff
- operator dashboard/attention queue
- multi-day issue -> PR lifecycle

A manually-triggered single development goal does not justify the dependency.

---

## 12. Future adapter acceptance

1. LoopX absent -> Native L3 works.
2. one outer goal wake maps to one dev goal re-entry, not internal actions.
3. dev receipt exact goal/repository/verifier binding is preserved.
4. LoopX cannot mark success when dev receipt is non-success.
5. duplicate wake does not launch duplicate unknown active effect.
6. authority drift surfaces from dev-skills rather than rewriting goal conditions.
7. raw transcripts/credentials are not copied between stores.
8. uninstalling LoopX leaves no required state in native playbooks.

---

## 13. Final decision

> **Native reactive Development Loop as L3; Optional LoopX as L4.**

The separation is semantic:

```text
LoopX L4:
  should this goal receive another wake/resources?

Native dev-skills L3:
  given the current development world, what single bounded action is justified next, and is completion externally proven?
```

This retains LoopX's strengths without creating a second development authority.

---

## 14. Sources

### LoopX

- https://github.com/huangruiteng/loopx
- https://huangruiteng.github.io/loopx/docs/guides/minimal-custom-runtime-example/
- https://huangruiteng.github.io/loopx/docs/capabilities/issue-fix/

### Local architecture reviewed

- `README.md`
- `COMPOSABILITY.md`
- `ARCHITECTURE.md`
- `docs/architecture/playbook.md`
- `docs/architecture/implement.md`
- `docs/architecture/plan-delivery.md`
- `docs/architecture/recovery.md`
- `docs/architecture/dev-evidence.md`
- `runtime/workflow/playbook_runtime.py`
