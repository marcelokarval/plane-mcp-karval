# Contribution method

Every change, including documentation and configuration, requires an issue first and a pull request before merge. Direct pushes to the default branch are not an accepted delivery path.

1. Open an issue describing the problem, scope, non-goals, acceptance criteria and verification. Never include secrets, customer data, private receipts or local operational paths.
2. Obtain maintainer agreement on scope before implementation. Work on a dedicated branch, keep the diff bounded and preserve technical safety controls.
3. Reproduce defects with a regression test. Run `uv sync --frozen`, `uv run --frozen pytest -q`, `uv lock --check`, and `git diff --check`. Use fake providers: CI must never mutate a live Plane workspace.
4. Open a PR linking the existing issue, documenting implementation, tests, risks and rollback. No issue means no merge. Public contributions use a GitHub issue in this repository. Maintainer work may originate in a private tracker: a maintainer must verify the real issue and apply `issue-verified`; do not publish private identifiers or URLs.
5. Resolve review discussions and pass current checks. Review and merge are maintainer responsibilities; self-authored claims of approval are not reviews.
6. Release only from a merged immutable commit; build and test the package in isolation, scan its contents, and publish checksums.

## Required repository enforcement

Before public launch, configure protected main: PR required, required CI checks, resolved review conversations, no force pushes or deletion, and review approval from an eligible independent maintainer. Read back settings. Documentation and a workflow alone do not prove branch protection is active.

## Core invariants

Preserve authentication, target/payload validation, durable idempotency, single-attempt writes and exact readback. Never replay an ambiguous mutation. Reconciliation is read-only. Harness rules belong in separately governed plugins, not ad-hoc branches in the shared Plane client. Planned plugins are not implemented capabilities.

## Rights and security

Read LICENSE, THIRD_PARTY.md and SECURITY.md. Project-owned contributions are
distributed under MIT; third-party material retains its applicable terms.
Do not submit third-party material without documented permission; identify
provenance and license obligations. Security reports must use a private maintainer
channel, not a public issue containing exploit details or credentials.
