---
id: issue-20261108-cross-host-design-implementation-loops-t
kind: issue
title: Cross-host design/implementation Loops take 45-60 min per unit and serialize
  on one checkout
status: open
created: '2026-11-08'
issue_type: plan
tags: []
owners: []
relations: []
subject_paths:
- runtime/development_loop/observe.py
- runtime/development_loop/production_dispatch_v2.py
- runtime/development_loop/instructions.md
- profiles/agent-placement-presets.yaml
summary: Design/implementation Loops via Codex took 45-60 min per unit, needed main-session
  interventions and could not run in parallel on one checkout.
---

## 症状 / 機会

2026-11-08、1 件の transport 改修（design 1 単位 + implementation 4 単位）を dev-skills の design / implement Loop で進めたところ、design に約 40 分、implementation 1 単位あたり 45〜60 分かかり、2 時間 40 分経過時点で 4 単位中 2 単位しか完了しなかった。ユーザーから「時間かかりすぎ」との指摘を受けた。implementation 単位を同じ checkout で並行実行することもできない。

## 証拠

- 単位ごとに Codex producer（implement は `gpt-6-luna` / xhigh、design / review は `gpt-6.1-sol` / low。`profiles/agent-placement-presets.yaml`）で「実装 → 独立評価 → 差し戻し → 再評価」を直列に回し、1 turn に 15〜30 分かかった。
- 初回の implementation 候補は通常テストを実行せずに提出され（producer の自己申告は「通常テストは実行していません」）、subsession テストが 28 failures + 7 errors だった。テスト実行は独立評価の差し戻しを待って初めて行われ、1 往復が余分にかかった。
- design producer（`gpt-6.1-sol` / low）は、repository を調査しないまま `BLOCKED`（pursuit-unresolved: inspection 不足）を返した。メインセッションが調査対象ファイルを示す pursuit-resolution を記録するまで進まなかった。
- 独立評価は、実機の Codex `thread/closed` 遅延（約 60 秒）に起因する close / continue の欠陥を見逃した。メインセッションが発見した。implementation Loop にはメインセッションの証拠や指摘を追加する入口がなく、`export-pursuit` → `cancel` → 後継 `open` で作り直した。
- 2 つの implementation Loop を同じ checkout で並行して開くと、後から開いた側が `world-observation-unconfirmed` で失敗した。`runtime/development_loop/observe.py` の `capture_binding` が worktree 全体の digest を前後で比較するため、もう一方の producer の書き込みで不一致になる。別 git worktree を作れば回避できるが、手順書（`runtime/development_loop/instructions.md`）に記載がない。
- producer が使う sandbox 内では、`tests/runtime/subsession` の cell 系テストが `Read-only file system: /run/user/1000/...` で失敗する（未変更の HEAD でも同じ）。producer と評価役はこれを候補による回帰と区別できず、差し戻しが 1 回発生した。

## 調査結果: 直列化か単位内 pipeline か（2026-11-08）

結論: 主因は単位内の pipeline であり、単位間の直列化は副次的である。

計測元（観測事実）: composition の各 execution の `invocation.json` / `receipt.json` の mtime、メインセッション transcript の transport 呼び出し時刻、Codex rollout（`~/.codex/sessions/2026/11/08/rollout-*-<thread>.jsonl`）の tool 呼び出し時刻・`Wall time`・token 使用量。tool 呼び出しの分類（Loop 手続き / テスト / コード）は入力文字列の正規表現による近似である。

1. 時間の内訳（11:27 の design dispatch から 14:07 の receipt 単位の candidate まで約 160 分）:
   - Codex producer の作業時間（dispatch → receipt）は約 128 分（80%）。うち implementer が約 114 分、評価役が 4 回で計約 9 分、design（designer 2 turn・評価役 1 turn）が約 5 分。
   - メインセッション側の間隙（producer の後処理、メインセッションの検証・待機・dispatch）は約 32 分（20%）。
2. implementer turn の中身（Codex rollout）:
   - turn 時間の 87〜91% はモデル側の時間で、tool の実行時間（テスト・Loop 手続き・コード操作の合計）は 1 turn あたり 2〜5 分。
   - 1 turn のモデル往復は 100〜230 回、1 往復は平均 9〜15 秒（`gpt-6-luna` / xhigh）。入力 token は 1 turn で 1,200〜3,500 万（cache 率 98〜99%）。
   - モデル時間の配分: コードの読み書き 56〜71%、Loop 手続き（manifest 読み取り・`agent-step`・checkpoint 等）12〜29%、テスト関連 9〜15%。評価役はモデル時間の約 74% が Loop 手続きだった。
3. 差し戻しの往復: chunk-host 2 回目の implementer turn（約 38 分）は初回候補がテスト未実行で提出されたため、chunk-lifecycle の 2 回目（約 16 分）は評価役が見逃した欠陥をメインセッションが見つけたため発生した。合計約 54 分（全体の約 34%）が手戻りである。
4. 直列化の影響: 承認済み設計の chunk 依存は host → lifecycle → receipt → loop の一直線で、transport の 3 chunk は同じ `mcp_application.py` を変更し内容上も依存していた。並行化できたのは Loop 側の chunk-loop だけで、短縮できた時間はその 1 単位分（全体の 15〜20% 程度、推定）に留まる。加えて、同じ checkout では Loop の world observation が並行実行を拒否する（証拠節を参照）。

推論: 所要時間を最も左右するのは、(a) xhigh の implementer が 1 turn に行う多数の短いモデル往復、(b) テスト実行と実機挙動の確認が候補提出後・評価後に回ることによる手戻り、(c) producer turn 内の Loop 手続き往復、の順である。いずれも 1 セッション分の計測で、モデルの応答時間の時間帯差は考慮していない。

## 改善の実施状況（2026-11-08）

Loop 手続きの内訳（chunk-host implementer、rollout の tool 入力による分類）: pursuit・候補状態の読み取り 30 回・モデル約 6.2 分、`loop agent` による pursuit/candidate 操作 16 回・約 3.3 分、`agent-step` 7 回・約 2.1 分、prompt・入力の読み取り 7 回・約 1.4 分、manifest 読み取り 2 回・約 0.9 分。`agent-step` 自体の実行コストは小さい。

実施した変更:

- `tests/runtime/subsession/test_cell_membership.py` の 2 件が、既定 cell path（`XDG_RUNTIME_DIR` 配下）を使い、読み取り専用の sandbox で失敗していた。test の一時ディレクトリを `XDG_RUNTIME_DIR` に使うよう修正し、producer と評価役が回帰と取り違える偽の失敗を除いた。`test_installed_package_authority` の multiprocessing Manager の失敗はソケット作成の制限による環境固有のもので、変更していない。
- `agents/definitions/{repository,readonly}-agent.yaml` の bootstrap に、独立した読み取り・検索・確認を 1 回の tool 呼び出しにまとめ、必要な範囲と出力量だけを要求する指示を加えた。往復回数への効果は未計測である。
- `profiles/implementation/profile.yaml` の implementer 指示は「全体の check は委任先へ提出し、広い check は不確実性の解消に必要なときだけ走らせる」としていた。implementation Profile には全体 check を実行する Execution がなく、producer がテストを省いて提出する余地になっていた。単位責務を覆う unit・contract・反例テストを修正中に使い、組み上げた候補で提出前に実行し、候補が起こした失敗を解消する指示に置き換えた。単位責務を超える check は持ち主に残す。

実施していない変更と理由:

- 候補提出時のテスト結果添付を必須にする機械ゲートは採用しない。accepted ADR `adr-20261010-outcome-evidence-boundaries`（固定 verify 段を廃止し、証拠の十分性を独立検収で判断する）に従い、テスト証拠の十分性は implementation の評価役（regression evidence の評価と `test-discipline` Knowledge）が判断する。turn 終了時の hook（Codex 0.167 / Claude Code の Stop hook）は、`agent-step` が候補を記録した後に発火するため、継続させると記録済み候補と worktree が食い違う。`agent-step` 内でテストを実行させる案は、ADR が廃止した固定 gate の移設になる。
- `agent-step` と `CONTINUE` の処理を runtime 側へ移す案は、上の内訳のとおり `agent-step` の往復が小さいため見送った。
- 実行中の Loop にメインセッションの証拠・指摘を追加する入口は、Loop の instruction 契約と review 入力の変更を伴う設計変更であり、未着手である。
- 配置 preset の effort（xhigh / high / medium）の比較計測は未実施である。

## 着手に必要な文脈

- 対象は dev-skills の Development Loop 実行経路（`runtime/development_loop/`）、配置 preset、`skills/implement` / `skills/design` の運用。
- 発見時の作業: cross-host transport の shared host / turn capacity / start receipt 分類の実装（design Loop `ba6b9545...`、implementation Loop `7cbd88bf...`、`769d706e...`、`6d705bb5...`、`e3dadf28...`）。
- 制約: Loop の独立評価とメインセッションによる受け入れ判断は残す。所要時間は計測値が 1 セッション分しかない。
