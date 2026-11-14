---
id: adr-20261114-agent-sessions-continue-and-approvals-are-refused
kind: adr
title: 実行は自分の agent session を続け、承認の要求は明示的に拒否して turn を続ける
status: accepted
created: '2026-11-14'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: dependsOn, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-unit-completes-when-facts-hold}
source_paths: []
summary: 同じ単位の同じ実行が再び走るときは、その実行の前の session に差分だけの続きのメッセージを送る。agent の承認の要求は 3 つの agent
  で同じく明示的に拒否し、turn は続け、拒否の回数を単位の記録に残す。
updated: '2026-11-14'
---

# 実行は自分の agent session を続け、承認の要求は明示的に拒否して turn を続ける

## 背景

Loop Engine は turn ごとに新しい agent session を開始し、閉じていた。同じ実行が差し戻し・回答・やり直しで再び走るたびに、指示の全文を送り直していた。

2026-11-13 より前の Loop は、実行が観測した agent の session を次の実行でも続け、その session がまだ受け取っていない内容だけを送っていた(`runtime/development_loop/agent_assignment_v2.py` の `mode: start | continue` と `delivered_prompts`)。失われた session は続けなかった。transport は今も継続の手段を持つ。provider session の `continue`、Codex の同じ thread での `turn/start` と process が無くなった後の `thread/resume`、ACP の `session/load` と `session/prompt`、Pi の `--session-id` である。

agent の承認の要求の扱いは 3 つの driver で違っていた。

- Codex には `thread/start` と `thread/resume` で常に承認なしの指定を送り、要求そのものを起こさせなかった。
- ACP(claude)は permission の要求に `cancelled` を返すか、環境変数で許可が与えられていれば `allow_once` を選んでいた。この環境変数を設定するものは repository に無い。
- Pi は extension の dialog に答えず、turn が戻らないことがあった。

[engine の境界の決定](adr-20261114-loop-engine-host-boundary.md)の項目 2 と `DEVELOPMENT.md` §11 は、engine も transport も agent に sandbox や承認の指定を渡さず、利用者の設定のまま動かすと定めている。承認なしの指定は、この規則に反していた。

## 決定

ユーザーは 2026-11-14 に次を決めた。

### session の継続

1. **同じ単位の同じ実行が再び走るとき(指摘の差し戻し、質問への回答、やり直し)、engine はその実行の前の session に続きのメッセージを送る。**
2. **異なる実行は session を共有しない。** 評価役が作る役の session を見ることはない。
3. **続きのメッセージが運ぶのは新しいもの(指摘、回答、変わった事実)と返答の形式だけである。** 指示の全文を送るのは session の最初のメッセージだけとする。
4. **session を続けられないとき(engine が再起動して session を再開できない、provider が session を失った)は、engine は指示の全文で新しい session を開始する。**
5. **session は単位が最終的に止まるまで保ち、止まったら閉じる。**
6. **評価役も同じ規則に従う。** 評価役の後の評価は、評価役自身の session を続ける(ユーザー:「評価役： 続ける」)。

### 承認の要求

7. **agent の承認の要求には明示的な拒否で答え、turn は続ける。** claude、codex、pi で同じとする(ユーザー:「拒否を明示的に返し、turn は続ける」で 3 agent を揃える)。agent に承認の指定を渡さない。engine と transport が承認する経路を持たない。拒否した回数を単位の記録に残す。

## 比較した案

- **turn ごとに新しい session を開始する(これまでの engine):** 規則 1 と 3 により採らない。再び走るたびに指示の全文を送り直し、agent はそれまでの作業の文脈を失う。
- **評価役は毎回新しい session で評価する:** ユーザーが「評価役： 続ける」と決めた。独立性は規則 2(実行ごとに別の session)で保つ。
- **承認の要求があったら単位を止める:** ユーザーの決定は「turn は続ける」である。要求を拒否されて実行できなかった検査は実行記録に合格の行を残さないので、その事実は成立しない。止めなくても、事実の成立で判断する[事実による完了の決定](adr-20261114-unit-completes-when-facts-hold.md)のまま扱える。
- **要求をメインセッションへ上げて判断を待つ:** 承認するかを engine の外から決める経路になる。[engine の境界の決定](adr-20261114-loop-engine-host-boundary.md)は engine が sandbox と承認に関わらないことを求めており、agent の承認は利用者の設定が決める。engine は要求を拒否し、回数を記録して見えるようにするだけにする。

## 結果

- 差し戻し・回答・やり直しの turn は、指示の全文の代わりに差分だけを受け取る。続きのメッセージも返答の形式を含むので、engine は毎回の返答を同じ規則で検査できる。
- engine は実行ごとの session の token を単位の記録に保存し、中身を解釈しない。engine が再起動しても、記録から同じ session を続けられる。
- session を続けられないことを transport が知らせると、engine はその turn を 1 回だけ新しい session でやり直す。新しい session の失敗は、ほかの失敗した turn と同じに扱う。
- 実行記録は turn ごとに区切られる。続けた session でも、前の turn の検査の行が後の turn の事実を成立させることはない。
- 承認を要する操作は、利用者の設定がその操作を許していない限り、agent の中で実行されない。利用者の設定で承認なしに動く操作は、要求そのものが起きない。
- 拒否された要求の件数と内容(種類と短い説明)が turn の記録に残り、単位の状態・止まり方の記録・集計 tool に実行ごとの回数として出る。
- 続きのメッセージを agent がどう解釈するか、各 provider が session をどこまで保つかは、実機で確かめる必要がある。

## 確認方法

- engine のテストで、差し戻しが作る役の session を差分だけのメッセージで続けること、回答が質問した実行の session を続けること、評価役の 2 回目が評価役自身の session を続けること、2 つの実行が同じ token を受け取らないこと、続けられない session が全文の新しい session 1 回に置き換わること、token が engine の再起動を越えて残ること、最終的な停止で session を閉じ続けられる停止では保つこと、前の turn の検査の行が事実を成立させないこと、続けた turn を取消が中断すること、を確かめる。
- transport のテストで、3 つの driver それぞれについて、agent が受け取る答えが明示的な拒否であること、turn がその後普通に終わること、turn の記録に拒否が残ることを確かめる。Codex の `thread/start` と `thread/resume` に承認の指定が無いこと、どの環境変数でも ACP が許可を選ばないことも確かめる。
- skill-lint の `producer_environment`(C16)が、承認の指定を置くことを拒否する。


{% transition from="proposed" to="accepted" date="2026-11-14" %}
ユーザーが 2026-11-14 に承認した。session の継続は、提示した規則 1〜5 への返答「評価役： 続ける」。承認の要求は「1. 拒否を明示的に返し、turn は続ける」で 3 agent を揃える」。
{% /transition %}
