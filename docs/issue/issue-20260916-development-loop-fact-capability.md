---
id: issue-20260916-development-loop-fact-capability
kind: issue
title: Development Loop が未充足 fact の理由を次の Capability へ渡さず盲目的に再試行する
status: done
created: '2026-09-16'
issue_type: bug
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/kernel_v2.py
- runtime/development_loop/production_implementation_v2.py
- runtime/development_loop/production_diagnostic_v2.py
- runtime/development_loop/single_change_v2.py
summary: Evaluator の不受理理由が次の Capability 入力へ渡らず、同じ未充足 fact を盲目的に再実行する。
disposition_reason: production single-change frame evaluator の不受理条件を typed findings
  として次の同一 Capability inputへ渡し、snapshot evidenceの再評価も再帰せず処理する回帰テストを追加した。production
  implementation 12件と Development Loop 全684件で検証した。
updated: '2026-09-16'
---

## 症状 / 機会

Development Loop が Capability の成果を `UNSATISFIED` と評価しても、その理由を次に予約する同一 Capability の入力へ渡さない。このため、Goal が必要とする fact は正しく選ばれていても、producer は前回と同じ情報だけで再実行され、同じ種類の不受理を反復する。

## 証拠

- `issue-20260916-skills-suite-imports-removed-managed-child-entry-test` の single-change child Goal で、`implementation.frame` が `disposition: ready` を返した後も `premise.current: UNSATISFIED` となり、同じ `implementation.frame` が再予約された。
- 1回目は `constraint_checks[].constraint_ref` が request の文字列と完全一致せず不受理になった。2回目は `evidence_refs` の `:44-45` のような範囲 locator が assessor の単一行 locator 契約を満たさず不受理になった。
- 次の frame handoff には request だけが渡され、どの検査条件で `UNSATISFIED` になったかは含まれなかった。conductor が producer へ不受理理由を別途通知した場合にだけ、3回目で `premise.current: SATISFIED` へ進んだ。
- `runtime/development_loop/production_implementation_v2.py:96` の frame assessor は複数条件を単一の `bool` に畳み込む。
- `runtime/development_loop/single_change_v2.py:178` は assessor の `bool` を `SATISFIED` / `UNSATISFIED` に変換し、不受理理由を Evidence または次回入力へ保存しない。
- `runtime/development_loop/single_change_v2.py:257` は未充足 fact に対応する Capability を再選択するが、直前の evaluator rejection を入力に加えない。

## 着手に必要な文脈

- `change-20260913-development-loop-first-class-integration` は Development Loop を単一 semantic controller とし、Profile と直接 Capability / Child Goal で選択された成果へ到達する契約を持つ。
- `GoalKernel.tick()` の一 tick 一操作、immutable Evidence、native producer の排他的 lifecycle は維持する。
- producer の自己申告を成功根拠にせず、独立 evaluator が受理を決める境界は維持する。
- debug、requirements、design、test-harness、single-change で同じ欠陥が存在するかは未確認。起票時点で再現済みなのは production single-change の frame retry である。
