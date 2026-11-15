---
id: issue-20261111-codex-host-loop-drive-sleep
kind: issue
title: Codex host で loop drive の完了通知が無く、メインセッションが sleep ポーリングし途中で停止する
status: done
created: '2026-11-11'
issue_type: bug
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/instructions.md
summary: Codex では loop drive の終了通知が届かず、メインが sleep ポーリングし、待機中に turn を終了して停止した
disposition_reason: loop drive の契約から host の終了通知への依存を除いた。driver を起動直後に終わる launcher
  経由で別の process group と session に起動し、呼出し元の process group にも子孫にも属さないようにした(Codex の process
  group 停止と Claude Code の子孫列挙による停止の両方で止まらない)。Loop ごとの lease と run 記録は driver_process.py
  が所有し、loop drive は実行中の run に接続して報告を返す。driver は呼出し時の run 記録を lease の下で照合してから駆動する。呼出しが先に終わっても同じ
  command の再実行で報告を受け取れる。instructions.md から background command と終了通知の前提を除いた。現在の構造は
  docs/architecture/development-loop.md に記載。Codex・Claude Code 上での live 確認は未実施
updated: '2026-11-11'
---

## 症状 / 機会

`runtime/development_loop/instructions.md:25` は「`loop drive` は終了を通知できる background command として実行し、終了まで待つ」と指示している。しかし Codex host のメインセッションには background command の終了通知が届かず、待つ手段が `sleep` によるポーリングしかない。その結果 (1) drive の待機中にメインが turn を終了して作業が止まり、(2) 完了の検知が最大約 50 秒遅れ、(3) 約 1 分ごとに進捗メッセージと model call が発生している。

## 証拠

- 2026-11-11、knowledge-pipeline の worktree `pteropod` のメインセッション(Codex、gpt-6.1-sol low、rollout `01a1c97e-333d-7cb1-87e0-c166af6bfdba`)。
- 16:29 JST に design loop を `open` / `drive` した後、16:31 にメインが turn を終了した。ユーザーが「じゃあなぜ止めた？」「だったら続けろよ」と 3 回促すまで約 3 分停止した。メイン自身の説明は「設計処理の完了待ちの途中で、私が誤って応答を終了した」。
- 以後のメインは `sleep {"duration_ms":50000}` と状況メッセージを約 1 分間隔で繰り返し、合計 40 回以上になった(16:40–17:31)。完了の把握は `loop status` の手動呼び出しに頼っていた。
- 関連する loop: `49c480541fa6…`(design)、`29c8763359…`(implementation asm)、`62f7ca90b7…`(implementation dashboard-ui)。いずれも `~/.dev-skills/development-loop/.families/` に保存されている。

## 着手に必要な文脈

- host 差の扱いは cross-host transport に置き、実行側に host 別 dispatch を置かない(AGENTS.md「開発原則」「変更規律」)。
- Codex に background 終了通知の手段があるか、`loop drive` を foreground で境界まで blocking 実行すれば足りるかは未確認。
