---
id: adr-20261112-loop-command-continuations
kind: adr
title: Continue a Loop across non-interactive commands and operate host resources only through MCP tools
status: proposed
created: '2026-11-12'
decision_makers:
- unknown
tags: []
owners: []
relations: []
source_paths: []
summary: Do not require holding an interactive stdin or a caller-side wrapper. Resume from an explicit checkpoint and the receipts of prepared operations,
  without moving the kernel to the host and without blindly re-running side effects.
---

<!-- The section structure of the body is defined by the producer methodology (for example: design/knowledge/artifact-semantics.md).
     The dev-docs skill itself does not enforce a section structure. The Markdoc tags `{% context %}` `{% decision %}` `{% consequence kind="positive" %}` can optionally be bound in the body. -->

<!-- consequences is optional. To record them, write the three poles positive / negative / neutral in the frontmatter.
     Example:
     consequences:
       positive: ["..."]
       negative: ["..."]
       neutral: ["..."]

  {% consequence kind="positive" %} ... {% /consequence %}
  {% consequence kind="negative" %} ... {% /consequence %}
  {% consequence kind="neutral" %}  ... {% /consequence %}
-->

<!-- OPTIONAL: `confirmation` block.
     Write the means of mechanically confirming that this decision is kept (fitness function / grep / test).
     The frontmatter `confirmation:` (string) is the SoT. To use it, delete this comment and fill in the following block.

  {% confirmation %} ... {% /confirmation %}
-->
