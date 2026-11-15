---
id: issue-20260925-drivetime-overhead
kind: issue
title: design Goal run が遅く、run 中の repo 編集で evidence が無効化される (駆動摩擦・tree_sha 比較の妥当性)
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
summary: design Profile の Goal run は 14 capability を 1 step 1 subagent で直列駆動するため遅く、
  continue セッションの attempt 取り違え・domain reject・anchor 欠落で再実行が重なる。加えて conductor が run
  中に repo file を編集すると working tree の tree_sha が変わり先行 evidence が 一括無効化され run が継続不能になる。tree_sha
  比較がどの surface を守るべきかは未決。
disposition_reason: design の Goal run(capability の直列駆動)と、observe.py の capture_binding、attempts.py
  は実装に存在しない。
updated: '2026-11-14'
---

## 症状 / 機会

design Profile の Goal run は固定の 14 capability を 1 step = 1 subagent で直列に駆動するため遅い。subagent の再実行と conductor の observe 往復が重なると 1 run が長時間化する。さらに run 中に conductor が repo file を編集すると working tree の tree_sha が変わり、先行 evidence が一括で無効化されて run が継続不能になる。

期待する成果: (a) step 間の round-trip を減らすか overlay し、失敗 step だけを再実行できる、(b) run 中の非 governed file (docs 運用文書など) の編集が run 全体を無効化しない。少なくとも tree_sha 比較がどの surface を守る invariant なのかが宣言されている。

## 証拠

- **直列長と往復**: `profiles/design/profile.yaml` の capability 順は sources → context → constraints → problem → decision → draft-1 → draft-2 → integrate-2 → review → revise → minimality → reassess → package → record (14 node)。各 producing step は `goal dispatch` → `task` subagent → `goal observe-start` → `goal observe-terminal` → `goal advance` の ~5 CLI 呼び出し + 1 LLM turn を直列に要求する。`goal agent-step` は observe-start 前 (native_producer=null) では常に `agent-loop-producer-not-started` で fail するため、step 単位の中断性がない。
- **高コスト出力**: design.package の design-publication は prose + spine + 3 member markdown で ~106KB。これを長大な continue セッション (`ses_f2bc14…`) の 1 turn で生成するため重く、応答が返らないように見えた (本 run で 2 回キャンセルされた)。
- **再実行コスト**: draft-1 は初回 subagent が output.json を書かず再実行、minimality は domain reject で 2 attempt、package は anchor 欠落で新 attempt が予約され続けた (`daba73cb` → `a06c42ca` → `dacf007c`)。
- **attempt 取り違え (package)**: attempt `daba73cb…` (design.package, continue step6) を designer セッション `ses_f2bc14…` に実行させたところ、このセッションが package ではなく旧 revise attempt `09c174aa…` を再実行して output.json を上書きした。「manifest 提示 path の hash が実在しない」という誤認で誤った attempt に fallback した。continue セッションの context 蓄積が原因と推定。
- **domain reject (minimality)**: attempt `e0179023…` が `rejected / native-output-invalid:production-composition-domain-transition-invalid`。原因は `runtime/policies/planning/policy.py` の `validate_result` が pair/group 参照に `classifications[].signal_id` との byte 一致を要求するのに対し、LLM が descriptive 名で記述したこと。再実行 attempt `56cd3254…` で解消。参照 ID の一致は事前に機械検証されない。
- **package の UNSATISFIED 再試行ループ**: design.package の evaluator は `runtime/policies/planning/policy.py` の `PlanningBundle.validate` で design.md の全 spine identity に `<!-- anchor: <id> -->` を要求する。producer の design-publication は JSON schema (`artifacts/design-publication/schema.json`) は通るが anchor 30 件が欠落し、evidence は ACCEPTED かつ `result: UNSATISFIED` のまま新 package attempt が予約される。schema は anchor 要求を表現しないため schema-valid でも domain 検証で再試行が続く。anchor を補完すると `validate` は 0 error。
- **run 中の repo 編集が evidence を無効化**: `runtime/development_loop/observe.py::capture_binding` の `files_digest` (= tree_sha) は working tree 全体 (tracked + untracked。`docs/index/` は ignored で無影響) のハッシュ。conductor が run 中に repo file (issue 起票・change.md member 追加・consultation-evidence 追加) を編集したため、先行 evidence の tree_sha (`ac445407…`) と現在の tree (`39d68622…`) が不一致になり `sources.current` が STALE、続いて全 evidence が BLOCKED になって run が継続不能になった。attempt 単体では `runtime/development_loop/attempts.py::_verify_result` の `readonly-world-changed` が発火する。
- **先に起票済み**: issue-20260924-debug-goal-observation-gate-verification (observation gate の完全一致要求で goal が回復不能になる)。

## 着手に必要な文脈

- 発見元: `docs/changes/change-20260924-integrator-host-observation` (design Goal run 本体)。
- **未決の問い: tree_sha (working tree 全体) の比較は必要か。** これが守っているのは「readonly attempt が宣言外の repo 変更をしないこと」と「evidence が同一 world を指すこと」である。しかし現行の粒度では、design の governed surface (`runtime/development_loop/**` など) ではない docs 運用文書 (`docs/issue/**`、`docs/changes/**` のメタ) の編集でも run 全体が無効化される。`docs/.docs-config.yaml` は dev-evidence 用に `evidence.meta_paths` / `generated_paths` を既に宣言しており、比較 surface を governed path に限定する先例になりうる。決めるべきは (a) 比較対象を Profile の declared source/scope surface に限定するか、(b) meta path 編集で run を継続できる明示 re-baseline を置くか、(c) 厳格なまま「run 中の repo 編集禁止」を運用規律にするか — evidence currency が何を守る invariant なのかで結論が変わる。
- 対象 module: `agent_loop_v2.py` (agent-step gating)、`planning/policy.py` (reference/anchor 検証)、`native_producer_v2.py` (producer 束縛)、`observe.py` (capture_binding / tree_sha)、`attempts.py` (readonly-world-changed)。

## 2026-09-25 実行状況

- 対象 run (design Goal `6b38f619…`) は `sources.current` STALE (tree_sha 不一致) で継続不能となり `goal cancel`。design 内容 (candidate A' 採択・plan・review/minimality) は runtime で accept 済みのため、受理済み package 出力から change `change-20260924-integrator-host-observation` を手動 materialize して回収した (design-plan/design.md + spine.yaml + 3 member、`docs lint` green)。
- 本 issue 自体は未解決のまま (design の遅さ・run 中編集での evidence 無効化・package anchor ループのいずれも runtime 側は未修正)。
- tree_sha 比較の必要性は上記「未決の問い」の通り未確定。


{% transition from="open" to="rejected" date="2026-11-14" %}
2026-11-14 の棚卸しで実装と照合した(ユーザー指示「実装と照らし合わせて実装されていないものは archive」)。design の Goal run(capability の直列駆動)と、observe.py の capture_binding、attempts.py は実装に存在しない。
{% /transition %}
