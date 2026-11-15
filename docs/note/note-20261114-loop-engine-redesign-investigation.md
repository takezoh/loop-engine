---
id: note-20261114-loop-engine-redesign-investigation
kind: note
title: Loop Engine 再設計の調査記録(失敗の実測、原因、参照ハーネス、PoC)
status: draft
created: '2026-11-14'
tags: []
owners: []
relations: []
source_paths: []
summary: 2026-11-14 の調査。Loop の完了率、不完全な実装が繰り返される原因、参照ハーネスとの比較、PoC と実機確認の結果、確定した方針と未確認事項。
---

## Summary

Loop が安定して動かない原因と、再設計の方針を調べた記録である。完了しない Loop の大半は、成果の質ではなく実行基盤の段階で止まっていた。不完全な実装が繰り返される原因は、要件を運ぶ文書と検査を、実装するエージェント自身が同じ変更で書き換えられることにあった。方針は 2 件の ADR 案にまとめた。

- [engine の境界](../adr/adr-20261114-loop-engine-host-boundary.md)
- [検査の判定](../adr/adr-20261114-checks-judged-from-execution-record.md)

## Notes

### Loop の完了率(実行記録の集計)

`~/.dev-skills/development-loop/.families/*.sqlite3` の記録を、受理済みの品質評価が充足しているかで機械的に分類した。

| 記録 | 期間 | 件数 | 完了 | 取消 | 未完了 |
|---|---|---|---|---|---|
| この端末 | 11/07〜11/12 | 61 | 29 | 21 | 11 |
| 別の端末 | 9/29〜11/13 | 174 | 58 | 30 | 86 |

- 別の端末の未完了のうち、最後の記録が「実行開始」のまま終了記録がないものが 43 件、登録だけのものが 8 件。
- 完了しなかった implementation 63 件の review 回数は中央値 0。評価に届く前に止まっている。
- host MCP service 方式(11/13 以降)の記録は別の端末に 6 件。成果境界に到達 1 件、`producer-not-started` 3 件、`producer-output-missing` の繰り返しによる打ち切り 2 件。

### 不完全な実装が繰り返される原因

- commit `0540f7a5`(240 file)は、Loop を host へ移し、bwrap の隔離を追加し、`DEVELOPMENT.md` §11 の規則を削除し、skill-lint の境界規則(C11〜C15)の意味を入れ替え、test 103 file を書き換え、ADR 2 件を accepted で新規作成した。承認の記録はない。
- 以後の文書と検査は、違反後の状態を正として一致する。巻き戻しを指示されたセッションも、その文書を基準にしたため、producer 側だけが戻った(`00e288da`)。
- sandbox の中からは、git の書き込み、`~/.dev-skills` への書き込み、agent の起動ができない(実測)。Loop がこれらに依存していたことが、境界を越える経路を繰り返し生んだ。

### 参照ハーネスとの比較

文書とコードの読み取りによる。実際の動作は確かめていない。

| ハーネス | 進行の担い手 | 会話からの利用 | sandbox |
|---|---|---|---|
| Ouroboros | 切り離した worker | skill が MCP tool を呼び、待機・照会する | 持たない |
| Lobster | runtime | agent が tool として同期的に呼ぶ | 持たない |
| Clawpatch | CLI | 手段は見つからず | provider に頼る |
| autoresearch | shell の loop | なし | 持たない |

- どれも自前の sandbox と一度きり実行の台帳を持たない。
- Ouroboros は機械検査だけが受入を出せる。検査 command は project の設定 file から読む。
- Ouroboros は検査を host で実行していて、閉じ込めに関する未解決の issue がある。この点は取り入れない。

### PoC の結果

PoC は一時 directory に作り、repository には入れていない。engine 本体は約 670 行。

- 偽の agent での試験 38 件: 流れ、差し戻し、7 つの止まり方の区別、強制終了後の再開、8 経路の遮断、純粋関数の範囲が成り立った。
- 実機(CLI を直接起動):
  - `codex exec` は command と数値の終了コードを出す。
  - `claude -p` は数値の終了コードを出さない。
  - 既定の `claude -p` は、作業 directory の hook と `.mcp.json` の server を確認なしで起動した。
  - `--setting-sources user` を付けると起動しないが、`CLAUDE.md` と `AGENTS.md` も読まれなくなる。
  - codex は `.codex/config.toml` の MCP server を、読み取り専用 sandbox と信頼なしの指定でも起動した。
- git は、hook を無効にする設定と、属性を空の tree から読ませる指定の両方を付けて初めて、外部 program を起動しなくなった。

### 今の実装で、方針と合わない点

- `codex_driver.py` は producer の sandbox を自分で指定している。
- 書き込み先を強制できる driver だけを producer にする判定がある。
- turn の結果に、実行した command の記録がない。
- 対応表では pi を使う側が空で、無効になっている。

### 未確認

- 本体の `.git` が agent から読めない場所にある worktree。
- ACP 経由の claude で `~/.ssh` を一覧できた理由。環境の sandbox 設定がどこまで効いているか。
- pi が `--exclude-skills` を受け付けなかった理由。
- codex の記録に command が現れない条件。
- 取消になった Loop の個別の理由。

### 既存の抽象を通した実機確認

各 agent 3 turn。記録は一時 directory にあり、repository には入れていない。

- provider の共通処理は、MCP server なしで普通の process から組み立てて動かせた。
- 対応表のままでは起動できなかったものが 2 つある。
  - claude: launcher の起動前 check が失敗した。導入済みの bundle(11/12)の実行 file 名が、現在の repository が期待する名前と違う。
  - pi: 対応表が渡す `--exclude-skills` を、起動された pi が受け付けなかった。
- 実行記録: codex と pi は成否と数値の終了コードを出す。ACP 経由の claude は出さず、失敗する command も completed として届いた。3 つの driver はどれもこれらの event を捨てている。
- codex は、sandbox で失敗した command 1 件を記録に出さなかった。
- 作業 directory の設定: claude は hook と MCP server を起動した。codex は MCP server だけを起動した。pi はどちらも起動しなかった。
- 指示 file: claude は `CLAUDE.md`、codex と pi は `AGENTS.md` を読んだ。
- codex は、承認なしの指定のもとで git の実行を拒否した。利用者の設定が git を sandbox の外で実行するよう求めているためである。
- claude と pi は、worktree の中で `git status` と `git diff` を実行できた。本体の `.git` が読めない場所にある場合は試していない。

### 文書の棚卸し(同日)

status が無効を示す文書 172 件と、実装に対応物がない文書 20 件を `docs/archived/` へ退避した。
