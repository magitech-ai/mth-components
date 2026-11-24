# mth-plugins

Official portable components and marketplace catalog for [Magitech Harness](https://github.com/magitech-ai/magitech-harness).

## Overview

`mth-plugins` contains model providers, domain tools, guards, bridges and
session services adhering to the **Everything is a Component** architecture.
Each component ships a `manifest.json` and is registered in the marketplace
catalog (`src/mth_plugins/index.json`). Components mount into an `animas`
context through `apply(ctx)` and register capabilities on explicit services
such as `ctx.llm` and `ctx.tools`.

## Structure

| Category   | Components | Source |
| :--------- | :--------- | :----- |
| Providers  | `openrouter-provider`, `pollinations-provider` (*) | `src/mth_plugins/providers/` |
| Tools      | `bash-tool`, `codeact-tool`, `edit-file-tool`, `fs-search-tool`, `fs-extra-tools`, `code-analysis-tools`, `web-search-tool`, `verify-repo-tool`, `dev-tools`, `read-file-tool`, `subagent-tool`, `write-file-tool` | `src/mth_plugins/tools/` |
| Guards     | `approval-guard`, `compaction-guard`, `redaction-guard`, `repeat-tool-reminder`, `timeout-policy`, `truncation-guard`, `workspace-trust-gate` | `src/mth_plugins/guards/` |
| Bridges    | `lsp-bridge`, `mcp-bridge`, `opentelemetry-bridge`, `skills-registry`, `skills-filesystem`, `skills-loader`, `okf-registry`, `okf-filesystem`, `okf-loader`, `steering-bridge`, `workspace-snapshot` | `src/mth_plugins/bridges/` |
| Sandboxes  | `sandbox-ladder` | `src/mth_plugins/sandbox/` |
| Sessions   | `session-search` | `src/mth_plugins/sessions/` |

(*) Pollinations is a magitech-only keyless provider. The Agent Skills and OKF
bridges are post-1.0 additions. Neither group is part of the 1.0 parity scope;
see [PARITY.md](PARITY.md).

## Discovery and installation

The installed wheel advertises the shipped catalog through the
`magitech.components` entry point, so `mth component discover` and
`mth component install` work without a source checkout. Catalog lookup and
discovery live in `src/mth_plugins/index.py`.

## Development

```sh
uv sync
python3 ../agents-home/scripts/check_all.py --fast   # local pre-commit gate
uv run pytest -q                           # full suite, 100% floor
```

CI runs the full shared gate — lint, format, strict mypy, tests, and a 100%
statement-and-branch coverage floor — on Linux, macOS and Windows.

## Documentation

- [CONTEXT.md](CONTEXT.md) — domain vocabulary.
- [docs/adr/](docs/adr/) — architecture decision records.
- [PARITY.md](PARITY.md) — 1.0 parity pins and current status.
- [AGENTS.md](AGENTS.md) — agent guidance and shared standards.
- [Release notes](docs/release-notes/).

## License

Apache-2.0
