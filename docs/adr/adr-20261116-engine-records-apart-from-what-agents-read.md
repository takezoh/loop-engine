---
id: adr-20261116-engine-records-apart-from-what-agents-read
kind: adr
title: engine の記録を agent が読むものから分け、agent から読めない場所に置く
status: accepted
created: '2026-11-16'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-verdict-computed-from-checkable-findings}
source_paths: []
summary: 記録(契約、状態、出来事、turn の記録、結果、要求、transport の状態)を DEV_SKILLS_HOME/records/loop-engine に移し、agent の sandbox が読めなくする。agent が読む検査 program、単位の checks.json、元の commit の複製は DEV_SKILLS_HOME/loop-engine に残す。
---

# engine の記録を agent が読むものから分け、agent から読めない場所に置く

## 背景

engine は、状態の置き場所(`DEV_SKILLS_HOME/loop-engine`)を agent の sandbox から読めて書けない場所として使っていた。そこには agent が読むもの(検査 program、単位の `checks.json`、元の commit の複製)と、engine の記録(契約、状態、出来事、turn の記録、結果、transport の session の状態)が並んでいた。共通の sandbox 方針は `~/.dev-skills` 全体を読み取り許可にしていた。

2026-11-16 の記録では、評価役が 4 単位の 9 turn で、この場所の記録(`result/` の turn の記録と `state.json`)を読んでいた。作る役の turn の記録(`result/turns/02-implement.json`、作る役の返答と実行記録)を読んだ turn もある。元の commit の複製を読んだ turn は、渡された場所を読んだもので、ここに数えない。engine は評価役に作る役の報告を渡さない([評価の決定](adr-20261114-verdict-computed-from-checkable-findings.md)項目 5)が、file として読めていた。pi の session の記録(`agents/pi-sessions/`)も同じ場所にあった。

## 決定

1. engine の記録(契約、受付、状態、出来事、状態の要約、turn の記録、結果、要求と応答、lock、transport の状態)を `DEV_SKILLS_HOME/records/loop-engine` に置く。
2. agent が読むもの、すなわち検査 program(`check/<sha256>/check_runner.py`)と、単位ごとの `checks.json` と元の commit の複製(`units/<unit_id>/`)は `DEV_SKILLS_HOME/loop-engine` に置く。役に渡す path(検査の command、元の commit の場所)はこの場所だけを指す。
3. agent の sandbox は記録の場所を読めなくする。共通の sandbox 方針は、`~/.dev-skills` 全体ではなく `~/.dev-skills/loop-engine` だけを読み取り許可にし、`~/.dev-skills/records` を拒否する。読めなくするのは環境の sandbox 設定であり、engine は作らず、確かめない。
4. 2 つの場所は、どちらの path も他方の前方一致にならない名前にする。path の前方一致で許可や拒否を判定する設定でも、一方の規則が他方に当たらないためである。

## 既存の判断との関係

- [engine の境界の決定](adr-20261114-loop-engine-host-boundary.md)の項目 6(状態は `~/.dev-skills` に置き、書き手は engine だけ)は変えない。記録も公開物も `~/.dev-skills` の下にあり、engine だけが書く。sandbox を engine が持たない規則(同項目 2)も変えない。
- 評価役に作る役の報告を渡さない規則(評価の決定の項目 5)を、file の読み取りでも保てるようにする。

## 必要な変更

- 共通の sandbox 方針(agent-environments の `modules/_lib/sandbox-policy.json`)の読み取り許可を `~/.dev-skills` から `~/.dev-skills/loop-engine` に狭め、`~/.dev-skills/records` を拒否に加える。共通 compiler は読み取り許可の内側の拒否を受け付けず、Codex は `~/` の拒否を allowlist の外として扱うので、許可を狭めることで 3 つの host で同じに読めなくなる。各 host の setup で反映する。
- 以前の engine が `~/.dev-skills/loop-engine` に残した記録(`units/` の記録、`agents/`、`inbox/`、`outbox/`)は、記録の場所へ移すまで agent から読める。
- sandbox の中で動くメインセッションも、記録と `~/.dev-skills` のほかの directory を直接読めなくなる。単位の状態と結果は、host の process が読む MCP の `unit_status`・`unit_result` で受け取る。

## 比較した案

- **engine が評価役の実行記録に記録を読んだ跡を探し、その評価を数えない。** 実行記録に path が現れない読み方を検出できず、読んだ後の session も残る。ユーザーが選ばなかった。
- **記録を今の場所に残し、agent が読むものを別の場所へ移す。** 入口と検証 tool が読む場所は変わらず、以前の記録もそのまま読めなくなる。ユーザーが選んだ方式(記録を別の root へ移す)と異なるので採らなかった。
- **`~/.dev-skills` の読み取り許可を残し、記録の場所だけを拒否する。** 共通 compiler が読み取り許可の内側の拒否を拒み、Codex はその拒否を表せない。採らなかった。

## 結果

- sandbox の設定が入ると、どの役も、ほかの役の返答、実行記録、session の記録を file から読めない。役が受け取るのは、契約が読むと宣言した事実と依頼者の回答だけになる。
- 以前の Loop の記録(`~/.dev-skills` の下の `composition/`、`development-loop/` など)も agent から読めなくなる。
- 記録を調べる作業(Loop の記録の調査など)は、sandbox の外の読み取りか MCP の tool を使う。

## 確認方法

`tests/test_unit_flow.py` が、役に渡すメッセージが記録の場所を指さず agent が読む場所を指すこと、2 つの場所が互いの内側にも前方一致にもならないこと、agent が読む場所に検査 program と `checks.json` しか残らないこと、記録の場所に `checks.json` と元の commit の複製がないことを確かめる。共通方針の翻訳は agent-environments の `modules/tests/test_sandbox_contracts.py` と `test_shared_sandbox.py` が確かめる。sandbox が記録を読めなくしていることは、各 host の setup の後に実機で確かめる。

## ユーザーの決定

2026-11-16、項目 5 の方式を尋ねた問いに対し、ユーザーは「記録を分離し sandbox で遮断 (推奨)」を選んだ。問いの説明は「agent が読む物だけを今の場所に残し、記録を別の root へ移す。その root を agent-environments の共通 policy で deny にする」だった。最初の実装はこれと逆に公開物を移していたため、ユーザーの「sandbox 側の遮断(5)、5 の実装方式の変更を修正対応して」に従い、説明した方式に直し、共通の sandbox 方針も変更した。
