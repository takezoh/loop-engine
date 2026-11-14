---
id: adr-20261114-checks-judged-from-execution-record
kind: adr
title: 検査は agent が sandbox の中で実行し、engine は実行記録で合否を判定する
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-loop-engine-host-boundary}
source_paths: []
summary: 検査 command は project の設定に人が置き、producer が sandbox の中で実行する。engine は agent の報告文ではなく実行記録から合否を判定し、通らなければ評価役を呼ばない。
updated: '2026-11-14'
---

# 検査は agent が sandbox の中で実行し、engine は実行記録で合否を判定する

## 用語

**検査**は、test、lint、型検査、build のように、command の終了状態だけで合否が決まる確認を指す。**評価**は、評価役の agent が契約と差分を読んで下す判断を指す。

## 背景

2026-11-08 の記録では、最初の実装候補が通常テストを実行しないまま提出され、差し戻しが 1 往復増えた。独立評価が実機の欠陥を見逃した例もある。現在の受入は評価役の判断に寄っていて、決定的な検査が受入の条件になっていない。

一方、engine が検査 command を自分で実行すると、producer が書き換えたコードが host の権限で動く。[engine の境界の決定案](adr-20261114-loop-engine-host-boundary.md)はこれを認めない。

## 決定案

1. **検査 command は project の設定 file に人が置く。** 単位契約は名前で選ぶだけにする。
2. **実行するのは producer で、agent の sandbox の中である。** engine は実行しない。
3. **判定するのは engine である。** agent の報告文ではなく、agent の実行記録を使う。設定された command が成功し、その後に他の command も編集もないことを確かめる。
4. **通らなければ評価役を呼ばずに差し戻す。**
5. **検査は、導入時の複製にある固定の program 経由で実行させる。** engine は元の commit にある設定から、実行されるべき command の文字列を作り、producer への指示に含める。固定の program は検査 command を実行し、最後の行に終了状態を出力する。engine は、記録にある command が期待した文字列と完全に一致することと、出力の最後の行の終了状態を確かめる。agent が報告する成否や終了コードは判定に使わない。
6. **実行記録を取れない場合は「未検証」とし、承認しない。** 読めない出力を合格にしない。
7. **止まり方を区別して記録する。** 完了、質問待ち、回数切れ、同じ指摘の繰り返し、実行結果が不明、取消、agent 設定が安全でない、の 7 つとし、完了以外を完了に変えない。

## 既存の判断との関係

[成果の独立検収への統合](../../../docs/adr/adr-20261010-outcome-evidence-boundaries.md)は固定の検証段を廃止し、証拠の十分性を独立検収に任せた。本 ADR は独立検収を残したまま、その前に決定的な条件を 1 つ置く。同 ADR の Decision のうち「観測の実行を必須の中間工程にせず」の部分を、project が検査を設定している単位について本 ADR が変更する。同 ADR のほかの決定は変更しない。

本 ADR 全体は、2026-11-14 にユーザーが承認した(「2 approve」)。実装はまだ本 ADR に追従していない。

## 必要な変更

今の agent 起動の契約では、turn の結果は本文と通信の状態だけで、実行した command の記録を持たない。protocol ごとの driver が実行記録を返すように、契約を広げる必要がある。

## 比較した案

- **engine が検査を host で実行する:** 判定は最も確実だが、host で任意のコードが動く。
- **agent の報告文を信じる:** テスト未実行の提出を防げない。
- **検査専用の turn を別に起動する:** producer の省略は防げるが、往復が増える。必要になれば追加できる。

## 結果

- テスト未実行の提出が、評価役を呼ぶ前に機械的に止まる。
- 検査をごまかす余地(テスト自体の書き換えなど)は残る。差分に現れるので評価役が見る対象になる。
- sandbox の中では環境要因でテストが失敗することがある。元の tree との比較で、悪化した分だけを不合格にする扱いが要る。

## 確認方法

- 検査が通らない候補で、評価役が起動されないことを確かめる試験。
- 最後の編集より前に実行した検査、検査の後の編集が、合格にならないことを確かめる試験。
- 実行記録がない turn が「不明」になることを確かめる試験。

PoC では偽の agent でこれらが成り立った。実機では、`codex exec` の出力から command と数値の終了コードを取り出せた。`claude -p` の出力には数値がなく、成否の flag と本文から読み取った。既存の抽象を通した実機確認(2026-11-14、各 agent 3 turn)の結果は次のとおりである。

| protocol | 実行記録 | 現在の driver |
|---|---|---|
| codex-app-server | command、成否、数値の終了コードがある。ただし sandbox で失敗した command 1 件は記録に現れなかった | 捨てている |
| pi-rpc | command、成否、数値の終了コードがある | 捨てている |
| acp-v1(claude) | 終了コードがない。失敗する command(`false`)も completed として届いた | 捨てている |

項目 5 は、この結果を受けて 2026-11-14 に改めた(ユーザーの承認「accept」)。agent が報告する成否は、ACP 経由の claude では読めず、codex では抜けがありうるので、判定に使わない。改めた方法の前提は、次の事実で確かめた。

- 3 つの protocol とも、記録から command の文字列と出力本文を取り出せる。ACP 経由の claude でも、`echo ssh_rc=$?` の出力が command の文字列と一緒に届いた。
- 導入時の複製(`~/.local/share/agent-module/plugins/dev-skills`)は、agent-environments の共通の sandbox 方針で 3 つの agent とも読み取り専用であり、producer は固定の program を書き換えられない。

固定の program を 3 つの agent に実行させる通しの確認は、まだ行っていない。記録が抜けた検査は、項目 6 により「未検証」になる。
