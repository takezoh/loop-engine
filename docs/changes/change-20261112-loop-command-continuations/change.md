---
id: change-20261112-loop-command-continuations
kind: change
title: Design of a Loop that can be resumed with non-interactive commands and MCP tools
status: draft
created: '2026-11-12'
profile: sdd@1
intent: Separate the Loop's decisions from its side effects, and change to a structure that can continue with only finite
  processing in the sandbox and boundary operations through MCP tools, without a wrapper on the using side.
outcomes:
- The kernel takes only values as input and returns a decision without calling the filesystem, controller or transport.
  Reservation and recovery alone do not start a producer.
- The Loop continues with only ordinary non-interactive commands and the session's MCP tools, and needs no relay script,
  socket, FIFO or interactive stdin on the using side.
scope: []
non_goals: []
change_classes: []
governance:
  gate: auto
  reasons: []
members:
- role: requirements
  path: changes/change-20261112-loop-command-continuations/requirements.md
  required: true
- role: implementation
  path: changes/change-20261112-loop-command-continuations/implementation.md
  required: true
- role: verification
  path: changes/change-20261112-loop-command-continuations/verification.md
  required: true
promotion: []
unresolved_decisions: []
tags: []
owners: []
relations: []
source_paths: []
summary: The structural rework is being implemented. It separates the kernel's pure decisions, the orchestration of
  observation and persistence, and explicit effect execution. The completion condition extends through the removal of
  interactive stdin and acceptance on a non-interactive host.
---

## Summary

## Closure Notes
