---
id: note-20261111-loop-sandbox-mcp-contract
kind: note
title: Sandbox 内 Loop と host MCP の実装契約案
status: draft
created: '2026-11-11'
tags: []
owners: []
relations: []
source_paths: []
summary: 資源操作は MCP tool を通す。独自 HTTP／socket 接続案を撤回し、sandbox コマンドの tool 要求を conductor
  が MCP tool として実行して結果を返す形へ修正。
---

## 接続案の撤回（2026-11-12）

この note が指定した独自 HTTP／Unix socket 接続と接続情報の注入は撤回する。MCP tool を呼ぶというユーザー指示を、同じ host handler に到達すればよいと誤って読み替えていた。下記の資源操作の責務分離は、その独自経路を正当化しない。eb331b40 の接続実装は不適合であり、後続の change-20261112-loop-mcp-tool-dispatch がその撤去と MCP tool 呼出しへの修正を扱う。

## 成果と構成

実装契約。確定境界は ADR adr-20261111-loop-sandbox-mcp-boundary に従う。sandbox 内に CLI、kernel、Unit Driver と判断ロジックを置く。host 側 MCP に認可、永続化、worktree 操作、既存 cross-host executor を置く。producer の配置と動作権限は既存 transport 契約に従う。

```text
sandbox: CLI → kernel / driver → resource ports + execution port
                                      ↓ MCP
host: admission → state / worktree adapters + cross-host executor
```

## 操作契約

| port | 入力と結果 | host 側の責務 |
|---|---|---|
| LoopState | repository に束縛された handle、snapshot revision、更新内容 → revision または conflict | 状態読書き、排他、原子的 commit、許可範囲の検査 |
| ArtifactStore | 管理 artifact の handle、内容、digest → 保存 handle／内容 | 入出力と成果の管理保存・取得。任意絶対パスを公開しない |
| RepositoryWorkspace | repository/worktree handle と必要な取得・変更操作 → snapshot／変更結果 | path と symlink の検査、Git/worktree 副作用、操作範囲の認可 |
| ProducerExecution | 既存 subsession start/continue/observe/recover/cancel 等 | provider 起動、receipt、revision、実プロセス観測 |

port は利用側の語彙で定義し、汎用 shell や任意ファイル read/write を公開しない。状態の意味・次の Execution の判断は sandbox の kernel が持つ。host は保存の原子性と認可を保証し、次の Execution を選ばない。変更 commit は expected revision 付き compare-and-swap とし、複数 file の ledger 更新を一つの transaction として保存する。kernel が snapshot から算出した更新を適用し、競合なら再読込みして再判断する。

最初の repository handle は server 起動時の origin と既存 CwdAuthority から発行する。worktree 作成・参照 repository 登録はその権限内の明示操作にする。handle は server の scope と照合し、所持だけを認可としない。state root は server 設定で決まり caller が変更できない。入力 goal/source は共有アクセスを仮定せず内容を送って保存する。成果も内容または resource で取得し、host の任意出力先を受け付けない。

## 起動と通信

MCP tool は用途に応じて定義・追加・変更する。既存 tool だけに限定しない。

sandbox 内から、MCP tool を呼ぶ。独自 HTTP／socket 接続や接続 URL／bearer の注入で tool 呼出しを置き換えない。driver が server を起動せず、Loop の判断を host へ移さない。単独 Python process という実行形態は固定せず、MCP tool を使う形へ見直す。sandbox コマンドは標準出力へ tool 名・引数・要求 id を返す。conductor が MCP tool を実行し、その構造化結果を標準入力へ返すと同じコマンドが続行する。この標準入出力は sandbox コマンドと conductor の受渡しであり、host 資源へのアクセスは MCP tool だけが行う。driver を別プロセスへ切り離さない。

## 排他・再試行・lifecycle

現在の filesystem lease を MCP 側の LoopState lease 操作に置換する。lease は driver session と fencing token に結び付ける。通信切断で lease が失効しても producer 終了とは扱わない。後継 driver は新しい token で snapshot と既存 operation receipt を照合し、同一 operation_id を再利用する。失効 token の更新は拒否する。executor の起動・継続の冪等性を再利用し、通信 timeout から新しい producer を起動しない。

MCP server が provider process と永続 receipt を所有し、driver 終了では provider を巻き添えにしない。server 自身の終了時の child cleanup は既存 transport 契約に従う。再接続で終了を確認できなければ recovery-unknown を返し、成功や停止を推測しない。明示 cancel は停止要求と writer 終了確認を区別する。

## 実装単位と順序

1. state、artifact、worktree、process の直接アクセス箇所を runtime/development_loop と依存先から列挙し、利用側 port と snapshot/commit 契約を定義する。純粋な kernel 判断は維持する。
2. host MCP に資源操作 adapter と契約テストを追加する。既存認可と executor を共有し、許可外 request が副作用へ到達しないことを検証する。
3. MCP tool を呼ぶ実行形態を定義し、全 CLI 操作と driver の資源操作をその経路へ接続する。open_execution_host の runtime 向け公開経路と呼出しを削除する。独立 MCP server 用 executor 実装は保持する。
4. 独自 bridge と接続情報注入を撤去し、MCP tool 経由の観測・再開を接続する。instructions.md の一律 sandbox 外指定を削除し、生成物へ影響すれば generator から同期する。

各単位の完了は次の契約テストで判定する。順序途中で実運用へ切り替えず、現在契約の producer/consumer を同期する。互換用の埋込み fallback は作らない。

## 検証計画

- lint/static: 変更対象 schema、prompt、generator、docs を検査。runtime から embedded host と provider 起動への依存を検査する。
- 通常テスト: fake と本番資源 adapter に同じ契約試験を適用。revision 競合、transaction 全反映／無反映、失効 token、異なる repository の handle、path traversal、symlink、操作拒否、副作用なしを確認。local test MCP を使い通信切断・再接続・重複 request・server 再起動の receipt を確認する。
- 受入れ E2E: sandbox の host directory 直接アクセスと provider 起動を拒否したまま open/drive/status/instruction/再開/export/cancel を完了する。並行 drive でも producer は一度だけ起動し、拒否 request で状態・worktree は変化しない。外部 provider と実 host を使うため別途明示承認が必要。

## 未検証の具体点

既存 ledger の transaction 範囲、MCP tool と sandbox 内の判断処理の受渡し、既存 server の複数接続と終了時 cleanup の振る舞いは実装前の確認対象。これらは上記境界を変更する理由にはしない。未対応なら明示停止し、対応する host adapter を整備する。

## 実装で確定した操作境界

host effect は保存済み Execution の固定文書記録と delivery commit／land を扱う。baseline、任意の検証コマンド、delivery postflight は caller sandbox に残す。host に汎用 command executor を置かない。host の Git 子 process は cwd admission と lifetime cell に束縛する。通常テストの注入境界と実 host sandbox の実証を区別する。
