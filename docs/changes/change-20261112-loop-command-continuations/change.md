---
id: change-20261112-loop-command-continuations
kind: change
title: 非対話コマンドと MCP tool で再開できる Loop の設計
status: draft
created: '2026-11-12'
profile: sdd@1
intent: Loop の判断と副作用を分離し、sandbox の有限な処理と MCP tool による境界操作だけで、利用側ラッパーなしに継続できる構造へ変更する。
outcomes:
- kernel は値だけを入力し、filesystem・controller・transport を呼ばず判断を返す。予約・回復だけでは producer を起動しない。
- 通常の非対話コマンドとセッションの MCP tool だけで Loop が継続し、利用側の中継スクリプト・socket・FIFO・対話 stdin を必要としない。
scope: []
non_goals: []
change_classes: []
governance:
  gate: auto
  reasons: []
members:
- role: requirements
  path: changes/change-20261112-loop-command-continuations/requirements.md
  required: true
- role: implementation
  path: changes/change-20261112-loop-command-continuations/implementation.md
  required: true
- role: verification
  path: changes/change-20261112-loop-command-continuations/verification.md
  required: true
promotion: []
unresolved_decisions: []
tags: []
owners: []
relations: []
source_paths: []
summary: 構造改修を実装中。kernel の純粋な判断、観測・保存の orchestration、明示的な effect 実行を分離する。対話 stdin
  の除去と非対話ホストでの検収までを完了条件とする。
---

## Summary

## Closure Notes
