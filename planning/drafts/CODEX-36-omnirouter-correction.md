# CODEX-36 — OmniRouter correction dependent draft

> **Local draft only.** This file is not a Plane lifecycle record and does not
> claim that any Plane event occurred.

## Dependency and handoff

CODEX-36 is dependent on the MCP prepare → explicit-confirm → execute
compatibility contract in the companion TASK-026 draft. The correction should
use the same explicit authorization boundary, idempotency binding, receipt
shape, and fail-closed error handling rather than introducing a parallel
mutation path.

Validate the correction against the local implementation and tests before any
provider-facing registration. Later Plane registration is historical evidence
only: record only events that can be proven from receipts or other preserved
artifacts, with no invented lifecycle events or outcomes.
