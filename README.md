# Loop Engine

委任された 1 単位の作業を、契約にある事実がすべて成立するまで host 上で進める engine。作る・機械検査する・独立に評価する、を agent に行わせ、engine 自身は判断の規則と記録だけを持つ。

engine が起動するのは git と agent の command だけで、sandbox を持たず、project の command を実行しない。受け取るのは契約(成立させる事実と、それを成立させられる実行)だけである。

全体像、境界と設計原則は [ARCHITECTURE.md](ARCHITECTURE.md)、module・契約の field・記録の詳細は [docs/architecture.md](docs/architecture.md)、決定の理由は [docs/adr/](docs/adr/) にある。

この repository では dev-skills が engine を使う。dev-skills が契約をどう組み立て、単位の結果をどう扱うかは [dev-skills の単位の入口](../docs/architecture/loop-kernel.md)にある。

## 配置

| path | 内容 |
|---|---|
| `src/loop_engine/` | engine 本体。`facts.py` と `prompts.py` は値だけを扱い、platform の違い(Linux と native Windows)は `platform_facts.py` だけが見分ける |
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
