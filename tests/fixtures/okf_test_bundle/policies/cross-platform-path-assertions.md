---
type: Policy
title: Cross-platform path assertions
description: Never assert on a path as a slash-delimited string; use pathlib-native assertions instead.
resource: /references/okf-spec-md.md
tags: [testing, paths, cross-platform]
generated: { by: "okf-spec/0.3", at: "2026-11-21T00:00:00Z" }
sources:
  - id: okf-spec-md
    resource: /references/okf-spec-md.md
    title: OKF v0.3 SPEC.md
---

Tests run on Linux, macOS, and Windows. Never assert on a path as a slash-delimited string; `Path` renders `/` on POSIX and `\` on Windows.[^okf-spec-md]

- Use `Path(value).name`, `Path(value).parts`, or `Path(value).as_posix()` for assertions and rendered tool output.
- Never use `line.split("/")` or assert a literal `/c`-style path substring.

[^okf-spec-md]: OKF v0.3 SPEC.md