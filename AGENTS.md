# Agent Guidelines

## Plugin Architecture & Invariants

- **Repository Role & Marketplace Structure**:
  - `mth-components` is the official extensible component marketplace for `magitech-harness`.
  - Owns **optional, on-demand, and third-party** capabilities only. Anything mounted by the reference's `packages/bundle/base/cordis.patch.yml` belongs to `magitech-harness` (ADR 0022), not here.
  - Implements pluggable model providers (`openrouter-provider`, `pollinations-provider`), extra tools (`codeact-tool`, `code-analysis-tools`, `fs-extra-tools`, `web-search-tool`, `verify-repo-tool`, `dev-tools`, `subagent-tool`), and marketplace guardrails (`redaction-guard`, `truncation-guard`, `workspace-trust-gate`).
  - Relocated: `bash-tool`, `read-file-tool`, `write-file-tool`, `edit-file-tool`, and `fs-search-tool` now live in `magitech-harness` (`tools/{bash_tool,fs,fs_search}.py`) and are mounted by its `base.yaml`; they are no longer sold here.
  - Every component ships with a `manifest.json` describing its metadata, type, and entrypoints, registered in `index.json`.
- **Component Integration**: Components mount into `animas` context through an `apply(ctx)` entry point and register capabilities on explicit services such as `ctx.llm` and `ctx.tools`. Tool definitions on `ctx.tools` use the canonical `magitech_harness.tools.schema.define_tool`; the legacy `Tool`/`tools.definition` module no longer exists.
- **Component discovery** — catalog: `src/mth_components/index.py`; Agent Skills activation: `bridges/skills_registry`, `bridges/skills_filesystem`, `bridges/skills_loader`; OKF knowledge activation: `bridges/okf_registry`, `bridges/okf_filesystem`, `bridges/okf_loader`; safeguards: `docs/adr/0012-0014`.
- **No Hardcoded Tunables**: Provider endpoints, default models, and token pressure thresholds must be configurable parameters, not hidden hardcoded constants.

## Shared Development Guidance

Shared standards, workflow and quality gates:
[agents-home AGENTS](../agents-home/AGENTS.md) → `knowledge/engineering-standards/index.md`,
`docs/WORKFLOWS.md`, `knowledge/coding-standards/index.md`, and `scripts/check_all.py`.

### Local checks
Run `python3 ../agents-home/scripts/check_all.py --fast` before committing;
CI runs the full gate.

## Plugin-Specific References

### Issue tracker
GitHub Issues via `gh` CLI (`magitech-ai` organization). See `docs/agents/issue-tracker.md`.

### Triage labels
Canonical five-role triage vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs
Single-context layout (`CONTEXT.md` + `docs/adr/`). See `docs/agents/domain.md`.

