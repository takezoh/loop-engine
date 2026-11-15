---
id: note-20260905-loop-engineering-research
kind: note
title: Loop Engineering 研究ノート — external feedback、verification、long-running harness
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
summary: Loop Engineering、self-correction、long-running coding harness、software-engineering agent interface の文献を調べ、dev-skills の Development Loop に必要な設計原則へ写像する
---

## 目的

`dev-skills` に goal-level Development Loop を追加するにあたり、単純な retry / reflection loop ではなく、2023-2026 の研究・実務知見から成立条件を抽出する。

対象は:

- external loop specification
- self-correction / feedback
- independent verification
- long-running agent harness
- context reset / durable memory
- software-engineering agent interface
- simple deterministic workflows vs. heavyweight orchestration

結論を先にまとめる。

> Development Loop の正本は Agent の内省ではなく **sealed goal + external evidence + deterministic stop policy + durable state** に置くべき。

---

## 1. Loop Engineering: external loop specification

### Macedo 2026 — Stop Hand-Holding Your Coding Agent

Source:

- https://arxiv.org/abs/2607.00038

このpreprintは Loop Engineering の対象を、普通のprogramming loopやagent harness内部の perceive-act-observe cycleと区別し、外部の **loop specification** として定義する。

主要構成:

- trigger
- goal
- verification
- stopping rule
- memory

重要なのは「Agentに何度もpromptする」のではなく、人間がstep-by-step promptingをしなくても済むよう、反復の契約自体をartifact化すること。

Corpus 50 loops の記述分析では verification と named terminal states が比較的成熟し、automated trigger と durable memory はまだ弱いと報告している。

### dev-skillsへの意味

現在のPlaybook Runtimeは L2 workflow feedback を既に所有するため、Macedoのexternal loopをplaybook step DSLへ混ぜる必要はない。

新規L3は:

```text
sealed goal
  -> initial verify
  -> bounded episode
  -> independent verify
  -> deterministic stop/continue
```

だけを所有するのが自然。

Macedoは新しい実務概念を整理したpreprintであり、成熟した学術disciplineとして過大評価しない。一方、verification burden / reward hacking / named terminalsを前面に出す点は既存のdev-skills設計と強く一致する。

---

## 2. Self-correctionは external feedback の質が支配する

### Kamoi et al., TACL 2024

Source:

- https://aclanthology.org/2024.tacl-1.78/

Critical surveyの主要結論:

1. prompted LLM自身からのfeedbackだけで一般的なself-correction成功を示した先行研究は、例外的に自己修正向きのtaskを除いて確認できない。
2. reliable external feedbackが使えるtaskではself-correctionがよく機能する。
3. fine-tuningを伴うself-correctionは別カテゴリとして有効性がある。

### Pan et al., TACL 2024

Source:

- https://aclanthology.org/2024.tacl-1.27/

automated correctionを training-time / generation-time / post-hoc に整理。feedback source と correction mechanism を分離して考える重要性を示す。

### Reflexion / Self-Refine

Sources:

- Reflexion: https://papers.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html
- Self-Refine: https://arxiv.org/abs/2303.17651

両者は反復feedbackの有効性を示した重要な先行例。

Reflexionはexternal/internal feedbackをverbal memoryへ持ち越し、Self-Refineは同じLLMのfeedback/refine反復で改善を示した。

ただしTACLの後続critical surveyを踏まえると、これらを根拠に「Agent自身のreflectionをsuccess verifierにする」と一般化してはいけない。

### dev-skillsへの意味

Development Loop のfeedback packetは:

```text
failed check
failure code
expected / observed
artifact/evidence ref
protected binding
```

を主とし、free-form introspectionを correctness authority にしない。

Executorが生成するreflectionは解法探索の補助として使えても、continue/successの判定入力はexternal verifierを優先する。

---

## 3. Verificationは current postcondition にbindする

Loop Engineeringで最も強い設計原則は「completion text != proof」。

Software engineeringでは特に:

- test
- build
- lint
- schema
- exact git tree / HEAD
- runtime readback

のようなenvironment-backed signalが利用できる。

Dev-skillsには既に:

- verified tree
- consumer verification
- acceptance commit
- stale evidenceのFAIL/UNKNOWN扱い
- plan revision / authority binding

がある。

したがって新Loopで新しい「judge」を発明するより、このexternal verification chainをgoal levelへ持ち上げる方が安全。

### 推奨verification ladder

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
  diagnostic only, proofではない
```

下位番号を優先し、semantic reviewを万能judgeにしない。

---

## 4. Anthropic long-running harness: durable artifacts and evaluator separation

### Harness design for long-running application development — 2026-03-24

Source:

- https://www.anthropic.com/engineering/harness-design-long-running-apps

Anthropicは long-running codingで planner / generator / evaluator を分離し、structured handoff、context reset、外部evaluatorの価値を検討している。

重要な観測:

- older/weaker agentではcontext管理とseparate evaluatorが大きく効く
- model自身のself-evaluationは過度にpositiveになりやすい
- 強いmodelではfull harnessの固定費が逆に無駄になるケースもある
- harnessはモデル能力に合わせて簡素化すべき

### dev-skillsへの意味

1. transcript全体を次iterationへ送らない。
2. diskのcanonical artifacts + compact feedbackをhandoffに使う。
3. fresh subsessionは有効なexecution policyだがsemantic correctness条件にはしない。
4. reviewer/evaluatorを毎iteration無条件追加しない。既存plan-deliveryのmechanical gate + cross-task reviewを再利用する。
5. Development Loop自身は「もう一人のplanner/evaluator Agent」を常設しない。

---

## 5. SWE-agent: agent-computer interface matters

Source:

- https://arxiv.org/abs/2405.15793

SWE-agentは model 単体ではなく Agent-Computer Interface (ACI) の設計がsoftware engineering agent性能を大きく左右すことを示した。

例:

- succinctなfile viewer
- search interface
- edit時lint
- tool resultの明確なsignal

### dev-skillsへの意味

Loopの改善をprompt sophisticationだけで行わない。

`dev-skills`では既に:

- typed requests
- bounded file scope
- verification commands
- host capabilities
- receipts

を持つため、Development Loopはこれらのinterface contractを再利用すべき。

Feedbackも「前回だめだった、頑張れ」ではなく tool/evidence-friendly packet にする。

---

## 6. Agentless: complexityを増やせばよいわけではない

Source:

- https://arxiv.org/abs/2407.01489

Agentlessは localization -> repair -> validation という単純な3段構造で、当時のSWE-bench Lite上で複雑なagent frameworkに対して競争力と低コストを示した。

Benchmark世代や絶対scoreは現在のモデル性能判断には使わないが、architecture lessonは重要:

> dynamic autonomous planning/orchestrationの複雑性は、それ自体が価値ではない。

### dev-skillsへの意味

v1 Development Loopに:

- dynamic skill chooser
- arbitrary planner
- multi-agent voting
- scheduler
- generic todo graph

を入れない。

Executor profileをfixedにし、external verification結果だけでcontinue/stopする。

---

## 7. Fresh context vs. durable memory

Long-running agentで必要なのは「同じchatを永久に伸ばす」ことではない。

推奨構造:

```text
canonical disk state
+ current sealed contract
+ current failed evidence
+ bounded repository context
```

Fresh contextが有効なhostでは各iterationを新しいsubsessionにできるが、resume correctnessはconversation memoryに依存させない。

Dev-skillsは既にdocument/state/evidenceをdiskへmaterializeしており、この方向と整合する。

---

## 8. Stop conditionはnamed + machine-readableにする

Loopが危険になる典型例:

```text
while not agent_says_done:
    ask_agent_again()
```

これでは:

- 無限loop
- token burn
- reward hacking
- fake completion
- regression after temporary success

を防げない。

推奨terminal:

- `SUCCESS`
- `NO_OP`
- `BLOCKED`
- `STALLED`
- `EXHAUSTED`
- `ABORTED`

`FAILED` 一語に潰さないことで、callerが次のowner/actionを正しく選べる。

---

## 9. Stall detectionは repository activity ではなく failure frontier

Git SHAやchanged filesだけを見ると、意味のない変更でもprogressに見える。

よりよいsignal:

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

閾値で停止。

### oscillation stall

```text
A -> B -> A -> B
```

も停止。

Diffは診断情報でありprogress authorityではない。

---

## 10. Retry budgetは層ごとに二重計上しない

Dev-skillsには既に:

- implement単回repair
- playbook retry/back-to
- plan-delivery integration remediation

が存在する。

新Development Loopが内部attemptを直接数えると既存ownershipを破壊する。

したがってL3の1 iterationは:

> 既存skill/playbookが返す1 bounded terminal episode

とする。

Cost amplificationを測るときは内部attempt telemetryを参照しても、control ownershipは変えない。

---

## 11. Authority and reward hacking

Autonomous loopで最も重要な保護対象:

- user intent
- requirements
- acceptance criteria
- protected invariants
- verifier contract
- permission / authority

難しい実装に遭遇したAgentが成功条件を書き換えられると loopは壊れる。

したがって:

```text
before loop:
  seal goal / acceptance / verifier / authority binding

autonomous zone:
  implementation may change
  protected contract may not
```

premise conflictは自動scope expansionではなく `BLOCKED` + upstream hint。

これは現行 recovery / implement のauthority separationをそのまま延長する。

---

## 12. Research-to-architecture mapping

| Evidence | Architecture implication |
|---|---|
| Macedo external loop spec | L3をPlaybook/Agent内部loopと分離 |
| TACL self-correction survey | reliable external feedbackをcorrectness authorityにする |
| Reflexion / Self-Refine | feedback反復は有用だがreflection自体をproofにしない |
| Anthropic long-running harness | structured disk handoff、evaluator separation、harness over-complexity回避 |
| SWE-agent | typed/tool-friendly interfaceを重視 |
| Agentless | dynamic autonomous orchestrationをv1から入れない |
| dev-skills existing runtime | append-only ledger / exact binding / typed receiptを再利用 |

---

## 13. Adopted principles

1. **Evidence-driven, not reflection-driven.**
2. **Verify before first execution.** Already-green goal is `NO_OP`.
3. **Stop immediately on current GREEN.** Improvement for its own sakeを続けない。
4. **Exact-state verification.** stale GREENは禁止。
5. **Sealed goal/verifier.** success conditionはexecutorから保護。
6. **Deterministic controller.** LLMはsemantic worker。
7. **Bounded retries.** budgetをcontractに含める。
8. **Machine-visible stall detection.** activity != progress。
9. **Structured feedback only.** raw transcriptをiteration memoryにしない。
10. **Keep it smaller than a general agent control plane.** scheduler/portfolioは外部層。

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
