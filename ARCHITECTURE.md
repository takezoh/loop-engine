# Loop Engine の技術アーキテクチャ

Loop Engine は、委任された 1 単位の作業を、契約にある必須の事実がすべて成立するか、止まる理由が生じるまで host 上で進める。作る・機械検査する・独立に評価する作業は agent が自分の sandbox の中で行い、engine は次の実行の選択、事実の判定、記録だけを持つ。engine が受け取るのは契約(成立させる事実と、それを成立させられる実行)だけで、契約を作った側の概念(役の名前、判断知識、skill)を知らない。

本文書は全体像と核心の境界を示す。module、契約の field、記録の file、終了コードの詳細は [docs/architecture.md](docs/architecture.md)、決定の理由と比較した案は [docs/adr/](docs/adr/) にある。

## 境界

```mermaid
flowchart LR
  Entry["呼出し元の入口<br/>MCP server・CLI・CI<br/>client.py / cli.py"]
  subgraph HostG["host"]
    Engine["engine process<br/>service.py・unit.py"]
    Git["git<br/>hook・filter・外部 diff を無効にして起動"]
    Home[("状態の置き場所<br/>DEV_SKILLS_HOME/loop-engine")]
    Trees[("worktree の置き場所<br/>DEV_SKILLS_WORKTREES")]
    Pkg[("導入済みの package<br/>engine と transport の code・定型文")]
    AgentHost["agent の host process<br/>codex・claude・pi<br/>利用者の設定のまま"]
    subgraph SandboxG["環境の sandbox(engine の外)"]
      Work["agent が実行する command<br/>project の code と検査"]
    end
    Never["engine が起動しないもの<br/>project の command・worktree の code<br/>sandbox の作成と変更"]
  end
  Entry -->|"要求 file: 契約・回答・やり直し・取消"| Home
  Home -->|"status.json・stop.json・patch.diff"| Entry
  Entry -.->|"engine がなければ固定の command で起動"| Engine
  Engine -->|"要求を取り、記録を書く"| Home
  Pkg -->|"code と定型文"| Engine
  Engine -->|"固定の subcommand"| Git
  Git -->|"worktree の作成・元の commit の記録・差分"| Trees
  Engine -->|"transport で起動・turn・解放・取消"| AgentHost
  AgentHost -->|"返答・実行記録・拒否した要求"| Engine
  AgentHost -->|"command を sandbox の中で実行"| Work
  Work -->|"読み書き"| Trees
  Work -->|"検査 program と元の commit の複製を読む"| Home
  Engine -->|"turn の後に読む"| Trees
  Engine -.->|"起動しない"| Never
```

engine が起動する process は git と、transport の対応表にある agent の起動 command だけであり、project の code が動くのは agent の sandbox の中だけである。

- 入口は要求を file で渡し、engine の記録を読むだけで、進行・判定・記録の処理を持たない。engine process が動いていなければ、入口が固定の command(`python -I` で engine の main)で起動する。
- engine は 1 つの状態の置き場所につき 1 つの process で、単位ごとに thread を持つ。止まっても、次の engine が記録から続ける。
- agent の起動は `composition.py` だけが transport に接続する。engine の core は `agents.py` の port(turn の実行、session の終了、全体の停止)だけに依存する。

## 単位の宣言

```mermaid
flowchart LR
  subgraph UnitG["unit"]
    UG["goal・criteria・scope<br/>limits.rounds・base(省略可)"]
  end
  subgraph FactsG["facts(id・required・by)"]
    FG["given<br/>初めから成立"]
    FR["report<br/>1 つの実行が成立させる"]
    FC["checks<br/>検査の name と expect"]
    FV["verdict<br/>block_at"]
    FT["git<br/>engine が成立させる"]
  end
  subgraph ExecsG["executions(id・agent・model・effort・instructions)"]
    EW["作る実行<br/>writes: true"]
    EN["報告する実行・評価する実行<br/>writes: false"]
  end
  FC -->|"subject"| FR
  FT -->|"subject"| FR
  FV -->|"subject"| FR
  FV -->|"subject"| FG
  FR -->|"reads"| EW
  FR -->|"reads"| EN
  EW -->|"establishes"| FR
  EW -->|"establishes: 対象を書いた実行"| FC
  EN -->|"establishes"| FR
  EN -->|"establishes"| FV
  EN -.->|"independent_of: agent と model の組が違う"| EW
  UG -->|"criteria: 条件ごとに判定"| FV
  UG -->|"scope: 範囲の判定"| FT
```

契約に実行の順番はなく、事実と実行の参照(`reads`、`establishes`、`subject`、`independent_of`)だけがある。この構造は engine が定義し(`facts.py` の `validate`)、受付時に検査して、合わない契約を受け付けない。新しい種類の単位は、この節の規則を満たす契約を書けば engine で動く。

### 契約の形

```
unit_id, repository(repository の最上位の絶対 path)
unit:        goal, criteria[{id, text}], scope[path], limits{rounds}
             任意: base(commit id)、allow_same_model(bool)、examine_min(0..1)
facts:       {id, required, by} と by ごとの項目
executions:  {id, agent, model, effort, instructions, reads, establishes, writes, independent_of}
```

- `unit.goal` と `criteria` は全実行の指示に入る。評価役は `criteria` の条件ごとに判定する。`scope` は `git` の事実が判定する変更してよい path で、作る役にも示される。`limits.rounds` は差し戻しの上限である。
- `unit.base` を指定すると、その commit から repository の現在の commit までの既存の変更を対象にする(現在の commit は `base` の子孫でなければならない)。省略すると現在の commit が元の commit になる。
- `allow_same_model` は `independent_of` の検査を緩める。`examine_min` は、評価役の実行記録が読んだことを示さなければならない変更 file の割合で、既定は 1 である。

### 事実の種類

| `by` | 項目 | 成立させる実行 | 成立のさせ方 |
|---|---|---|---|
| `given` | なし | 持たない | 初めから成立する。内容は今の tree の digest で、これを対象にした事実は tree が変わると未成立に戻る |
| `report` | なし | ちょうど 1 つ | その実行の turn が完了したとき |
| `checks` | `subject`、`checks[{name, expect}]` | `subject` を成立させる実行と同じ集合で、すべて `writes: true` | その実行の実行記録にある検査の状態の行 |
| `verdict` | `subject`、`block_at`(`minor`・`major`・`blocker`) | 1 つ以上(評価役) | 全評価役の判定から engine が計算 |
| `git` | `subject` | 持たない | `subject` が成立した後、変更 path を `scope` と比べる |

- `checks` の `expect` は `pass`(候補で通る)か `fails_on_base`(元の commit に候補の test だけを入れて落ちる)である。名前は元の commit の `.dev-skills/checks.json` に定義がなければならず、`fails_on_base` の検査は `test_paths` を持たなければならない。
- `required: false` の事実は完了の条件に入らず、ほかの事実の対象や読む事実にはなれる。

### 実行の規則

- `agent` は導入済みの対応表にある名前、`model` と `effort` は名前か `null` である。engine はこれらの値で agent を起動し、解釈しない。
- `establishes` は 1 つ以上で、`reads` と重ならない。参照する事実と実行はすべて契約にある。
- `writes: true` の実行だけが worktree を変えてよい。書かない実行の turn が差分を変えれば、その turn は完了しない。
- `independent_of` に挙げた実行とは、`unit.allow_same_model` が真でない限り、agent と model の組が違わなければならない。
- 事実の依存(対象と、その事実を成立させる実行が読む事実)は循環してはならない。`checks` の事実の依存は対象だけである。

### 例

```json
{"facts": [
  {"id": "a", "required": true, "by": "report"},
  {"id": "b", "required": true, "by": "verdict", "subject": "a", "block_at": "major"}],
 "executions": [
  {"id": "x", "agent": "codex", "model": null, "effort": null, "instructions": "…",
   "reads": [], "establishes": ["a"], "writes": false, "independent_of": []},
  {"id": "y", "agent": "claude", "model": null, "effort": null, "instructions": "…",
   "reads": ["a"], "establishes": ["b"], "writes": false, "independent_of": ["x"]}]}
```

x が報告 a を書き、x と独立な y が a を評価して b を成立させる。b が否定されると、指摘は a を作った x へ戻り、a は外される。x の次の turn で a が再び成立すると、b は新しい a を根拠に判定し直される(指摘の後も報告が変わらなければ `stagnation`)。大きさと文字種の上限は [docs/architecture.md#契約](docs/architecture.md#契約) にある。

## 次の実行の選択と止まり方

```mermaid
flowchart TD
  Start(["記録した状態"]) --> Cancel{"取消の要求"}
  Cancel -->|"ある"| Cancelled["cancelled"]
  Cancel -->|"ない"| Judge["engine が判定する事実を判定<br/>git: 対象が成立したら範囲を判定<br/>verdict: 全評価役の判定が揃ったら合否を計算"]
  Judge --> Refuted{"否定された事実"}
  Refuted -->|"ない"| AllHeld{"必須の事実がすべて成立"}
  Refuted -->|"ある"| Maker{"対象を作る実行"}
  Maker -->|"ない"| Rejected["rejected<br/>指摘が結果"]
  Maker -->|"ある"| Same{"前回と同じ指摘"}
  Same -->|"はい"| Stagnation["stagnation"]
  Same -->|"いいえ"| Rounds{"差し戻しが rounds に達した"}
  Rounds -->|"はい"| Exhausted["rounds_exhausted"]
  Rounds -->|"いいえ"| Back["対象を作った実行へ指摘を渡し<br/>その実行が成立させた事実を外す"]
  Back --> AllHeld
  AllHeld -->|"はい"| Done["done"]
  AllHeld -->|"いいえ"| Pick{"読む事実がすべて成立し<br/>成立させる事実に未成立が残る実行"}
  Pick -->|"ない"| Stuck["stuck"]
  Pick -->|"契約の順で最初の 1 つ"| Turn["その実行の turn"]
  Turn -->|"agent 設定が変わった"| Unsafe["unsafe_agent_config"]
  Turn -->|"完了しない・解放されない・読めない"| Unknown["unknown_result"]
  Turn -->|"質問で終わる"| Waiting["waiting_for_answer"]
  Turn -->|"確かめられない評価が 2 回"| Invalid["invalid_result"]
  Turn -->|"指摘の後も候補・報告が同じ"| Stagnation
  Turn -->|"turn を事実に適用"| Start
```

事実は、判定したときの根拠(対象と読んだ事実の内容)が今と同じ間だけ成立し、根拠が変われば自動的に未成立に戻るので、engine は工程を持たずに次の実行を導く。続けられる止まり方は `waiting_for_answer`(回答)、`unknown_result` と `invalid_result`(やり直し)だけで、ほかは最終である([止まり方と終了コード](docs/architecture.md#止まり方と終了コード))。

## 1 turn

```mermaid
sequenceDiagram
  participant U as unit.py
  participant W as worktree
  participant P as prompts.py
  participant A as agent の port
  participant G as agent(sandbox の中)
  participant F as facts.py
  participant S as 状態の置き場所
  U->>W: agent 設定の digest と worktree より上を確かめる
  alt 変わっている
    U->>S: unsafe_agent_config で止める
  end
  alt 実行の session がない
    U->>P: 最初のメッセージ(定型文・指示・目的・受入条件・範囲・検査の command)
  else 実行の session の token がある
    U->>P: 続きのメッセージ(指摘や回答・未送付の報告・変更 file・検査の command)
  end
  U->>S: turn_started
  U->>A: TurnRequest(agent・model・effort・cwd・prompt・session)
  A->>G: session を始める、または続ける
  G->>W: 読み書きと検査の実行
  G-->>A: 返答と実行記録(承認の要求は拒否される)
  A->>A: session を解放する
  A-->>U: TurnOutcome(completed・text・actions・session・refused・released)
  opt session を続けられない
    U->>A: 指示の全文で新しい session として 1 回だけ起動し直す
  end
  U->>S: result/turns の turn の記録
  alt 解放されなかった
    U->>F: 完了していない turn(worktree は読まない)
  else 解放された
    U->>W: agent 設定と worktree より上を再び確かめる
    U->>W: tree の digest と変更 path を読む(環境の entry は外す)
    alt 読めないものがある
      U->>F: 完了していない turn(worktree-not-readable)
    else 全体を読めた
      U->>F: apply_turn(返答・実行記録・tree・変更 path)
    end
  end
  U->>S: turn_finished と state.json
```

engine が worktree を判定するのは、port が agent の session を解放して turn を返した後だけであり、読めないものが 1 つでもある tree では何も判定しない。書かない実行の turn では、git が記録する差分を turn の前後で比べ、変わっていれば完了として扱わない。詳細は [turn の終わりと worktree](docs/architecture.md#turn-の終わりと-worktree) と [環境の entry](docs/architecture.md#環境の-entry) にある。

## 事実の成立

```mermaid
flowchart LR
  subgraph ReportG["report"]
    R1["実行の turn が完了し<br/>質問で終わらない"] --> R2["成立<br/>内容: 書く実行は tree の digest<br/>書かない実行は返答の digest"]
  end
  subgraph ChecksG["checks"]
    C1["engine が検査 program を<br/>check/sha256/ に公開"] --> C2["作る実行の agent が<br/>指定の command を sandbox で実行"]
    C2 --> C3["program の最後の行<br/>名前・tree・digest・base・exit"]
    C3 --> C4["engine が実行記録の<br/>その command の行だけを読む"]
    C4 --> C5{"digest が今の tree<br/>base が置いた複製<br/>exit が期待どおり"}
    C5 -->|"はい"| C6["成立"]
    C5 -->|"いいえ"| C7["否定: 作る実行へ戻す"]
  end
  subgraph VerdictG["verdict"]
    V1["評価役の返答<br/>条件ごとの判定と指摘"] --> V2{"引用が今の file か報告にある<br/>根拠の command をこの turn で実行<br/>変更 file を読んだ"}
    V2 -->|"いいえ"| V3["数えない<br/>2 回で invalid_result"]
    V2 -->|"はい"| V4["評価役の判定として記録<br/>全評価役の判定が揃うのを待つ"]
    V4 --> V5{"block_at 以上の指摘<br/>または満たさない条件"}
    V5 -->|"ない"| V6["成立"]
    V5 -->|"ある"| V7["否定: 対象を作った実行へ戻す"]
  end
  subgraph GitG["git"]
    T1["対象の事実が成立"] --> T2["worktree の file と<br/>固定した git directory の<br/>元の commit の記録を比べる"]
    T2 --> T3{"変更 path が<br/>すべて scope の中"}
    T3 -->|"はい"| T4["成立"]
    T3 -->|"いいえ"| T5["否定: 範囲外の path を指摘"]
  end
```

どの事実も、agent の自己申告ではなく engine が確かめられるもの(engine の置いた program の状態の行、引用と実行記録、git の記録)で成立する。評価役は承認を宣言せず、合否は engine が計算する。検査の command と状態の行は [検査](docs/architecture.md#検査)、元の commit の扱いは [元の commit](docs/architecture.md#元の-commit) にある。

## agent の session と承認の要求

```mermaid
stateDiagram-v2
  state "session なし" as NoSession
  state "token を保つ" as Held
  state "閉じた" as Closed
  [*] --> NoSession
  NoSession --> Held : 実行の最初の turn。指示の全文で新しい session
  Held --> Held : 同じ実行が再び走る。続きのメッセージだけを送る
  Held --> Held : 続けられる止まり方、engine の再起動
  Held --> NoSession : 続けられないと port が返す。その turn を全文で 1 回だけ起動し直す
  Held --> Closed : 単位が続けられない止まり方で終わる
  Closed --> [*]
  note right of Held
    token は実行ごとに state.json にある
    turn が終わるたびに transport が session を解放し
    次の turn で token から読み込み直す
  end note
```

```mermaid
sequenceDiagram
  participant G as agent(codex・claude・pi)
  participant D as transport の driver
  participant U as engine
  G->>D: 承認・permission・利用者の入力の要求
  D-->>G: 明示的な拒否
  Note over G: turn は続く
  D-->>U: 実行記録の actions.refused(種類と短い説明)
  U->>U: 実行ごとの拒否の回数を state.json・status.json・stop.json に足す
```

session は実行ごとに 1 つで、ほかの実行(評価役と作る役を含む)と共有しない。承認の要求は 3 つの agent で同じく拒否され、単位は止まらない。拒否されて実行できなかった検査は合格の行を残さないので、その事実は成立しない。詳細は [agent の session](docs/architecture.md#agent-の-session) と [承認の要求](docs/architecture.md#承認の要求) にある。

## 置き場所と書き手

```mermaid
flowchart LR
  Cal["呼出し元の入口"]
  Eng["engine"]
  Ag["agent(sandbox の中)"]
  subgraph HomeG["状態の置き場所 DEV_SKILLS_HOME/loop-engine<br/>agent の sandbox から読めて書けない"]
    Inbox["inbox/"]
    Outbox["outbox/"]
    Lock["engine.lock"]
    Check["check/sha256/check_runner.py"]
    AgentState["agents/(transport の状態)"]
    subgraph UnitDirG["units/unit_id/"]
      Rec["contract.json・admission.json<br/>state.json・events.jsonl・status.json"]
      Cfg["checks.json"]
      Base["base-token/<br/>元の commit の複製"]
      Res["result/<br/>patch.diff・stop.json・turns/"]
    end
  end
  subgraph TreesG["worktree の置き場所 DEV_SKILLS_WORKTREES<br/>agent の sandbox から書ける"]
    WT["repository の digest/unit_id-token/"]
  end
  subgraph PkgG["導入済みの package"]
    Code["loop-engine/src/loop_engine/<br/>packages/cross-host-transport/"]
  end
  Cal -->|"書く"| Inbox
  Inbox -->|"読んで消す"| Eng
  Eng -->|"書く"| Outbox
  Outbox -->|"読む"| Cal
  Eng -->|"保持する"| Lock
  Eng -->|"書く"| Rec
  Eng -->|"書く"| Res
  Rec -->|"status.json を読む"| Cal
  Res -->|"stop.json・patch.diff を読む"| Cal
  Eng -->|"内容の sha256 の名前で公開"| Check
  Eng -->|"transport が書く"| AgentState
  Eng -->|"書く"| Cfg
  Eng -->|"git で作る"| Base
  Check -->|"実行する"| Ag
  Cfg -->|"読む"| Ag
  Base -->|"読む"| Ag
  Eng -->|"git で作る・turn の後に読む・最終の停止で消す"| WT
  Ag -->|"読み書き"| WT
  Code -->|"読む"| Eng
```

agent が書ける場所は worktree の置き場所だけで、判定に使うもの(検査 program、元の commit の複製、記録)はすべて agent が書けない状態の置き場所にある。この読み書きの区別を守るのは環境の sandbox 設定であり、engine は前提とするだけである。file の内容は [記録](docs/architecture.md#記録) にある。

## 信頼の境界

engine が確かめるもの:

- 契約は受付時に形と参照を検査し、agent 名、model、effort は固定の文字種に限る。agent は導入済みの対応表にあるものだけを受け付け、engine 自身の code を含む repository は受け付けない。
- 検査の定義は元の commit の `.dev-skills/checks.json` を git から読む。範囲と変更 path は、worktree を作った時点で読んだ git directory にある元の commit の記録と比べ、agent が書ける tree から元の内容を読まない。
- 各 turn の前後で、worktree の agent 設定(`.claude`、`.codex`、`.pi`、`.agents`、`.mcp.json`)の digest と、worktree より上に engine が作ったもの以外がないことを確かめる。
- 検査 program は名前を渡す前に、内容が directory 名の sha256 と一致することを確かめる。状態の行は今の tree の digest と、engine が置いた元の commit の複製の digest に結び付ける。
- 評価役の引用は今の file か判定対象の報告に、根拠の command はその turn の実行記録にあることを確かめる。評価する実行と、それが独立すべき実行は、契約が許さない限り agent と model の組が違う。
- session の解放を確かめられない turn と、全体を読めない worktree では何も判定しない。
- 環境が worktree の最上位に置く空の読み取り専用 file(どちらの commit にもない名前)だけは変更として数えず、外した名前を記録する。worktree の中のものは削除も変更もしない。

engine が確かめずに環境に頼るもの:

- agent の sandbox が有効であること、状態の置き場所と engine の Python を読めて状態の置き場所に書けないこと。
- 確認と agent の起動の間に別の agent が worktree の置き場所へ file を置かないこと。
- 検査 program が複製を写した後の、sandbox の中での test の振る舞い。
- agent の実行記録は transport が provider から受け取ったものであり、engine はそれを agent の報告文の代わりに使う。

## 決定

| 決定 | ADR |
|---|---|
| engine は host で動き sandbox を持たず、git と agent の command だけを起動する | [adr-20261114-loop-engine-host-boundary](docs/adr/adr-20261114-loop-engine-host-boundary.md) |
| 単位の完了は事実の成立で決め、次の実行は契約のデータから導く | [adr-20261114-unit-completes-when-facts-hold](docs/adr/adr-20261114-unit-completes-when-facts-hold.md) |
| 検査は agent が sandbox の中で実行し、engine は実行記録で判定する | [adr-20261114-checks-judged-from-execution-record](docs/adr/adr-20261114-checks-judged-from-execution-record.md) |
| 評価の合否は engine が計算し、確かめられる結果だけを数える | [adr-20261114-verdict-computed-from-checkable-findings](docs/adr/adr-20261114-verdict-computed-from-checkable-findings.md) |
| 実行は自分の session を続け、承認の要求は拒否して turn を続ける | [adr-20261114-agent-sessions-continue-and-approvals-are-refused](docs/adr/adr-20261114-agent-sessions-continue-and-approvals-are-refused.md) |
| 環境が置く空の読み取り専用 file は役の変更として数えない | [adr-20261115-environment-entries-are-not-changes](docs/adr/adr-20261115-environment-entries-are-not-changes.md) |
