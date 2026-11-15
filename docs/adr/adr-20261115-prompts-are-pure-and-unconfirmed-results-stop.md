---
id: adr-20261115-prompts-are-pure-and-unconfirmed-results-stop
kind: adr
title: 指示文の組み立ては値だけを扱い、確かめられない結果では止まって自分ではやり直さない
status: accepted
created: '2026-11-15'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
- {type: references, target: adr-20261114-agent-sessions-continue-and-approvals-are-refused}
source_paths: []
summary: engine の指示文の組み立て(prompts.py)は判断(facts.py)と同じく値だけを扱う。turn の結果を確かめられない単位は理由を付けて止まり、engine
  は自分ではやり直さず、呼出し元の retry の要求だけでやり直す。
updated: '2026-11-15'
---

# 指示文の組み立ては値だけを扱い、確かめられない結果では止まって自分ではやり直さない

## 背景

engine の規則のうち 2 つは、engine の決定として記録されていなかった。

- 「判断は値だけを扱う」は[事実による完了の決定](adr-20261114-unit-completes-when-facts-hold.md)の項目 8 が定めるが、対象は判断(`facts.py`)である。役に渡す指示文を組み立てる `prompts.py` も同じ規則で書かれ、`tests/test_closed_paths.py` が計算だけの module しか import しないことを検査しているが、それを定めた記述は engine を使う側の開発指針にしかなかった。
- 「turn の結果を確かめられないときは止まり、自分ではやり直さない」は、engine の振る舞い(`unknown_result`、`invalid_result` と、やり直しの要求)として実装され文書化されているが、決定としての記録がなかった。

engine の規則は engine の文書に置く。使う側の文書からこれらの規則を除いたので、engine の決定として記録する。

## 決定

1. **指示文の組み立て(`prompts.py`)は、判断(`facts.py`)と同じく値だけを扱う。** I/O、時計、乱数、process、global な実行文脈を持たない。定型文の本文と、指示文に入れる値(契約、状態、報告、指摘、検査の command、agent 設定の名前)は、呼び出す側が値として渡す。
2. **turn の結果を確かめられない単位は、理由を付けて止まる。** 次の場合は `unknown_result` で止まる: agent の turn が完了しなかった、session の解放を確かめられなかった、作業用の複製を読めなかった、検査または判定の対象になる turn に実行記録がなかった、書かない役の turn の後に差分が変わっていた、engine 自身が判定の前に失敗した。判定の返答を決められた形として確かめられないときは、受け取らなかった理由を付けて同じ役に 1 回だけ出し直させ、同じ判定で 2 回続いたら `invalid_result` で止まる。
3. **engine は、止まった単位を自分では再開しない。** やり直すのは、呼出し元が `retry` を要求したときだけである。engine は結果を推測せず、代わりの結果も作らない。
4. **session を続けられない turn を、新しい session で 1 回だけ起動し直すことは、この規則の外とする。** それは[session の決定](adr-20261114-agent-sessions-continue-and-approvals-are-refused.md)の規則 4 が定める、同じ turn の届け直しである。

## 比較した案

- **確かめられない turn を engine が自動でやり直す:** 採らない。原因が分からないまま同じ操作を繰り返すことになり、agent の利用枠を使い、作業用の複製の状態が分からないまま次の判定に進む。止まった理由を呼出し元が見て決めるほうが、何が起きたかを失わない。
- **確かめられない結果を失敗として扱い、差し戻しに数える:** 採らない。差し戻しは、成立しなかった事実に対する指摘を作る役に返す仕組みである。結果が分からないことは指摘ではなく、役に返しても直せない。
- **指示文の組み立てに file の読み込みを許す:** 採らない。定型文を読むのは配線の側だけにすると、組み立ては入力の値だけで決まり、test で入力と出力を突き合わせて確かめられる。

## 結果

- 指示文は、同じ値からいつも同じ文になる。定型文の変更の影響は、組み立ての入力と出力の test で確かめられる。
- 呼出し元は、`unknown_result` と `invalid_result` の単位について、止まった理由(`stop.json` の detail)を読んでから `retry` を決める。engine が止まっている間に、利用枠は使われない。
- 一時的な障害(agent の一時的な失敗など)でも単位は止まる。続けるには呼出し元の要求が要る。

## 確認方法

- `tests/test_closed_paths.py` が、`facts.py` と `prompts.py` の import が計算だけの module(`re`、`json`、`shlex`、`hashlib`)に限られ、`open`、`print`、`exec` などを呼ばないことを検査する。
- engine の test(`tests/test_unit_flow.py`、`tests/test_sessions.py`、`tests/test_judged_trees.py`、`tests/test_environment_entries.py`、`tests/test_facts.py`)が、決定 2 の各場合に単位が `unknown_result` または `invalid_result` で止まり、`retry` の要求があるまで次の turn が始まらないことを確かめる。
