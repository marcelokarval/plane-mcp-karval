# Release 0.3.4

Status: implementation candidate; release tag has not been published.

## Scope

This release expands the frozen Plane API contract from 225 to 233 documented
operations and from 134 to 139 governed mutations. The existing v0.3.3 tag
remains immutable.

## Added API coverage

- Inbox Issues: add, list, detail, update, and delete.
- Work-item relations: create, list, and remove.
- Relation-map response validation for Plane's eight relation buckets.
- Mutation readback for relation presence and absence.
- Detail-state readback for Inbox Issue updates.

## Added quality gates

- `scripts/build_v034_registry.py` deterministically extends the registry and
  source contract from the official Plane documentation.
- `scripts/verify_contract_parity.py` validates registry/source parity and the
  v0.3.4 endpoint denominator without provider calls.
- `scripts/verify_release_consistency.py` checks package, skill, release notes,
  and contract counts before release.
- `scripts/plane_contract_diagnostics.py` prints safe contract/runtime metadata;
  it performs zero provider calls and zero mutations by default.

## Explicitly deferred

- Issue-attachment upload remains outside this release because it needs a
  multipart/storage security boundary, temporary-file policy, and reconciliation.
- Harness plugins remain v0.4.x scope.
- Shared broker/process consolidation remains v0.5.x scope.

## Verification target

Before tagging, run the complete test suite, contract parity, release
consistency, `uv lock --check`, package build, and isolated installation.
