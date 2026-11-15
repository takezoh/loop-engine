---
id: adr-20261112-loop-command-continuations
kind: adr
title: Loop を非対話コマンド間で継続し MCP tool だけで host 資源を操作する
status: proposed
created: '2026-11-12'
decision_makers:
- unknown
tags: []
owners: []
relations: []
source_paths: []
summary: 対話 stdin の保持と利用側ラッパーを必須にしない。明示的な checkpoint と準備済み操作の receipt で再開し、host に
  kernel を移さず副作用を盲目的に再実行しない。
---

<!-- 本文セクション構成はproducer方法論 (例: design/knowledge/artifact-semantics.md) が規定する。
     dev-docs skill 自体はセクション構成を強制しない。Markdoc tag `{% context %}` `{% decision %}` `{% consequence kind="positive" %}` を任意で本文に bind できる。 -->

<!-- consequences は任意。記録する場合は positive / negative / neutral の三極を frontmatter に記す。
     例:
     consequences:
       positive: ["..."]
       negative: ["..."]
       neutral: ["..."]

  {% consequence kind="positive" %} ... {% /consequence %}
  {% consequence kind="negative" %} ... {% /consequence %}
  {% consequence kind="neutral" %}  ... {% /consequence %}
-->

<!-- OPTIONAL: `confirmation` block.
     この decision が守られていることを機械的に確認する手段 (fitness function / grep / test) を書く。
     frontmatter `confirmation:` (string) が SoT。使うときはこのコメントを削除して次のブロックを埋める。

  {% confirmation %} ... {% /confirmation %}
-->
