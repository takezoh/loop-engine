---
id: note-20261111-loop-mcp-access-boundary
kind: note
title: Loop の MCP 操作境界と sandbox アクセス設計案
status: draft
created: '2026-11-11'
tags: []
owners: []
relations: []
source_paths: []
summary: Loop／driver を sandbox 内に保ち、host 資源アクセスを MCP の検査・認可経由に限定する確定境界。先の集約案は撤回。
---

## 確定した境界

ユーザー指示: 「loop はサンドボックスの中で動いている。 sub agent (subsession) を起動するにはサンドボックスを越える必要がある。それをMCPがホスト側で検閲することで提供している。worktree や状態ディレクトリへのアクセスも同じだろう。二度とこの境界を破壊するな。」

開発上の設計原則は DEVELOPMENT.md §11 に記録した。Loop／driver は sandbox 内、host 資源への操作は、MCP tool の呼出しと、その先の検査・認可を経由する。同じ handler を呼ぶ独自接続はこの境界を満たさない。driver に executor を埋め込むことも、driver を host 側へ移すことも認めない。

## 撤回した提案

このメモで先に提案した「MCP に Loop 操作を集約する」案は撤回する。Loop の制御と host 資源へのアクセスを混同していた。CLI に直接アクセスを与える案、一律 sandbox 外実行、既存の埋込み executor 経路の継続利用も採用しない。

## 判断時点で観測した実装

runtime/development_loop/driver_process.py の _production_host は open_execution_host を呼ぶ。packages/cross-host-transport/src/cross_host_transport/subsession/execution_host.py は MCP handler を呼出し元プロセスに組み込む。この経路と runtime/development_loop/instructions.md の一律 sandbox 外指定は、上記境界に整合させる必要がある。この記録時点では実装修正と動作検証は未実施だった。

後続の設計は sandbox 内 driver から独立した host 側 MCP を呼ぶ契約を具体化する。起動、状態・worktree 操作、再接続、拒否時の副作用防止を対象とし、既存 ledger と lease の業務規則を重複実装しない。

実装修正と検証の記録は [change package](../../../docs/changes/change-20261111-loop-sandbox-mcp-boundary/change.md)、現在の構造は [subsession architecture](../../../docs/architecture/subsession.md) を参照する。
