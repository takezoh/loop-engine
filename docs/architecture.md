# Loop Engine の構造

現在の構造だけを書く。決定の理由と比較した案は [adr/](adr/) にある。

## 責務

| module | 責務 |
|---|---|
| `facts.py` | 契約の受付検査、次の実行の選択、事実の成立と無効化、検査と評価の判定。値だけを扱う |
| `prompts.py` | 実行に渡す指示文(session の最初のメッセージと続きのメッセージ)の組み立て。値だけを扱う |
| `unit.py` | 1 単位を、保存された状態から次に止まるところまで進める |
| `service.py` | engine process。単位ごとに thread を持ち、要求を受け付ける |
| `store.py` | 単位の記録、要求と応答の file、process の lock |
| `git.py` | git の起動。hook、filter、外部 diff を無効にした引数を必ず付ける |
| `worktree.py` | worktree の観測(agent 設定の digest、tree の digest、変更された path) |
| `check_runner.py` | agent が sandbox の中で実行する固定の検査 program |
| `agents.py` | agent の turn を実行する port。session は中身を解釈しない token で表す |
| `composition.py` | port を provider transport に接続する。transport に触れる唯一の module |
| `client.py` / `cli.py` | 入口。要求を渡し、記録を読む。進行と判定の処理を持たない |

## 契約

```
unit_id, repository
unit:        goal, criteria[{id, text}], scope[paths], limits{rounds}, base(省略可)
facts:       id, required, by: given | report | checks | verdict | git, と種類ごとの項目
executions:  id, agent, model, effort, instructions, reads, establishes, writes, independent_of
```

- 事実は、成立したときの根拠(判定の対象と読んだ事実の内容)を持つ。根拠が現在と同じ間だけ成立する。
- engine は、読む事実がすべて成立していて、成立させる事実に未成立が残る実行を選ぶ。
- `checks`・`verdict`・`git` の事実が否定されると、その対象を作った実行へ指摘を渡して戻す。対象を作る実行がなければ、指摘を結果として終える。
- `unit.base` を指定すると、その commit から現在の commit までの既存の変更を対象にする。

## agent の session

実行ごとに 1 つの agent session を持つ。

- 実行の最初の turn は新しい session を開始し、役の定型文に指示の全文を入れて送る。port は turn が動いた session の token を返し、engine は実行ごとに `state.json` に保存する。
- 同じ実行が再び走るとき(差し戻し、回答、やり直し)は、その token で session を続け、役の続きの定型文(`protocol/<役>-follow-up.md`)を送る。続きのメッセージは、指摘・回答・やり直しの理由、その session がまだ受け取っていない報告、評価役には今の変更 file の一覧、と返答の形式を持つ。指示、目的、受入条件は送り直さない。
- 異なる実行は session を共有しない。評価役は作る役の session を見ない。
- port が「session を続けられない」と返したら、engine はその turn を 1 回だけ、指示の全文で新しい session として起動し直す。その失敗はほかの失敗した turn と同じに扱う。
- token は `state.json` にあるので、engine が再起動しても同じ session を続ける。
- 単位が続けられない止まり方で終わると、engine は保っていた session をすべて閉じる。続けられる止まり方では保つ。engine の停止は、起動したすべての agent の process を終える。
- agent 設定の検査、worktree より上の検査、取消は、続けた turn にも同じく当てる。事実の判定に使う実行記録は、その turn のものだけである。

transport への接続(`composition.py`)では、token は provider の session の識別子(Codex の thread、ACP と Pi の session)である。transport は turn が終わるたびに session を unload し、続けるときに識別子で読み込み直す。

## 承認の要求

agent が承認、permission、利用者の入力を求めたら、transport が明示的に拒否して答え、turn は続く。Codex には承認の指定を送らない。3 つの driver の答え方は [subsession の構造](../../docs/architecture/subsession.md)にある。

拒否した要求は、種類(`command`、`file_change`、`permissions`、`input`)と短い説明で turn の実行記録(`actions.refused`)に残る。engine は turn の回数を実行ごとに足し、`status.json` と `result/stop.json` の `refused` に出す。拒否で単位を止めない。拒否されて実行できなかった検査は合格の行を残さないので、その事実は成立しない。

## 記録

状態の置き場所(`DEV_SKILLS_HOME` の下の `loop-engine/`)に、単位ごとの directory を作る。

| file | 内容 |
|---|---|
| `contract.json` / `admission.json` | 受け付けた契約と、受付時に固定した元の commit・検査の定義 |
| `state.json` | 判断の状態と、実行ごとの session の token、拒否された要求の回数。engine が止まっても、ここから続ける |
| `events.jsonl` | 開始、turn の開始(新しい session か続けた session か)と終了、続けられなかった session、要求、session を閉じたこと、停止の記録 |
| `checks.json` | 検査 program が読む設定 |
| `result/patch.diff` / `result/stop.json` | 元の commit との差分と、止まり方・事実の成否・指摘・実行ごとの拒否の回数 |
| `result/turns/NN-<実行>.json` | 各 turn の返答、実行記録、session の token、拒否の件数 |

worktree は状態の置き場所とは別の、agent の sandbox が書ける場所(`DEV_SKILLS_WORKTREES`)に作る。単位ごとに、作業用と、元の commit の複製の 2 つを持つ。

単位が続けられない止まり方(`waiting_for_answer`、`unknown_result`、`invalid_result` 以外)で終わると、engine は 2 つの worktree を消す。変更は `result/patch.diff` に残る。

この場所はどの agent も書けるので、そこにあるものを信用しない。

- worktree の directory 名には、単位の id に推測できない値を付ける。先に同じ名前の directory を用意されても使わない。
- 作った worktree が、途中に link を挟まず、engine が作った場所そのものにあることを確かめる。
- worktree より上の directory には、engine が作る directory(repository ごとの directory と、単位の worktree)しか置かない。それ以外のものがあれば、agent を起動せずに止める。turn の前と後の両方で確かめる。
- git は、worktree を作った時点で読んだ git directory を明示して起動する。agent が worktree の `.git` を書き換えても、その先を使わない。
- agent 設定が link の場合は、link の先の内容まで digest に含める。読む量には上限があり、超える設定の単位は進めない。

確認と agent の起動のあいだに別の agent が file を置く余地は残る。この場所を agent から書けなくする手段は、engine ではなく環境の sandbox 設定が持つ。

## 止まり方と終了コード

| 止まり方 | 終了コード | 続け方 |
|---|---|---|
| `done` | 0 | |
| `waiting_for_answer` | 10 | `answer` |
| `rounds_exhausted` | 11 | |
| `stagnation` | 12 | |
| `unknown_result` | 13 | `retry` |
| `cancelled` | 14 | |
| `unsafe_agent_config` | 15 | |
| `rejected` | 16 | |
| `invalid_result` | 17 | `retry` |
| `stuck` | 18 | |

要求が受け付けられなかった場合は 2、engine を起動できなかった場合は 3 を返す。

## 検査

project は `.dev-skills/checks.json` に検査を定義する。engine は元の commit にあるこの file だけを読む。

```json
{"checks": {"unit": {"argv": ["python3", "-m", "unittest"], "test_paths": ["tests"]}}, "ignore": ["__pycache__"]}
```

検査が書き出す cache(`__pycache__` など)は `ignore` に書く。書かないと、検査の前後で tree の digest が変わり、検査が「現在の tree で実行されていない」と判定される。

agent は engine が指定した command を実行する。検査 program は結果の最後の行に、検査の名前、対象(候補か元の commit か)、実行時の tree の digest、終了コードを出す。engine は実行記録のこの行だけで合否を判定する。

## 検証の対応

| 確かめること | テスト |
|---|---|
| 判断の規則、判定の規則、契約の受付 | `tests/test_facts.py` |
| 実際の repository と engine での流れ、止まり方、再開、取消 | `tests/test_unit_flow.py` |
| session の継続、続きのメッセージ、続けられない session、session を閉じること、拒否の回数 | `tests/test_sessions.py` |
| 要求の受付、要求 file、CLI、engine process の起動 | `tests/test_service.py` |
| 検査 program | `tests/test_check_runner.py` |
| 起動する process が git だけであること、純粋な module の範囲、repository の hook や filter が動かないこと | `tests/test_closed_paths.py` |
| agent の port の契約(transport への接続と台本の偽物の両方に当てる) | `tests/test_transport_agents.py` |
| 検証 tool | `tests/test_tools.py` |
