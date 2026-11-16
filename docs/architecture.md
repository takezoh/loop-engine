# Loop Engine の構造

module、契約の field、記録の file、command の詳細を書く。全体像と境界は [ARCHITECTURE.md](../ARCHITECTURE.md)、決定の理由と比較した案は [adr/](adr/) にある。

## 責務

| module | 責務 |
|---|---|
| `facts.py` | 契約の受付検査、次の実行の選択、事実の成立と無効化、検査と評価の判定。値だけを扱う |
| `prompts.py` | 実行に渡す指示文(session の最初のメッセージと続きのメッセージ)の組み立て。値だけを扱う。書く役に変えさせない agent 設定の名前は、`worktree.py` が観測する一覧を値として受け取る |
| `unit.py` | 1 単位を、保存された状態から次に止まるところまで進める |
| `service.py` | engine process。単位ごとに thread を持ち、要求を受け付ける |
| `store.py` | 単位の記録、agent が読むものの公開(検査 program、単位の `checks.json`)、要求と応答の file、process の lock。lock の取得は、取れた・別の process が持つ・取れない(原因を示す `LockFailed`)の 3 つを区別し、取れないことを別の engine が動いているとは扱わない |
| `git.py` | git の起動。hook、filter、外部 diff を無効にした引数を必ず付ける |
| `worktree.py` | worktree の観測(agent 設定の digest、tree の digest、git の記録と比べた変更された path) |
| `check_runner.py` | agent が sandbox の中で実行する固定の検査 program。tree の読み方(読めないものがあれば失敗する)、tree の digest、環境の entry の判定を持ち、`worktree.py` も同じものを使う |
| `agents.py` | agent の turn を実行する port。session は中身を解釈しない token で表す |
| `composition.py` | port を provider transport に接続する。transport に触れる唯一の module |
| `client.py` / `cli.py` | 入口。要求を渡し、記録を読む。進行と判定の処理を持たない |
| `platform_facts.py` | engine が動く machine の platform の事実(下の表)。`os.name` で POSIX と Windows を選ぶ唯一の module |

platform の事実は `Platform` の値で、`CURRENT` が engine の動く machine のものである。`composition.py` が `Engine` に渡し、`Engine` は path の形を `facts.validate` と `facts.checks_config` に、quote を `Unit` に渡す。`git.py`、`store.py`、`client.py` は `CURRENT` の値を使う。`place_new` と `lock` は platform で異なる作用の adapter で、呼び出す OS の関数を引数で受け取る。Windows の adapter には偽の Windows の関数に対して、engine が動く machine の adapter と同じ契約テストを当てる。

| 事実 | POSIX | Windows | 使う module |
|---|---|---|---|
| `path_style`(絶対 path の形) | `/a/b` | `C:\a\b` か `\\server\share\a`(`\` だけで区切る) | `facts.py` |
| `null_device` | `/dev/null` | `nul` | `git.py`(hook、attributes、global 設定) |
| `home`(環境に `HOME` がないときに git に渡す値) | `/` | 渡さない | `git.py` |
| `quote`(agent に渡す検査の command line) | POSIX shell の quote | Microsoft C runtime が分け直す形(`subprocess.list2cmdline` と同じ) | `unit.py` |
| `detached`(呼出し元から切り離した engine の起動) | 新しい session | 新しい process group、console なし | `client.py` |
| `place_new`(すでにある file を置き換えない作成) | hard link | rename | `store.py`(検査 program の公開) |
| `lock`(process の lock) | `flock` | `msvcrt.locking` | `store.py` |
| `lock_held`(別の process が lock を持つときの errno。それ以外の errno は lock を取れないこと) | `EAGAIN`、`EWOULDBLOCK` | `EACCES` | `store.py`(`lock_contended`) |

## 契約

契約の構造(事実の種類、実行の規則、参照)は [単位の宣言](../ARCHITECTURE.md#単位の宣言) にある。受付の検査は `facts.py` の `validate` が行い、事実と実行の構造には `check_structure` を使う。`check_structure` は公開の純粋関数で、契約の一部だけを問える。契約を組み立てる側が engine の名前を写さずに使えるよう、`unit` の key(`UNIT_KEYS`、`UNIT_OPTIONAL`)、run が加える項目(`FACT_RUN_KEYS`、`EXECUTION_RUN_KEYS`)、検査の `expect` の値(`CHECK_EXPECT`)と id・名前・commit の文字種(`ID_RE`、`CHECK_NAME_RE`、`COMMIT_RE`)も公開する。検査は次の上限と文字種を加える。

| 対象 | 上限・文字種 |
|---|---|
| `unit_id`、事実・実行・受入条件の `id`、`agent`(下の文字種はどれも値の全体に当て、末尾の改行も受け付けない) | `^[a-z0-9][a-z0-9-]{0,62}$` |
| `model`、`effort` | `null` か `^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,95}$` |
| 検査の名前 | `^[a-z0-9][a-z0-9_-]{0,31}$` |
| `goal`、受入条件の `text`、`instructions` | 空でなく NUL を含まない 200,000 byte 以下の文字列 |
| `criteria`、`scope`、`facts`、`executions`、検査 | 各 32 件以下。`facts` と `executions` は 1 件以上 |
| `unit.base` | 40 桁の commit id |
| `repository`、`scope` の path | `repository` は engine が動く machine の形(`path_style`)で正規化した絶対 path、`scope` は `/` で区切り `.`・`..`・空の要素を持たない相対 path。Windows では各要素が file の名前にできるもの(`\`・`:`・`*?"<>|`・制御文字を含まず、`.` か空白で終わらず、拡張子の有無を問わず大文字小文字を問わず `CON`・`PRN`・`AUX`・`NUL`・`COM1`〜`COM9`・`LPT1`〜`LPT9` でない)に限る。`.dev-skills/checks.json` の `test_paths` と `ignore` も同じ |

契約の key は過不足なく一致しなければならない(事実は `by` ごとの項目だけを持つ)。同じ `unit_id` で異なる契約を渡すと受け付けない。

## agent の session

実行ごとに 1 つの agent session を持つ。

- 実行の最初の turn は新しい session を開始し、役の定型文に指示の全文を入れて送る。port は turn が動いた session の token を返し、engine は実行ごとに `state.json` に保存する。
- 同じ実行が再び走るとき(差し戻し、回答、やり直し)は、その token で session を続け、役の続きの定型文(`protocol/<役>-follow-up.md`)を送る。続きのメッセージは、指摘とやり直しの理由、その session がまだ受け取っていない報告と依頼者の回答、評価役には今の変更 file の一覧、と返答の形式を持つ。指示、目的、受入条件は送り直さない。
- 定型文は、役に渡す報告を、ほかの役がこの作業について見出したことで証拠として量るものとして渡し、その役が自分の turn に受けた規則は読む役も作業も縛らないと示す。書かない役(報告役・評価役)の定型文は、返答の形を決める節の規則がその turn だけの規則で、報告や指摘の対象ではないと示す。役の返答が別の役へ届くのは、書かない役の報告と評価役の指摘だけで、書く役の報告はどの役にも渡さない(`prompts.inputs`)。
- 異なる実行は session を共有しない。評価役は作る役の session を見ない。
- port が「session を続けられない」と返したら、engine はその turn を 1 回だけ、指示の全文で新しい session として起動し直す。その失敗はほかの失敗した turn と同じに扱う。
- token は `state.json` にあるので、engine が再起動しても同じ session を続ける。
- 単位が続けられない止まり方で終わると、engine は保っていた session をすべて閉じる。続けられる止まり方では保つ。engine の停止は、session をすべて解放してから、起動したすべての agent の process を終える。
- agent 設定の検査、worktree より上の検査、取消は、続けた turn にも同じく当てる。事実の判定に使う実行記録は、その turn のものだけである。

transport への接続(`composition.py`)では、token は provider の session の識別子(Codex の thread、ACP と Pi の session)である。transport は turn が終わるたびに session を解放(unload)し、続けるときに識別子で読み込み直す。session を保つとは engine が token を `state.json` に持つことで、閉じるとは token を捨て、transport もその session を続ける対応を捨てることである。provider が保存した会話は消さない。

`composition.py` は transport の `ProviderSessions` を engine と同じ process で使う。

- 実行の最初の turn は provider、model、effort、cwd、prompt を渡して `start` し、同じ実行の後の turn は同じ session に `continue` する。engine の再起動後は、保存した識別子を `recover_binding` で束ねてから `continue` する。
- どちらも `wait_turn` で待ち、`observe` で結果を読み、`wait_release` で session の解放を待ってから turn を返す。terminal view の `provider_thread_id` が session の token になる。
- `continue` が `session_lost` か `recovery_pending` を返すか、session の bind が失敗したら、port は「session を続けられない」と返す。
- `close` は単位が続けられない止まり方で終わったときと、続けられなかった session に使う。取消は `cancel` である。

transport の各操作の意味と driver ごとの wire 手順は transport の文書([subsession の構造](../../docs/architecture/subsession.md))にある。

## 指示文

`prompts.py` が、実行ごとに `protocol/` の定型文から turn のメッセージを組み立てる。定型文は書く役(`writer`)、評価役(`reviewer`)、報告役(`reporter`)の 3 つで、`writes: true` の実行は書く役、`verdict` の事実を成立させる書かない実行は評価役、それ以外は報告役の定型文を使う(`prompts.kind`)。

- 最初のメッセージは、定型文に実行の `instructions` をそのまま入れ、`## Goal` の節に `unit.goal`、受入条件の節に `criteria` を置き、与えられる報告、それまでの依頼者の回答、前回の指摘を加える。評価役には変更 file の一覧と元の commit の場所、書く役には範囲(`scope`)、検査の command、変えてはならない agent 設定の名前を加える。返答の形(報告、条件ごとの判定と指摘、質問)は定型文が決める。
- 続きのメッセージの中身は [agent の session](#agent-の-session) にある。
- engine は `instructions` の中身を解釈しない。呼出し元が役の振る舞いと判断の基準を `instructions` に書き、目的、受入条件、範囲、検査、返答の形は定型文が付ける。

## turn の終わりと worktree

port は、turn の agent session が解放されてから turn を返す。解放されるまでは、agent の process とその sandbox が worktree を使っていることがある。engine が worktree を読む(agent 設定の digest、候補の差分、変更された path、tree の digest)のは、port が turn を返した後だけである。

- 解放が provider の上限時間内に終わらない、または失敗したら、port は完了していない turn を返す(`released: false`、detail は `agent-session-not-released: <理由>`)。engine はその turn では worktree を何も判定せず、`unknown_result` で止まる。session の token は保つ。
- 次の turn の前の検査は、そのとき worktree にあるものすべてに当てる。engine は worktree の中のものを削除も変更もしない。判定から外すのは、次の節の環境の entry だけである。
- engine は turn の後、役が書くかどうかに関わらず worktree の全体を読んでから判定する。読めない directory や file(一覧できない、たどれない、開けない)が 1 つでもあれば、その turn は完了しない(`unknown_result`、detail は `worktree-not-readable: <path>`)。読めないものを tree から外すと、候補、変更された path、`patch.diff`(git も読めないものを黙って外す)が縮むからである。agent 設定の中に読めないものがあれば `unsafe_agent_config` で止まる。検査 program も、読めない tree では digest を `none` とし、その状態の行はどの判定でも合格にならない。

## 環境の entry

agent の host は、作業 directory に空の file や directory を置くことがある。engine はそれらの名前も sandbox の方針も知らず、次の形だけで見分ける。

環境の entry は、worktree の最上位の directory にある名前で、元の commit の最上位にも worktree を作った commit の最上位にもなく、通常 file(link ではない)で、大きさが 0 で、判定する process から書き込めないものである。

- 環境の entry は、候補の tree とその digest、変更された path、範囲内の判定、書かない役の turn の前後の比較、`result/patch.diff`、検査の状態の行の digest、agent 設定の digest に含めない。git の index からも外す。
- 外した名前は記録に残す。各 turn の `turn_finished` の `environment_entries` はその turn の判定で外した名前、`result/stop.json` の `environment_entries` は止まったときに worktree にあり `patch.diff` に入っていない名前である。どちらも止まったとき、または turn の後に worktree から読み、`{"count": 全部の数, "names": [...]}` の形で、名前は最大 32 個、各名前は 1000 文字までに切る。
- それ以外(書き込める空の file、空でない file、どちらかの commit にある名前、link、最上位より下の file)は、すべて変更として数える。entry が中身を持つか書き込めるようになると、その時点から数える。
- 判定は `check_runner.py` の 1 つの関数にあり、engine(`worktree.py`、`git.py` の patch への除外の指定)と検査 program が同じものを使う。2 つの commit の最上位の名前は、engine が単位の開始時に git から読み、`unit_started` の記録と `checks.json` の `committed` に書く。
- 空の directory は、git も tree の digest も agent 設定の digest も数えない。agent 設定の名前(`.claude`、`.codex`、`.pi`、`.agents`、`.mcp.json`)は、directory、file、link、そのほかの種類を問わず digest に含める。外すのは環境の entry だけで、それ以外の空の file も含める。
- engine の停止は、実行中の turn を止め、session を解放してから host の process を終える。process を signal で終えるのは、解放が上限時間内に終わらなかったときの最後の手段である。

## 承認の要求

agent が承認、permission、利用者の入力を求めたら、transport が明示的に拒否して答え、turn は続く。Codex には承認の指定を送らない。3 つの driver の答え方は [subsession の構造](../../docs/architecture/subsession.md)にある。

拒否した要求は、種類(`command`、`file_change`、`permissions`、`input`)と短い説明で turn の実行記録(`actions.refused`)に残る。engine は turn の回数を実行ごとに足し、`status.json` と `result/stop.json` の `refused` に出す。拒否で単位を止めない。拒否されて実行できなかった検査は合格の行を残さないので、その事実は成立しない。

## 記録

記録の置き場所(`DEV_SKILLS_HOME` の下の `records/loop-engine/`)に、単位ごとの directory を作る。agent はこの場所を読まない。読めなくするのは環境の sandbox 設定である。

| file | 内容 |
|---|---|
| `contract.json` / `admission.json` | 受け付けた契約と、受付時に固定した元の commit・検査の定義 |
| `state.json` | 判断の状態(事実、評価、否定の指摘の数 `refuted`、依頼者の回答 `answers`、評価役が読んだ file `examined`)と、実行ごとの session の token と受け取った回答の数、拒否された要求の回数。engine が止まっても、ここから続ける |
| `events.jsonl` | 開始、turn の開始(新しい session か続けた session か)と終了、続けられなかった session、要求、session を閉じたこと、停止の記録 |
| `result/patch.diff` / `result/stop.json` | 元の commit との差分と、止まり方・事実の成否・指摘・実行ごとの拒否の回数・worktree にあって差分に入っていない環境の entry |
| `result/turns/NN-<実行>.json` | 各 turn の返答、実行記録、session の token、拒否の件数 |

記録の置き場所には、ほかに要求と応答(`inbox/`、`outbox/`)、`engine.lock`、transport の状態(`agents/`)がある。

agent が読むものは、公開の置き場所(`DEV_SKILLS_HOME` の下の `loop-engine/`)に置く。

| file | 内容 |
|---|---|
| `check/<sha256>/check_runner.py` | engine が公開した検査 program(次の「検査」の節) |
| `units/<unit_id>/checks.json` | 検査 program が読む設定(worktree と元の commit の複製の path、検査の定義、無視する名前、2 つの commit の最上位の名前) |
| `units/<unit_id>/base-<推測できない値>/` | 元の commit の複製。元の commit での検査はこの複製に候補の test を入れて実行し、評価役は元の版をここで読む。単位が続けられない止まり方で終わると消す |

役に渡す path(検査の command、元の commit の場所)は公開の置き場所だけを指す。2 つの置き場所はどちらの path も他方の前方一致にならないので、一方を path の前方一致で許可または拒否する設定は他方に当たらない。

worktree は 2 つの置き場所とは別の、agent の sandbox が書ける場所(`DEV_SKILLS_WORKTREES`)に作る。元の commit の複製は worktree の横に置かない。

単位が続けられない止まり方(`waiting_for_answer`、`unknown_result`、`invalid_result` 以外)で終わると、engine は worktree と元の commit の複製を消す。変更は `result/patch.diff` に残る。

この場所はどの agent も書けるので、そこにあるものを信用しない。

- worktree の directory 名には、単位の id に推測できない値を付ける。先に同じ名前の directory を用意されても使わない。
- 作った worktree が、途中に link を挟まず、engine が作った場所そのものにあることを確かめる。
- worktree より上の directory には、engine が作る directory(repository ごとの directory と、単位の worktree)しか置かない。それ以外のものがあれば、agent を起動せずに止める。turn の前と後の両方で確かめる。
- git は、worktree を作った時点で読んだ git directory を明示して起動する。agent が worktree の `.git` を書き換えても、その先を使わない。
- agent 設定が link の場合は、link の先の内容まで digest に含める。読む量には上限があり、超える設定の単位は進めない。

確認と agent の起動のあいだに別の agent が file を置く余地は残る。この場所を agent から書けなくする手段は、engine ではなく環境の sandbox 設定が持つ。

## 差し戻しと回答

- 否定された事実(評価、検査、範囲)は、対象を作った実行へ指摘を渡して戻す。戻すのは、指摘の数が同じ事実の前回の否定より少ないときだけである。数えるのは、評価では engine が計算した止める指摘、検査では落ちた検査、範囲では範囲外の path である。最初の否定は戻す。少なくなければ `stagnation` で止まり、`stop.json` の `stop` に事実、指摘、`detail`(今回と前回の数)を残す。事実ごとの前回の数は `state.json` の `refuted` にある。
- 指摘を受けた作る役が候補を変えずに turn を終える、または報告役が同じ報告を返すと、`stagnation` で止まる(detail は `candidate unchanged` か `report unchanged`)。その指摘の事実を判定した後に回答があったときは止めず、事実を判定し直す。
- 回答は `state.json` の `answers`(`id`、`question`、`answer`、`asked_by`)に順に残る。session は受け取った回答の数を持ち、続きのメッセージはまだ受け取っていない回答だけを運ぶ。評価の事実の根拠は回答の digest を `@answers` の key に持ち、回答が増えるとその評価は成立しなくなる。報告、検査、範囲の根拠には回答を含めない。
- 差し戻した回数は `status.json` の `round` と `stop.json` の `rounds` に出す。止める条件には使わない。

## 評価の返答の受付

- 評価役の返答を数えないとき、engine は見つけた理由をすべて `; ` でつないで、続きのメッセージの指摘として返す。返答の形が読めない(最後の object がない、`criteria` が list でないなど)ときは、その理由だけを返す。
- 変更 file を読んだ跡は、その turn の実行記録に加えて、同じ実行が同じ根拠で前の turn に読んだ file も数える(`state.json` の `examined`)。根拠が変わったら前の turn の分は数えない。読んだ跡のない file は、理由の中にすべて(32 個までと残りの数)挙げる。
- 根拠の command は、その turn の実行記録にあるものだけを数える。file の引用は、今の file にあれば turn を問わない。

## 止まり方と終了コード

| 止まり方 | 終了コード | 続け方 |
|---|---|---|
| `done` | 0 | |
| `waiting_for_answer` | 10 | `answer` |
| `stagnation` | 12 | |
| `unknown_result` | 13 | `retry` |
| `cancelled` | 14 | |
| `unsafe_agent_config` | 15 | |
| `rejected` | 16 | |
| `invalid_result` | 17 | `retry` |
| `stuck` | 18 | |

要求が受け付けられなかった場合は 2、engine を起動できなかった場合は 3 を返す。記録の置き場所の lock を取れない(権限、directory を作れない、lock のない filesystem)場合も 3 で、原因を stderr に示す。

## 検査

project は `.dev-skills/checks.json` に検査を定義する。engine は元の commit にあるこの file だけを読む。

```json
{"checks": {"unit": {"argv": ["python3", "-m", "unittest"], "test_paths": ["tests"]}}, "ignore": ["__pycache__"]}
```

検査が書き出す cache(`__pycache__` など)は `ignore` に書く。書かないと、検査の前後で tree の digest が変わり、検査が「現在の tree で実行されていない」と判定される。

engine は起動するたびに、自分の `check_runner.py` を公開の置き場所の `check/<内容の sha256>/check_runner.py` に複製する。一時的な名前で同じ directory に書いてから、すでにある file を置き換えない方法(`place_new`)で最終の名前にするので、同時に起動した engine も、書きかけの file も、すでにある file を置き換えない。その path に内容の sha256 が directory 名と一致しない file(link を含む)があれば、engine は起動しない。起動した後も、command を渡す前に毎回内容を確かめ、一致しなければ単位を `unknown_result` で止め、agent を起動しない。

agent に渡す command は `<engine の Python> -I <公開の置き場所>/check/<sha256>/check_runner.py <公開の置き場所>/units/<unit_id>/checks.json <検査の名前> [--base]` で、path は platform の `quote` で quote する。Python は engine を動かしている interpreter(`sys.executable`)である。engine は、公開の置き場所と engine の Python を agent の sandbox が読めて、公開の置き場所には書けないこと、記録の置き場所を読めないことを前提とし、それを確かめない。

agent は engine が指定した command を実行する。検査 program は結果の最後の行に、検査の名前、対象(候補か元の commit か)、候補の tree の digest、終了コードを出す。元の commit での検査では、複製した元の commit の tree の digest(`base=`)も出す。engine は実行記録のこの行だけで合否を判定する。tree の digest は、engine と検査 program が同じ関数で計算し、環境の entry を含めない。

## 元の commit

判定が比べる元の commit は、agent が書ける tree から読まない。

- 変更された path(範囲内の判定、評価役に渡す変更 file の一覧、評価役が変更 file を読んだかの割合)は、worktree の file と、worktree を作った時点で読んだ git directory にある元の commit の記録(`git ls-tree`)を、git の blob の id で比べる。書かない役の turn の前後の比較と `patch.diff` は、元の commit に対する git の差分である。
- 元の commit での検査(`fails_on_base`)は agent が sandbox の中で実行するので、engine が保証できることは限られる。engine が保証するのは、検査 program が engine の置いた複製を読むこと、状態の行の `base=` が単位の開始時に engine が計算した複製の digest と一致しない実行を合格にしないこと(指摘は `not run on the base revision the engine placed`)、複製を作れない・検査を起動できない実行を合格にしないこと、`digest=` が実行の始めの候補の tree と一致することである。状態の行が示すのは、検査 program が実行の始めにその候補の tree を読み、その時点で engine の置いた内容の複製を写したことまでである。写した後の一時的な tree、実行中の test の振る舞い、検査 program の出力の後に同じ出力へ書く process は、agent の sandbox の中にあり、engine は確かめられない。

## 検証の対応

| 確かめること | テスト |
|---|---|
| 判断の規則、判定の規則、契約の受付、構造だけの検査、指摘が減る間だけ戻すこと、回答と評価の根拠、前の turn に読んだ file を数えること | `tests/test_facts.py` |
| 実際の repository と engine での流れ、止まり方、再開、取消、役に渡す path が公開の置き場所だけを指すこと | `tests/test_unit_flow.py` |
| session の継続、続きのメッセージ、続けられない session、session を閉じること、解放されなかった turn、拒否の回数 | `tests/test_sessions.py` |
| 要求の受付、要求 file、CLI、engine process の起動 | `tests/test_service.py` |
| 検査 program | `tests/test_check_runner.py` |
| 検査 program の公開(内容の digest の directory、同時に起動した engine、書き換えられた複製を使わないこと)、役に渡す command が公開した複製を指すこと | `tests/test_check_program.py` |
| 環境の entry を数えないこと、それ以外を数えること、digest の一致、空の directory と agent 設定の digest、外した名前の記録 | `tests/test_environment_entries.py` |
| 元の commit を git の記録と engine の複製から読むこと、`fails_on_base` の判定、読めない tree で判定が失敗すること、agent 設定の名前の種類を問わないこと | `tests/test_judged_trees.py` |
| 起動する process が git だけであること、純粋な module の範囲、repository の hook や filter が動かないこと | `tests/test_closed_paths.py` |
| Windows の事実を値で渡した判断(絶対 path、repository の中の path、検査の command の quote)、platform を見分けるのが `platform_facts.py` だけであること、`place_new` と `lock` の adapter の契約、識別子が末尾の改行を受け付けないこと、評価役の返答の受け付けない理由 | `tests/test_platform_facts.py`、`tests/test_facts.py` |
| agent の port の契約(transport への接続と台本の偽物の両方に当てる)、session の解放後に turn を返すこと、停止での解放と process の終了の順、解放後の worktree の判定 | `tests/test_transport_agents.py` |
| 検証 tool | `tests/test_tools.py` |
