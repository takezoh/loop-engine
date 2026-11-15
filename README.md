# Loop Engine

委任された 1 単位の作業を、契約にある事実がすべて成立するまで host 上で進める engine。作る・機械検査する・独立に評価する、を agent に行わせ、engine 自身は判断の規則と記録だけを持つ。

- engine が起動するのは git と、対応表にある agent の command だけである。project の command は実行しない。
- sandbox を持たず、agent に sandbox や承認の指定を渡さない。project code は agent が自分の sandbox の中で実行する。agent の承認の要求は明示的に拒否し、turn は続ける。
- 環境が作業 directory の最上位に置く空の読み取り専用 file(どちらの commit にもない名前)は、役の変更として数えない。外した名前は turn ごとと `stop.json` に記録する。worktree の中のものは削除も変更もしない。
- agent に実行させる検査 program は、engine が状態の置き場所に複製したものである。範囲内の判定は git の記録した元の commit と比べ、読めないものがある worktree では判定しない。
- 同じ実行が再び走るときは、その実行の agent session を続け、新しいことだけを送る。
- Profile、skill、判断知識を知らない。受け取るのは契約(成立させる事実と、それを成立させられる実行)だけである。

決定の理由は [docs/adr/](docs/adr/)、現在の構造は [docs/architecture.md](docs/architecture.md) にある。

## 配置

| path | 内容 |
|---|---|
| `src/loop_engine/` | engine 本体。`facts.py` と `prompts.py` は値だけを扱う |
| `src/loop_engine/protocol/` | agent に渡す定型文(書く役、評価役、報告役の、session の最初のメッセージと続きのメッセージ) |
| `tests/` | 実際の git repository と engine process を使うテスト。agent は台本で動く偽物 |
| `tools/` | 記録の集計と、実際の agent を使う scenario の実行 |
| `docs/` | 決定(ADR)と構造。dev-docs の独立した docs root |

agent の起動は `packages/cross-host-transport` に依存する。この依存を持つのは `src/loop_engine/composition.py` だけである。

## 使い方

```bash
# 契約を渡して、止まるまで待つ。止まり方ごとの終了コードを返す
runtime/python -I loop-engine/src/loop_engine/__main__.py run <contract.json>

# 照会、回答、やり直し、取消
runtime/python -I loop-engine/src/loop_engine/__main__.py status <unit_id>
runtime/python -I loop-engine/src/loop_engine/__main__.py answer <unit_id> <question_id> <answer-file>
runtime/python -I loop-engine/src/loop_engine/__main__.py retry <unit_id>
runtime/python -I loop-engine/src/loop_engine/__main__.py cancel <unit_id>
```

engine が agent を起動するので、agent の sandbox の中からは実行できない。

| 環境変数 | 意味 | 既定 |
|---|---|---|
| `DEV_SKILLS_HOME` | engine の状態の置き場所(この下の `loop-engine/`)。agent の sandbox が読めて書けない場所であること。検査 program と元の commit の複製もここに置く | `~/.dev-skills` |
| `DEV_SKILLS_WORKTREES` | 単位の worktree の置き場所。agent の sandbox が書ける場所であること | `~/.cache/dev-skills/worktrees` |

## 検証

```bash
runtime/python -m unittest discover -s loop-engine/tests          # 通常テスト
runtime/python loop-engine/tools/unit_report.py                   # 記録済みの単位の集計
runtime/python loop-engine/tools/scenarios.py run loop-engine/tools/scenarios/median-fix   # 実際の agent を起動する
```

`scenarios.py run` は実際の agent を起動し、利用枠を使う。[tools/README.md](tools/README.md) を参照する。
