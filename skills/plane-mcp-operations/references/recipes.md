# Recipes

All examples are argument shapes, not executable authorization. Replace every
placeholder only with fresh provider IDs and an operator-authorized target.
Client prefixes such as `mcp__plane__` are installation-specific.

## Read and discover

- `get_current_user({})`: authenticated identity check; do not dump private fields.
- `list_projects({"workspace_slug": "<workspace>"})`: resolve project.
- `get_work_item({"workspace_slug": "<workspace>", "project_id": "<project-id>",
  "work_item_id": "<item-id>"})`: read exact target.
- `plane_capture_state_catalog({"workspace_slug": "<workspace>",
  "project_id": "<project-id>"})`: read states; resolve Ready by current project semantics.
- `plane_action_descriptor({"operation": "issue__update_issue_detail",
  "path_params": {"workspace_slug": "<workspace>", "project_id": "<project-id>",
  "resource_id": "<item-id>"}})`: inspect contract without writing.

A workspace slug is not a workspace UUID. Detail registry actions commonly use
`resource_id`; convenience tools use `work_item_id`. Follow each tool's schema.
For labels, discover `label__list_labels` with workspace and project; do not
substitute a workspace-wide label route for a project's taxonomy.

## Create

`plane_mutation_action`:

```json
{
  "operation": "issue__add_issue",
  "path_params": {"workspace_slug": "<workspace>", "project_id": "<project-id>"},
  "payload": {"name": "<approved-title>"},
  "idempotency_key": "<unique-operation-key>",
  "attempts": 1
}
```

This is the technical minimum, not a claim of organizational readiness. Add the
approved description, assignees, labels and other required fields from discovery.
Do not infer owner from the API token. Capture provider ID and exact readback.

## Backlog → Ready / ADMIT

After policy admission and fresh item/state reads:

```json
{
  "workspace_slug": "<workspace>",
  "project_id": "<project-id>",
  "work_item_id": "<item-id>",
  "target_state_id": "<ready-state-id>",
  "expected_state_id": "<fresh-current-state-id>",
  "expected_updated_at": "<fresh-provider-updated-at>",
  "idempotency_key": "<unique-transition-key>",
  "attempts": 1
}
```

Tool: `plane_lifecycle_transition`. Inspect `status`, `state` and `comment`
receipts. A precondition failure means refresh/reassess, not force the change.
Alternatively, `plane_mutation_action` with `operation=issue__update_issue_detail`,
`path_params.resource_id` and `payload={"state":"<ready-state-id>"}` is supported.
Neither route implements atomic compare-and-set. If atomicity is mandatory,
report the provider limitation rather than claim the pre-read solves it.

## Comment

`plane_add_comment` takes `workspace_slug`, `project_id`, `work_item_id`,
`comment_html`, `idempotency_key`, and optionally `attempts=1`.
For structured lifecycle evidence, use the renderer/validator's actual contract;
ordinary comment support does not waive project policy. A state-only transition
must not be reported as a posted lifecycle comment.

## Delete and reconcile

DELETE is destructive and requires explicit target/action scope. Use
`plane_mutation_action(operation="issue__delete_issue", path_params={...,
"resource_id":"<item-id>"}, payload={}, idempotency_key="<key>", attempts=1)`.
Verify target absence, exhausting pagination when collection evidence is used.
An unrelated surviving item's detail-schema differences do not disprove absence.
An empty filtered page alone is not global absence. Only typed HTTP 404 at the
correct detail endpoint proves detail absence; generic MCP errors do not.

On an ambiguous/applied-but-unverified mutation, use `plane_reconcile_mutation`
with the original operation, path_params, payload, query and idempotency_key.
Do not reconstruct a different payload or change the key. Convenience workflows
can derive per-step keys: preserve their receipt information; do not guess those
keys. Reconciliation refreshes evidence and may update the local ledger, but does
not replay the provider write. Legacy-specific reconciliation tools are not the
normal operation path and may have separate historical authorization contracts.
