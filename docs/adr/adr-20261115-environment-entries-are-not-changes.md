---
id: adr-20261115-environment-entries-are-not-changes
kind: adr
title: 環境が作業 directory に置く空の読み取り専用 file は、役の変更として数えない
status: accepted
created: '2026-11-15'
decision_makers:
- user
tags: []
owners: []
relations:
- {type: references, target: adr-20261114-loop-engine-host-boundary}
- {type: references, target: adr-20261114-checks-judged-from-execution-record}
source_paths: []
summary: worktree を作った commit にも元の commit にもない名前で、worktree の最上位にある、空で書き込めない通常 file
  は、候補・変更 path・範囲・書かない役の比較・patch.diff・検査の tree digest に含めない。engine は sandbox の path
  を知らず、worktree の中を変えない。
updated: '2026-11-15'
---

# 環境が作業 directory に置く空の読み取り専用 file は、役の変更として数えない

## 背景

利用者の agent は、共通の sandbox 方針の下で動く。方針が守る path が作業 directory にないとき、agent の host がその path を作業 directory に作る。2026-11-15 の実測では、Codex(`codex app-server` で `thread/start` だけを送り、turn を始めない)で、多くの実行で thread の cwd に次が現れ、unsubscribe でも app-server の正常終了でも消えなかった。

- 空の directory: `.agents`、`.aws`、`.codex`
- 空の file: `.pi`、`AGENTS.md`、`AGENTS.override.md`

host から見ると、これらの file は通常 file、大きさ 0、mode `0444`、所有者は利用者である。agent の sandbox の中から見ると、同じ path は大きさ 0、mode `0600` の file が読み取り専用の mount にあり、directory は空の `dr-xr-xr-x` の tmpfs である。mode は 2 つの見え方で違う。一致するのは、通常 file であること、大きさ 0 であること、見ている process から書き込めないことである。

engine はこれらを役の変更として数えていた。実機の記録(2026-11-15)では、次が起きた。

- 書かない役の turn が `a-turn-that-must-not-write-changed-the-worktree` と判定される。
- 書く役の変更が範囲外と判定され、`patch.diff` に 3 つの空 file(`new file mode 100644 index 0000000..e69de29`)が入る。

同じ記録で検査の事実が成り立たなかったのは、これらの entry のためではない。agent が実行した検査 program の path が agent の sandbox から見えず、どの実行も状態の行を出さずに失敗していた(`can't open file '.../loop_engine/check_runner.py'`)。ただし、entry があると、検査 program が sandbox の中で計算する tree の digest と engine の digest は、sandbox の中で entry が読めない場合に食い違う。

## 決定案

ユーザーの判断(2026-11-15):

> 「環境が作業 directory に entry を置くのは普通のことで、それを役の変更として数える engine の側がおかしい」

1. **環境の entry を次のとおり定める。** worktree の最上位の directory にある名前で、次をすべて満たすもの。
   - 元の commit の最上位にも、worktree を作った commit の最上位にもない名前である。
   - 通常 file である(symbolic link ではない)。
   - 大きさが 0 である。
   - 判定する process から書き込めない。
2. **環境の entry は、次のどれにも含めない。** 候補の tree とその digest、変更された path、範囲内の判定、書かない役の turn の前後の比較、`patch.diff`、検査の状態の行が結び付く tree の digest。
3. **それ以外はすべて今までどおり数える。** 書き込める空の file、空でない読み取り専用の file、どちらかの commit にある名前の file(切り詰めて読み取り専用にしたものを含む)、symbolic link、最上位より下の file、元の commit にある path へのあらゆる変更。空の directory は、git も tree の digest も元から数えない。
4. **判定は 1 か所に定義し、engine と検査 program が同じ入力で評価する。** 2 つの commit の最上位の名前は、engine が単位の開始時に git から読み、単位の記録と検査 program の設定(`checks.json` の `committed`)に書く。検査 program は worktree の横の元の commit の複製を読まない。
5. **engine は sandbox の path も、方針が守る名前も知らない。** worktree の中のものを削除も変更もしない。

## 比較した案

- **環境に entry を作らせない:** ユーザーは、環境が作業 directory に entry を置くことを普通のことと判断した。
- **方針が守る名前の一覧を engine に持つ:** 環境の方針の写しを engine に置くことになり、方針が変わるたびに engine を変える必要がある。
- **agent の session が始まった後に基準を取り直す:** entry は session の途中や後の session でも現れうる。基準を取り直すと、その時点までの役の書き込みも基準に入り、隠れる。

## 結果

- 環境の entry だけがある worktree で、書かない役は書いていないと判定され、書く役の `patch.diff` と変更された path に entry は入らない。
- 検査 program と engine の digest は、entry の mode や読めるかどうかに関係なく一致する。
- entry が後に中身を持つか書き込めるようになると、その時点から変更として数える。
- この規則が通すのは、どちらの commit にもない名前で、worktree の最上位に、空で書き込めない file を置くことだけである。役が自分でそうした file を置いても数えない。そうした file が存在することに意味を持たせる道具(最上位の空の `pytest.ini`、`conftest.py`、`__init__.py` など)では、検査は file がある tree で実行され、`patch.diff` には file が入らない。
- 最上位より下に環境が entry を置く場合は、今までどおり変更として数える。
- engine を root で動かすと、読み取り専用の file も書き込めると判定され、entry は数えられる。

## 確認方法

`loop-engine/tests/test_environment_entries.py` が、実際の git repository と台本の agent で次を確かめる。

- 書かない役の turn に空の読み取り専用 file が現れても書いたと判定されず、書き込める空の file では判定される。
- 書く役の patch に entry は入らず、役が作った書き込める空の file は入る。
- 6 つの entry がある worktree で、書かない役と書く役の単位が `done` に達し、patch は書く役の変更だけを持つ。範囲内の事実と検査の事実が成り立つ。
- 切り詰めて読み取り専用にした tracked file、空でなくなった entry、書き込めるようになった entry、worktree を作った commit だけにある名前は数える。
- 空の directory はどの digest も変えず、空の `.pi` は agent 設定の digest を変えない。
- entry の mode が `0000` のときと、書き込めない `0600` として見えるとき(書き込み可否を置き換えて模擬)に、検査 program と engine の digest が一致する。実際の読み取り専用 mount での確認は、実機の実行で行う。


{% transition from="proposed" to="accepted" date="2026-11-15" %}
ユーザーが 2026-11-15 に承認した。判断は「環境が作業 directory に entry を置くのは普通のことで、それを役の変更として数える engine の側がおかしい」。決定文 1〜5(最上位に限ること、2 つの commit で判定すること、数えなかった entry の名前を単位の記録に残すことを含めて提示)への返答は「承認する」。
{% /transition %}
