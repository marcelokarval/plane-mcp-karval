---
name: plane-mcp-operations
description: Use when operating Plane through plane-mcp-karval. Discover tools, read current state, create/update/comment, run simple or operator-bound v3 lifecycle transitions, reconcile writes, or troubleshoot MCP client drift.
license: MIT
metadata:
  version: "0.3.4"
---

# Plane MCP Operations

## Scope

Operate the shared `plane-mcp-karval` MCP in Hermes, Codex, OpenDesign or another
configured MCP client. This skill owns tool mechanics, not organizational policy.
It does not require Hermes-native connectors, profile files or task-stack scripts.
Use the actual tool names/schema advertised by the connected client; prefixes vary.
The operator-bound lifecycle route requires plane-mcp-karval 0.3.3 or newer;
live operations require trusted local stdio and configured Plane credentials.

## Fast path

1. Inspect connected tools. Require `plane_mutation_action`,
   `plane_lifecycle_transition`, `plane_operator_lifecycle_transition`,
   `plane_add_comment` and `plane_reconcile_mutation` for the corresponding task.
   Reconnect after an upgrade before declaring a newly added capability absent.
2. Before claiming current Plane state, read/search through the configured governed
   Plane MCP/connector. Discover the exact workspace, project, item and state IDs.
   A prior conversation, skill example or release note is not a current snapshot.
3. For unfamiliar actions: `plane_catalog` → `plane_action_descriptor` → inspect
   required arguments. The descriptor is offline; it neither writes nor authorizes.
4. Execute only the operator-authorized scope. An ordinary mutation takes operation,
   path parameters, payload and an idempotency key, with `attempts=1`. Do not invent
   `approved_live_mutation`, `authorization_receipt`, `issue_readiness` or a second
   consent ceremony for this public MCP tool. Existing policy/readiness still applies.
5. Verify the receipt and exact target. Distinguish `mutation_applied` from
   `readback_verified`; report partial/unknown outcomes honestly. Never repeat an
   ambiguous write, mint a new key to force it, or clear the ledger.

## State changes, including ADMIT

`ADMIT` is a policy meaning (Backlog → Ready), not a mandatory tool name or a
universal provider state. Resolve the project's states and readiness first.
Use `plane_lifecycle_transition` with `target_state_id` and a fresh
`expected_state_id`/`expected_updated_at` when available, or use the registered
`issue__update_issue_detail` via `plane_mutation_action` with `payload.state`.
State PATCH through this MCP is supported, not a bypass. Raw HTTP is not needed.
These operations are non-atomic: pre-read is not compare-and-set, and state plus
comment can partially succeed. Inspect nested receipts; do not auto-compensate.
For ordinary trusted-stdio state changes, the simple routes above remain valid.
When the governing client requires contract v3, use
`plane_operator_lifecycle_transition`: it binds explicit approval to one target,
requires canonical phase/state roles, fresh state and revision preconditions,
`attempts=1`, and explicit acceptance of Plane's non-atomic state/comment sequence.
The host owns human authorization; the MCP validates and records its bounded
technical representation without opening a second elicitation ceremony.

## Comments and evidence

Use `plane_add_comment` for ordinary HTML comments. Use
`plane_render_lifecycle_comment` / `plane_add_lifecycle_comment` for structured
lifecycle packets; inspect their current schema and validation result. Rendering
alone does not post. Do not invent an ADMIT renderer phase if unsupported.
Preserve required project evidence, title/taxonomy semantics and ownership policy.
Do not fabricate historical events, timestamps, runtime metadata or approvals.
Never put credentials, hidden reasoning or raw session logs into Plane.

## Resources — load only as needed

- [Recipes](references/recipes.md): exact tool shapes for reads, state changes,
  comments, DELETE and reconciliation; placeholder IDs are not real targets.
- [Troubleshooting](references/troubleshooting.md): stale clients, errors, partial
  writes, HTTP/stdio boundaries and installation checks.
- [Skill-family audit](references/skill-family-audit.md): responsibility split and
  legacy mechanics corrected for this release; not a current Plane-state report.

## Verification

Use the configured client's MCP path for readback. A successful HTTP ACK, a
rendered comment, a green offline validator or another client's successful run
alone does not prove the requested provider change. For operator-authorized live
smokes use a bounded test item and verify cleanup. Do not execute mutations just
to load or validate this skill. Catalog coverage is not full live API coverage.
