---
id: adr-20261116-work-goes-back-while-it-converges
kind: adr
title: 差し戻しは収束している間だけ続け、回数の上限を持たない
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
summary: 否定された事実の指摘が、同じ事実の前回の否定より少ないときだけ作業を差し戻す。少なくならなければ stagnation で止まる。契約は差し戻しの回数の上限を持たない。
---

# 差し戻しは収束している間だけ続け、回数の上限を持たない

## 背景

単位は差し戻しの回数が契約の `limits.rounds` に達すると `rounds_exhausted` で止まり、否定の指摘の文が前回と一致すると `stagnation` で止まっていた。

2026-11-16 に、syntoria-trace の実装単位 14 件(完了 6 件、失敗 8 件)の記録を調べた。

- 回数の上限は、収束している単位を止めていた。止める指摘の数は relay-gate-corrected で 4→2→1、messenger-gated-send-adapter で 6→4→3→2 と減っていたが、上限で止まった。relay-common-send-gate は上限 2 のうち 1 回を生成物の掃除で使い、8 件の指摘を直す機会がないまま止まった。
- 文の一致による停滞は、評価役が同じ欠陥を別の文で書くと働かない。記録の評価役は、残った欠陥を「前回の指摘は未解消です」で始まる新しい文で書いていた。
- 完了した単位では、同じ事実の否定のたびに止める指摘が減っていた(2→1→0、3→1→0、1→0)。進まなかった単位は 2→2→3 だった。

## 決定

1. 契約は差し戻しの回数の上限を持たない。`unit.limits` と止まり方 `rounds_exhausted` を除く。
2. 否定された事実を作る実行へ戻すのは、その否定の指摘が、同じ事実の前回の否定の指摘より少ないときだけとする。少なくなければ `stagnation` で止まり、指摘と、今回と前回の数を記録に残す。最初の否定は比べるものがないので戻す。指摘の数は、評価では engine が計算した止める指摘、検査では落ちた検査、範囲では範囲外の path の数である。
3. 指摘を受けた後も候補や報告が変わらない turn は `stagnation` とする。ただし、その指摘の事実を判定した後に依頼者が回答したときは、回答だけで解消しうるので止めない([回答の決定](adr-20261116-answers-reach-every-execution.md))。
4. 否定された事実の指摘は 1 件以上で、同じ事実の否定のたびに減るので、どの単位も有限回の差し戻しで止まる。差し戻しの回数は記録に残すが、止める条件には使わない。

## 既存の判断との関係

- [事実による完了の決定](adr-20261114-unit-completes-when-facts-hold.md)の項目 1 の「上限」と項目 7 の「回数切れ」、[検査の決定](adr-20261114-checks-judged-from-execution-record.md)の項目 7 の「回数切れ」と「同じ指摘の繰り返し」を、本 ADR が置き換える。「停滞」は本 ADR の項目 2 と 3 の意味になる。両 ADR のほかの項目は変えない。
- [評価の決定](adr-20261114-verdict-computed-from-checkable-findings.md)の、確かめられない結果が 2 回続いたら止める規則は、本 ADR の対象ではなく、変えない。

## 比較した案

- **Profile ごとに上限を増やす。** 収束している単位を回数で止める問題が残る。却下した。
- **満たさない受入条件が減ることを前進とする。** 記録では、1 つの条件に複数の指摘があった単位(relay-gate-fresh-approval は条件 1 つで指摘が 2→1→0)が、前進していないとして止まる。却下した。
- **指摘の文の一致で同じ指摘を見分ける(従来の停滞)。** 評価役が文を変えると一致しない。却下した。
- **前回にない指摘が出たことを前進とする。** 評価役は新しい反例を見つけ続けられるので、単位が止まる保証がない。却下した。

## 結果

- 記録で上限により止まった単位のうち、指摘が減り続けていたもの(relay-gate-corrected、messenger-gated-send-adapter、messenger-personal-implementation、messenger-runtime-guards、messenger-browser-read、relay-common-send-gate)は続く。指摘が減らなかった messenger-personal-api-analysis は 2 回目の否定で止まる。
- 評価役が前回と同じ数の新しい欠陥を見つけたときも止まる。収束していないものとして扱う。
- 同じ数の検査が 2 回落ちると止まる。作る役は、自分の変更では通せない検査を報告で済ませず、質問で止まるよう定型文が求める。
- 指摘の数は評価役の書き方(1 つの欠陥を 2 つに分けるなど)に左右される。評価役には止まる条件を示さない。

## 確認方法

- 2026-11-16 の記録にある評価の列 23 単位に項目 2 を当て、完了した単位がすべて続く側に入ること、指摘が減らなかった単位が止まることを確かめた(背景の数)。
- `tests/test_facts.py` の `ConvergenceTest` と `tests/test_unit_flow.py` が、指摘が減る間は戻すこと、同数か増えたら止まること、事実ごとに前回と比べることを確かめる。

## ユーザーの決定

2026-11-16、Loop 記録の調査で挙げた改善点の項目 6(単位の粒度と回数の上限)に対し、ユーザーは「6. は試行回数による上限ではなく停止条件を定義する」と指示した。項目 2 の比べ方(同じ事実の前回の否定より指摘が少ないこと)は、この指示を受けて記録に当てて定めた。この比べ方を示した報告に対し、ユーザーは同日「6 の停止条件の承認 承認する」と答えた。
