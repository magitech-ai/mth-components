# Agent Guidelines

## Plugin Architecture & Invariants

- **Repository Role & Marketplace Structure**:
  - `mth-plugins` is the official extensible component marketplace for `magitech-harness`.
  - Implements pluggable model providers (`openrouter-provider`, `pollinations-provider`), domain tools (`bash-tool`, `codeact-tool`, `read-file-tool`, `write-file-tool`, `edit-file-tool`), and guardrails (`truncation-guard`, `compaction-guard`).
  - Every component ships with a `manifest.json` describing its metadata, type, and entrypoints, registered in `index.json`.
- **Component Integration**: Components mount into `anima` context through an `apply(ctx)` entry point and register capabilities on explicit services such as `ctx.llm` and `ctx.tools`.
- **No Hardcoded Tunables**: Provider endpoints, default models, and token pressure thresholds must be configurable parameters, not hidden hardcoded constants.

## Shared Development Guidance

Follow [agents-home AGENTS](../agents-home/AGENTS.md) for cross-repository
standards, [uv skill](../agents-home/skills/uv/SKILL.md) for package management
and standard test/type-check commands, and
[Ruff skill](../agents-home/skills/ruff/SKILL.md) for linting and formatting.

## Plugin-Specific References

### Issue tracker
GitHub Issues via `gh` CLI (`magitech-ai` organization). See `docs/agents/issue-tracker.md`.

### Triage labels
Canonical five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs
Single-context layout (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.
