# Loop Engine の検証 tool

engine、定型文、判定の規則を変えたときに、変更の前後を比べるための tool。

## `unit_report.py`: 記録の集計

```bash
runtime/python loop-engine/tools/unit_report.py [--home DIR] [--json] [unit_id ...]
```

engine の記録だけを読む。単位ごとに、止まり方、差し戻しの回数、実行ごとの turn 数と時間、各 turn が新しい session か続けた session か、各 turn の実行記録の内訳、拒否された承認の要求の回数、各 turn の判定で外した環境の entry、成立した事実、残った指摘、worktree にあって patch に入っていない環境の entry を示す。合計には、やり直しになった turn、未完了の turn、数えられなかった結果(理由つき)、session を続けた turn、続けられずに置き換えた session、拒否された要求の件数が出る。

読み方の目安:

- 「数えられなかった結果」が多い実行は、定型文が求める返答の形を agent が守れていない。理由の文を見て定型文を直す。
- 差し戻しが多い単位は、指摘の内容を見る。同じ条件の指摘が続くなら、作る役の指示か受入条件の書き方を疑う。
- 実行記録に `read` も `command` もない評価は、調べずに返している。

## `scenarios.py`: 実際の agent での確認

```bash
runtime/python loop-engine/tools/scenarios.py prepare <scenario> <work-dir>   # repository と契約を作るだけ
runtime/python loop-engine/tools/scenarios.py run <scenario>... [--home DIR] [--work DIR] [--json]
```

`run` は engine と agent を起動する。agent の利用枠を使い、数分かかるので、実行前に対象と費用を利用者に示して承認を得る。期待と違う止まり方をした scenario があれば、終了コード 1 を返す。

engine は検査 program と元の commit の複製を `--home` の下の公開の置き場所(`loop-engine/`)に置き、agent はそこから検査を実行する。記録は `--home` の下の `records/loop-engine/` にある。検査の事実が意味を持つのは、agent の sandbox が公開の置き場所を読めて書けない場合だけである。実際の agent での実行には、そうした場所(既定の `~/.dev-skills`)を使う。sandbox が読めない場所では検査の状態の行が出ず、書ける場所では元の commit での検査を agent が変えられる。

scenario は次の directory である。

| 中身 | 意味 |
|---|---|
| `repo/` | 元の commit の file。検査を使うなら `.dev-skills/checks.json` を含める |
| `change/` | 省略可。元の commit の上に commit される、評価の対象になる既存の変更 |
| `contract.json` | 契約。`{repository}`、`{base}`、`{unit_id}` は実行時に埋まる |
| `expect.json` | 期待。`stop_reason`、指摘が名指しすべき条件(`unmet`)、turn 数の上限(`max_turns`) |

同梱の scenario:

| scenario | 確かめること |
|---|---|
| `median-fix` | 実装 → 評価 2 つが完了する。検査は「候補で通る」と「元の code では落ちる」 |
| `median-missing-empty-check` | 条件を 1 つ満たさない既存の変更を、3 つの評価役が該当の条件を名指しして止める |

欠陥を仕込んだ scenario を足すときは、`change/` に欠陥のある変更を置き、`expect.json` の `unmet` に見つけるべき条件を書く。
