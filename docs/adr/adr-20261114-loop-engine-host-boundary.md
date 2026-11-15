---
id: adr-20261114-loop-engine-host-boundary
kind: adr
title: Loop Engine は host で動き sandbox を管理せず、LLM と任意のコードを host で実行する経路を作らない
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations: []
source_paths: []
summary: engine は host で動く固定のコードとし、sandbox は環境(claude・codex・pi の sandbox 設定)に任せる。project
  のコードと LLM は agent の sandbox の中だけで動かす。MCP を agent との接続面にし、CI からも同じ engine を使う。
updated: '2026-11-14'
---

# Loop Engine は host で動き sandbox を管理せず、LLM と任意のコードを host で実行する経路を作らない

## 背景

2026-11-13 の変更(commit `0540f7a5`)は Loop を host の MCP service へ移し、project の command を実行するために bwrap による隔離を自前で持った。同じ変更が `DEVELOPMENT.md` §11 の規則を承認の記録なしに書き換えている。11 月 9 日から 13 日の間に、Loop の制御方式は 5 回変わった。

sandbox は、姉妹 repository の agent-environments が claude、codex、pi に共通の方針で提供している。dev-skills が別の sandbox を持つ理由はない。その repository の決定は、sandbox の外で実行させる経路に「実行される program が固定で、呼び出し側が子 program を選べないこと」と「実体が sandbox から書ける場所にないこと」を求めている。

調査の事実と実測は[調査記録](../note/note-20261114-loop-engine-redesign-investigation.md)にある。

## ユーザーが述べた要件(2026-11-14)

- 「dev-skills は sandbox をもっていないはずだ。もっているとしたら、実装が元要件を破っている。」
- 「CI的にも使えるようにしたい。MCP はエージェントとループエンジンの接続面としたらいいだろう。」
- 「engine はホスト動作でいい。LLM や任意のコードがホスト側で実行される経路を作らないということだ。」
- 「loop engine は sandbox を管理しません。sandbox は環境が用意するもの。現状で sandbox を用意する主体は claude, codex, pi のサンドボックス設定です。」
- 「claude も codex も同じ振る舞いにし複雑性を入れない。止めるで統一する。対応する agent は claude, codex, pi の３つ」
- 「agent の起動は現在と同じく抽象化(claude, codex, pi, ACP, AppServer)して」

## 決定案

1. **engine は host で動く固定のコードとし、MCP server とは別の 1 つの process として動く。** この process が複数の単位を実行し、単位ごとに process を作らない。CLI と MCP の入口は、engine に要求を渡し、状態の記録を読むだけで、進行・判定・記録の処理を持たない。CI では CLI が engine を起動し、渡した単位が止まるまで待って、止まり方を終了コードで受け取る。engine が止まったら、次の起動で記録から再開する。
2. **engine は sandbox を作らず、確かめず、変えない。** bwrap などの隔離を持たない。起動する agent に sandbox や承認の指定を渡さず、利用者の設定のまま起動する。agent ごとの例外や、書き込み先を強制できるかによる選別を持たない。
3. **LLM の出力と project の内容は、host で実行される program、引数、設定を決めない。** engine が起動するのは git と、導入済みの対応表にある agent の起動 command だけである。project の command(test、build、script)を engine は実行しない。
4. **agent の起動は既存の抽象を使う。** provider の共通処理と、protocol ごとの driver(codex-app-server、acp-v1、pi-rpc)を維持する。engine は「turn の開始、進捗、結果、取消」という自分の言葉の port だけに依存する。
5. **作業 directory の agent 設定は、変更されたときだけ止める。** 対象は `.claude/`、`.codex/`、`.pi/`、`.agents/`、`.mcp.json` とする。worktree を作った直後と agent を起動する直前で内容の hash を比べ、違えば止める。元からある定義は project の内容として読ませる。この扱いは 3 つの agent で同じにする。
6. **状態は `~/.dev-skills` に置き、書き手は engine だけにする。** 契約と回答は MCP tool の入力として data で渡す。
7. **engine は同じ repository の中の独立した package とし、導入時の複製から起動する。** 作業 tree のコードを読み込まない。将来は別の repository へ独立させる見込みなので、plugin 側のコードに依存させない。

host で実行されうる経路は、MCP tool の入力、単位契約と検査の設定、agent の出力、起動引数、git、作業 directory の agent 設定、engine 自身のコード、実行 file の解決、の 8 つとして扱い、それぞれに拒否の試験を持つ。

## 既存の判断との関係

| 判断 | 扱い |
|---|---|
| [host MCP service](../../../docs/adr/adr-20261113-loop-host-mcp-service.md) | host で動かし目的別の MCP tool で接続する点は維持する。台帳と復旧の作りは縮小する |
| [隔離実行](../../../docs/adr/adr-20261113-loop-effect-isolation.md) | 置き換える。engine が project の command を実行しないので、隔離の port は不要になる |
| [producer の環境](../../../docs/adr/adr-20261113-producer-development-loop-skill.md) | 維持する。producer は利用者の環境で動き、外すのは Loop を開く skill だけ |

項目 6 と 7 は、2026-11-14 にユーザーが承認した(「6. approve」「7. approve おそらく将来的には独立させるだろう」)。項目 1 の process の分け方は、ユーザーの指示による(「プロセスを分離した方が良いのでは？ シンプルな方を選べ」「タスクごとにプロセス化する必要ないだろ」)。

本 ADR 全体は、2026-11-14 にユーザーが承認した(「1 approve」)。この承認により[隔離実行](../../../docs/adr/adr-20261113-loop-effect-isolation.md)を置き換える。実装はまだ本 ADR に追従していない。`DEVELOPMENT.md` §11 と `AGENTS.md` の該当箇所の同期は、文面をユーザーが承認してから行う。agent-environments 側では、engine を sandbox の外で実行させる経路として棚卸しに加える必要がある。

## 比較した案

- **Loop を sandbox の中の CLI に戻す:** sandbox の中からは git の書き込み、状態の保存、agent の起動ができない(実測)。この依存が、境界を越える経路を繰り返し生んだ。
- **engine が検査 command を host で実行する:** 参照ハーネスの多くが採る形だが、producer が書き換えたコードが host の権限で動く。
- **engine を MCP server の process の中で動かす:** 会話と CI で実行の形が 2 つになる。MCP server が止まると単位も止まり、MCP の入口が engine の処理を抱える。
- **単位ごとに process を作る:** 必要がない。process の数が増え、agent の host process を単位の間で共有できない。
- **agent の起動を非対話 CLI の直接起動に置き換える:** 既存の抽象を維持するという指示により採らない。
- **元の commit に agent の定義があれば開始時に止める:** dev-skills を含め、定義を持つ repository を対象にできなくなる。

## 結果

- 自前の隔離、その中で動く worker、一度きり実行のための台帳が不要になる。
- 元からある定義が repository 内の script を呼ぶ場合、producer が書き換えた script が host で動く余地は残る。普段のセッションと同じ扱いである。
- agent の sandbox が有効かどうかは環境の責務であり、engine の側では保証しない。
- engine の process は 1 つなので、agent の host process を単位の間で共有できる。
- 入口から engine へ要求を渡す手段は、実装の設計で決める。
- 会話や MCP server が終わっても、実行中の単位は進み続ける。

## 確認方法

- 両 package に `bwrap` と `unshare` の記述がない。
- 起動された実行 file を記録する試験で、一覧の外のものが 0 件である。
- 8 つの経路それぞれに、command や path を混ぜた入力で何も起動されず状態も変わらないことを確かめる試験がある。
- 進行と判定の module が file、時刻、process を import しない。

PoC では、偽の agent を使った 38 件の試験でこれらが成り立った。CLI の入口が止まり方ごとに別の終了コードを返すことと、強制終了後に同じ command で再開できることも、この試験で確かめた。今の実装には MCP server 以外の入口がなく、CLI の入口は新たに作る必要がある。

既存の抽象を通した実機確認(2026-11-14、各 agent 3 turn)で、次を確かめた。

- provider の共通処理は、MCP server なしで普通の process から組み立てて turn を動かせる。engine を別の process にできる。
- 今の driver は、項目 2 に反する指定を渡している。codex には常に承認なし(`approvalPolicy: never`)と自前の sandbox(`workspace-write`)を送り、ACP では agent からの権限要求を自動で拒否する。
- driver の `workspace-write` では `~/.ssh` を一覧できた。利用者の permission profile では見えなかった。自前の指定のほうが読み取りが緩い。
- 作業 directory の設定の読み込みは agent ごとに違う。claude は hook と MCP server を起動し、codex は MCP server だけを起動し、pi はどちらも起動しない。項目 5 の検査は、この違いに関係なく同じ対象に適用する。
- agent と sandbox 自身が、作業 directory に空の file や directory を作る。項目 5 の hash はこれらを除いて計算する必要がある。
