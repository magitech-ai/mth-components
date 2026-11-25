# Parity

The authoritative parity map lives in the harness repo:
[`magitech-harness/PARITY.md`](../magitech-harness/PARITY.md). It is the
evidence-backed map against the pinned reference, DeepSeek Harness
`4b399c39a6d1479cd84ca591b4d7c88e24d38a96` (v0.2.3-alpha.2), and it supersedes
the earlier `fcc0ad46` (0.2.2-rc.1) record.

## What belongs here

Ownership is mechanical ([ADR 0022](../magitech-harness/docs/adr/0022-supersede-zero-builtin-domain-tools-base-bundle-in-harness.md)):

- **`magitech-harness`** owns every tool and service mounted by the reference's
  `packages/bundle/base/cordis.patch.yml`.
- **`mth-components`** owns optional, on-demand, and third-party capabilities:
  single-vendor providers, extra tools, marketplace guards, bridges.

So this repository holds:

- Providers: `openrouter-provider`, `pollinations-provider` (a Magitech-only
  keyless provider, labelled as an extension, not parity).
- Extra tools: `codeact-tool`, `code-analysis-tools`, `fs-extra-tools`,
  `web-search-tool`, `verify-repo-tool`, `dev-tools`, `subagent-tool`.
- Marketplace guards: `redaction-guard`, `truncation-guard`,
  `workspace-trust-gate`, plus the historical duplicate guard rows.
- Bridges: LSP, MCP, OpenTelemetry, Agent Skills (registry/filesystem/loader),
  OKF (registry/filesystem/loader), steering, workspace snapshot.
- Sandboxes and sessions: `sandbox-ladder`, `session-search`.

## Known transition

Six base-bundle tools — `bash`, `read_file`, `write_file`, `edit_file`, `glob`,
`grep` — are implemented and probed here but are owned by `magitech-harness`
under ADR 0022. They are recorded PARTIAL in the authoritative map until the
relocation (implementation + tests + catalog entry + `base.yaml` row) lands.
This is a recorded gap, not an accepted one.

## Historical pins

The frozen 1.0 audit numbers below are kept as history and are deliberately not
rewritten:

- Version `0.3.0`, audited at the C6-fix merge on `main`.
- Suite at that audit: 402 tests, 100% statement + branch coverage.
- Reference frozen at `fcc0ad46…` (0.2.2-rc.1) for that audit.

Current local verification is the repository's own gate; CI is the authority.
