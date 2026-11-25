# mth-components

Official portable components and marketplace catalog for [Magitech Harness](https://github.com/magitech-ai/magitech-harness).

## Overview

`mth-components` contains the **optional, on-demand, and third-party** model
providers, tools, guards, bridges, and session services that round out the
runtime. Everything mounted by the reference's base bundle — `bash`, the
filesystem tools, `glob`/`grep`, jobs, todo, and goals — ships in
`magitech-harness` instead ([ADR 0022](../magitech-harness/docs/adr/0022-supersede-zero-builtin-domain-tools-base-bundle-in-harness.md)).
Each component ships a `manifest.json` and is registered in the marketplace
catalog (`src/mth_components/index.json`). Components mount into an `animas`
context through `apply(ctx)` and register capabilities on explicit services
such as `ctx.llm` and `ctx.tools`.

## Structure

| Category   | Components | Source |
| :--------- | :--------- | :----- |
| Providers  | `openrouter-provider`, `pollinations-provider` (*) | `src/mth_components/providers/` |
| Tools      | `bash-tool`, `codeact-tool`, `edit-file-tool`, `fs-search-tool`, `fs-extra-tools`, `code-analysis-tools`, `web-search-tool`, `verify-repo-tool`, `dev-tools`, `read-file-tool`, `subagent-tool`, `write-file-tool` | `src/mth_components/tools/` |
| Guards     | `approval-guard`, `compaction-guard`, `redaction-guard`, `repeat-tool-reminder`, `timeout-policy`, `truncation-guard`, `workspace-trust-gate` | `src/mth_components/guards/` |
| Bridges    | `lsp-bridge`, `mcp-bridge`, `opentelemetry-bridge`, `skills-registry`, `skills-filesystem`, `skills-loader`, `okf-registry`, `okf-filesystem`, `okf-loader`, `steering-bridge`, `workspace-snapshot` | `src/mth_components/bridges/` |
| Sandboxes  | `sandbox-ladder` | `src/mth_components/sandbox/` |
| Sessions   | `session-search` | `src/mth_components/sessions/` |

(*) Pollinations is a magitech-only keyless provider. The Agent Skills and OKF
bridges are Magitech extensions. None of them is parity; see
[PARITY.md](PARITY.md) for the ownership rule and the current transition state.

## Discovery and installation

The installed wheel advertises the shipped catalog through the
`magitech.components` entry point, so `mth component discover` and
`mth component install` work without a source checkout. Catalog lookup and
discovery live in `src/mth_components/index.py`.

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
