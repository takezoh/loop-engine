# Loop Engine の構造

現在の構造だけを書く。決定の理由と比較した案は [adr/](adr/) にある。

## 責務

| module | 責務 |
|---|---|
| `facts.py` | 契約の受付検査、次の実行の選択、事実の成立と無効化、検査と評価の判定。値だけを扱う |
| `prompts.py` | 実行に渡す指示文の組み立て。値だけを扱う |
| `unit.py` | 1 単位を、保存された状態から次に止まるところまで進める |
| `service.py` | engine process。単位ごとに thread を持ち、要求を受け付ける |
| `store.py` | 単位の記録、要求と応答の file、process の lock |
| `git.py` | git の起動。hook、filter、外部 diff を無効にした引数を必ず付ける |
| `worktree.py` | worktree の観測(agent 設定の digest、tree の digest、変更された path) |
| `check_runner.py` | agent が sandbox の中で実行する固定の検査 program |
| `agents.py` | agent の turn を実行する port |
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

## 記録

状態の置き場所(`DEV_SKILLS_HOME` の下の `loop-engine/`)に、単位ごとの directory を作る。

| file | 内容 |
|---|---|
| `contract.json` / `admission.json` | 受け付けた契約と、受付時に固定した元の commit・検査の定義 |
| `state.json` | 判断の状態。engine が止まっても、ここから続ける |
| `events.jsonl` | 開始、turn の開始と終了、要求、停止の記録 |
| `checks.json` | 検査 program が読む設定 |
| `result/patch.diff` / `result/stop.json` | 元の commit との差分と、止まり方・事実の成否・指摘 |
| `result/turns/NN-<実行>.json` | 各 turn の返答と実行記録 |

worktree は状態の置き場所とは別の、agent の sandbox が書ける場所(`DEV_SKILLS_WORKTREES`)に作る。単位ごとに、作業用と、元の commit の複製の 2 つを持つ。

この場所はどの agent も書けるので、そこにあるものを信用しない。

- worktree の directory 名には、単位の id に推測できない値を付ける。先に同じ名前の directory を用意されても使わない。
- 作った worktree が、途中に link を挟まず、engine が作った場所そのものにあることを確かめる。
- worktree より上の directory に agent 設定や指示 file があれば、agent を起動せずに止める。
- git は、worktree を作った時点で読んだ git directory を明示して起動する。agent が worktree の `.git` を書き換えても、その先を使わない。
- agent 設定が link の場合は、link の先の内容まで digest に含める。

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
| 要求の受付、要求 file、CLI、engine process の起動 | `tests/test_service.py` |
| 検査 program | `tests/test_check_runner.py` |
| 起動する process が git だけであること、純粋な module の範囲、repository の hook や filter が動かないこと | `tests/test_closed_paths.py` |
| transport への接続 | `tests/test_transport_agents.py` |
| 検証 tool | `tests/test_tools.py` |
