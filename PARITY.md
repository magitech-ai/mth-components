# Parity

The authoritative 1.0 parity map lives in the harness repo:
[`magitech-harness/PARITY.md`](../magitech-harness/PARITY.md). It is the Phase 8
re-audit deliverable that proves acceptance criterion 1 (every in-scope item
DONE or PARTIAL-with-accepted-gap, nothing MISSING in-scope).

mth-plugins-specific pins at the 1.0 gate:

- Version `0.2.0`, audited at the C6-fix merge on `main`.
- Suite: 402 tests, 100% statement + branch coverage; CI green on all 6 legs.
- Tier 1 tools/providers (ratified §8) are all present; the C6 fix added the
  last missing one (`fs-search-tool`, registering `glob` and `grep`).
- Four kept inventions (Pollinations, truncation-guard, workspace-trust-gate,
  git-shadow checkpoint) are labelled magitech-only extensions, not parity.
- Accepted gaps (finding #13: Windows ACL, fs `sandbox_policy` enforcement,
  sandbox escalation) are recorded in the authoritative map.
- The reference is frozen at `fcc0ad46…` (0.2.2-rc.1) for the 1.0 audit; rc.3
  drift is in the post-1.0 appendix of the authoritative map.
