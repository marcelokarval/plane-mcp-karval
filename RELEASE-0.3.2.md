# Plane MCP Karval 0.3.2

## Delivered

- Portable `plane-mcp-operations` skill owned by this repository and bundled in the wheel.
- Global skill installer with check/apply modes, hash verification and backup of previous content.
- Operational recipes and troubleshooting for fresh discovery, mutations, state transitions, idempotency and verified readback.
- Audit of legacy Plane skill responsibilities.
- Harness-specific policy script proposal in `ROADMAP.md`.

Plugins are a roadmap only: this release does not implement or activate a plugin loader or harness policies. The Plane MCP runtime core remains independent of harness-specific rules.

## Traceability

Canonical tracking item: HERMES-254 (Plane). GitHub is the source, PR and release surface, not a duplicate issue tracker for this delivery.

Implementation commits:

- `990cf14d73d61b2a4528a253637418ae225852c1`: portable operating skill.
- `8a9a1308266969e0f86b968843b2f879f1fd1e72`: harness policy plugin roadmap.

## Verification

The candidate passed the full suite: 520 tests, with one non-blocking Authlib deprecation warning. `uv lock --check` and `git diff --check` passed.

Release artifacts are built from the merged commit; the GitHub release supplies the wheel and SHA-256 checksum. Publication does not imply rollout or reconnection of any configured MCP client. No credentials or local runtime state belong in release artifacts.
