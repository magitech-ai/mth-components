# Agent Guidelines

## Plugin Architecture & Invariants

- **Repository Role & Marketplace Structure**:
  - `mth-plugins` is the official extensible component marketplace for `magitech-harness`.
  - Implements pluggable model providers (`openrouter-provider`, `pollinations-provider`), domain tools (`bash-tool`, `codeact-tool`, `read-file-tool`, `write-file-tool`, `edit-file-tool`), and guardrails (`truncation-guard`, `compaction-guard`).
  - Every component ships with a `manifest.json` describing its metadata, type, and entrypoints, registered in `index.json`.
- **Component Integration**: Components mount into `anima` context through an `apply(ctx)` entry point and register capabilities on explicit services such as `ctx.llm` and `ctx.tools`.
- **Component discovery** — catalog: `src/mth_plugins/index.py`; activation: `bridges/skills/service.py`; safeguards: `docs/adr/0012-0014`.
- **No Hardcoded Tunables**: Provider endpoints, default models, and token pressure thresholds must be configurable parameters, not hidden hardcoded constants.

## Shared Development Guidance

Shared standards, workflow and quality gates:
[agents-home AGENTS](../agents-home/AGENTS.md) → `docs/ENGINEERING.md`,
`docs/WORKFLOWS.md`, `CODING_STANDARDS.md`, and `scripts/check_all.py`.

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

