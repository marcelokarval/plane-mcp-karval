"""Karval-owned Plane MCP server.

The public MCP surface is intentionally small and registry-first.  Full Plane
coverage lives in the checked-in operation registry, not in a dependency on the
upstream Plane MCP package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from importlib.metadata import version as installed_version
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from plane_api import (
    CONTRACT_HASH,
    CONTRACT_VERSION,
    HTTP_OPERATION_COUNT,
    METHOD_COUNTS,
    MUTATION_COUNT,
    OPERATION_COUNT,
    PlaneClient,
    PlaneConfig,
    get_operation,
    list_operations,
)
from plane_api.client import migrate_legacy_ledgers
from plane_mcp_karval.plane_title_icons import (
    ICON_CONTRACT_VERSION,
    PRESENTATION_CONTRACT_VERSION,
    normalize_title,
    presentation_descriptor,
    semantic_icon_id,
    validate_frozen_snapshot,
    validate_title,
)
from plane_mcp_karval.plane_work_item_contract import (
    CONTRACT_VERSION as WORK_ITEM_CONTRACT_VERSION,
    calculate_timing_metrics,
    render_comment,
    validate_document,
)
from plane_mcp_karval.issue_creation_readiness import (
    CONTRACT_VERSION as ISSUE_READINESS_CONTRACT_VERSION,
    readiness_fingerprint,
    validate_issue_creation_readiness,
)


Method = Literal["GET", "POST", "PATCH", "DELETE"]
Mode = Literal["read", "descriptor"]

LEGACY_LEDGERS = (
    "~/.hermes/state/plane-governed-mutations.json",
    "~/.codex/state/plane-mcp-karval-mutations.json",
)


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def _base_url() -> str:
    return _env("PLANE_BASE_URL") or _env("PLANE_API_URL") or _env("PLANE_API_HOST_URL")


def _api_token() -> str:
    return _env("PLANE_API_KEY") or _env("PLANE_API_TOKEN")


def _default_workspace() -> str:
    return _env("PLANE_WORKSPACE_SLUG")


def _client() -> PlaneClient:
    base_url = _base_url()
    token = _api_token()
    if not base_url:
        raise RuntimeError("Plane MCP missing PLANE_BASE_URL or PLANE_API_URL")
    if not token:
        raise RuntimeError("Plane MCP missing PLANE_API_KEY or PLANE_API_TOKEN")
    return PlaneClient(
        PlaneConfig(
            base_url=base_url,
            token=token,
            instance_id=_env("PLANE_INSTANCE_ID"),
            timeout=float(_env("PLANE_TIMEOUT") or "20"),
        )
    )


def _ledger_path() -> Path:
    """Resolve one shared ledger and absorb compatible historical state once."""
    configured = _env("PLANE_MUTATION_LEDGER")
    if configured:
        return Path(configured).expanduser()
    legacy_paths = [Path(value).expanduser() for value in LEGACY_LEDGERS]
    state_home = Path(_env("XDG_STATE_HOME") or "~/.local/state").expanduser()
    return migrate_legacy_ledgers(state_home / "plane-mcp-karval" / "mutations.json", legacy_paths)


def _workspace(value: str | None = None) -> str:
    workspace = str(value or "").strip() or _default_workspace()
    if not workspace:
        raise RuntimeError("workspace_slug is required; set PLANE_WORKSPACE_SLUG or pass it explicitly")
    return workspace


def _clean_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items() if item is not None}


def _payload_shape(value: Mapping[str, Any] | None) -> dict[str, str]:
    """Describe input fields without reflecting potentially sensitive values."""
    return {
        str(key): type(item).__name__
        for key, item in dict(value or {}).items()
    }


def _derived_idempotency_key(value: str, suffix: str) -> str:
    key = str(value or "").strip()
    if not 16 <= len(key) <= 190:
        raise ValueError("lifecycle idempotency_key must contain 16 to 190 characters")
    return f"{key}:{suffix}"


def _state_id(item: Mapping[str, Any]) -> str:
    state = item.get("state")
    if isinstance(state, Mapping):
        return str(state.get("id") or "").strip()
    return str(state or "").strip()


def _catalog_payload(
    *,
    query: str | None = None,
    surface: str | None = None,
    method: Method | None = None,
    mode: Mode | None = None,
    mutation: bool | None = None,
    group: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
) -> dict[str, Any]:
    catalog = list_operations(
        query=query,
        surface=surface,
        method=method,
        mode=mode,
        mutation=mutation,
        group=group,
        limit=limit,
        cursor=cursor,
        cursor_secret=(CONTRACT_HASH * 2)[:64],
        cursor_key_id="plane-mcp-karval",
    )
    catalog["server"] = {
        "name": "plane-mcp-karval",
        "contract_version": CONTRACT_VERSION,
        "contract_hash": CONTRACT_HASH,
        "operation_count": OPERATION_COUNT,
        "http_operation_count": HTTP_OPERATION_COUNT,
        "mutation_count": MUTATION_COUNT,
        "method_counts": METHOD_COUNTS,
    }
    return catalog


def _mutation_receipt(
    *,
    operation: str,
    method: str,
    path_params: Mapping[str, Any],
    payload: Mapping[str, Any],
    attempts: int,
    idempotency_key: str,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _client().execute_native_mutation_action(
        operation,
        path_params=_clean_mapping(path_params),
        query=_clean_mapping(query),
        payload=dict(payload or {}),
        idempotency_key=idempotency_key,
        attempts=attempts,
        ledger_path=_ledger_path(),
    )


def create_server() -> FastMCP:
    """Create the Plane MCP server without starting a transport."""

    mcp = FastMCP(
        "plane-mcp-karval",
        version=installed_version("plane-mcp-karval"),
        instructions=(
            "Use plane_catalog to discover the full Plane registry. Use "
            "plane_read_action for registered GET actions. Use "
            "plane_mutation_action with a caller-supplied idempotency key for "
            "one registry mutation. Descriptors and semantic helpers are optional. "
            "Local stdio is the caller authority boundary; this server does not "
            "manufacture human approval."
        ),
        strict_input_validation=True,
    )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_catalog(
        query: str | None = None,
        surface: str | None = None,
        method: Method | None = None,
        mode: Mode | None = None,
        mutation: bool | None = None,
        group: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """List the bounded full Plane operation catalog."""

        return _catalog_payload(
            query=query,
            surface=surface,
            method=method,
            mode=mode,
            mutation=mutation,
            group=group,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_action_descriptor(
        operation: str,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Describe a registered Plane action without calling the provider."""

        op = get_operation(operation)
        return {
            "dry_run": True,
            "would_mutate": op.mutation,
            "action": op.action,
            "method": op.method,
            "path_template": op.path,
            "path_params": _clean_mapping(path_params),
            "query": _clean_mapping(query),
            "payload_shape": _payload_shape(payload),
            "request_schema": op.request_schema,
            "provider_configuration": "unavailable_without_a_configured_client",
            "execution": "trusted_local_stdio_single_attempt_no_automatic_retry",
            "docs_path": op.docs_path,
            "docs_url": f"https://developers.plane.so{op.docs_path}",
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def plane_read_action(
        operation: str,
        path_params: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        """Execute any registered read-only Plane GET action."""

        op = get_operation(operation)
        if op.method != "GET" or op.mutation:
            raise PermissionError("plane_read_action accepts only registered GET operations")
        return _client().execute_read_action(
            operation,
            path_params=_clean_mapping(path_params),
            query=_clean_mapping(query),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def plane_capture_state_catalog(workspace_slug: str, project_id: str) -> dict[str, Any]:
        """Read a project's current states without changing them."""
        response = _client().execute_read_action(
            "state__list_states",
            path_params={"workspace_slug": workspace_slug, "project_id": project_id},
            query={},
        )
        return {
            "workspace_slug": workspace_slug,
            "project_id": project_id,
            "source_action": "state__list_states",
            "states": response.get("results", []) if isinstance(response, Mapping) else [],
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def plane_lifecycle_transition(
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        idempotency_key: str,
        target_state_id: str | None = None,
        expected_state_id: str | None = None,
        expected_updated_at: str | None = None,
        comment: dict[str, Any] | None = None,
        attempts: int = 1,
    ) -> dict[str, Any]:
        """Optionally change state and add a comment with explicit partial results.

        This is a convenience workflow. Generic state PATCH and comments remain
        independently available through the registry dispatcher.
        """
        if attempts != 1:
            raise ValueError("exactly one lifecycle attempt is allowed")
        if not str(target_state_id or "").strip() and comment is None:
            raise ValueError("target_state_id or comment is required")
        client = _client()
        before = client.get_work_item(workspace_slug, project_id, work_item_id)
        if not isinstance(before, Mapping):
            raise RuntimeError("Plane lifecycle pre-read returned an invalid work item")
        actual_state = _state_id(before)
        actual_updated_at = str(before.get("updated_at") or "")
        if expected_state_id is not None and str(expected_state_id) != actual_state:
            return {"status": "precondition_failed", "reason": "state", "state": actual_state}
        if expected_updated_at is not None and str(expected_updated_at) != actual_updated_at:
            return {"status": "precondition_failed", "reason": "updated_at", "updated_at": actual_updated_at}

        state_receipt: dict[str, Any] | None = None
        if str(target_state_id or "").strip():
            state_receipt = _mutation_receipt(
                operation="issue__update_issue_detail",
                method="PATCH",
                path_params={"workspace_slug": workspace_slug, "project_id": project_id, "resource_id": work_item_id},
                payload={"state": str(target_state_id)},
                idempotency_key=_derived_idempotency_key(idempotency_key, "state"),
                attempts=attempts,
            )
            if state_receipt.get("readback_verified") is not True:
                return {"status": "state_applied_comment_pending", "state": state_receipt, "comment": None}

        comment_receipt: dict[str, Any] | None = None
        if comment is not None:
            comment_receipt = _mutation_receipt(
                operation="issue_comment__add_issue_comment",
                method="POST",
                path_params={"workspace_slug": workspace_slug, "project_id": project_id, "work_item_id": work_item_id},
                payload={"comment_html": render_comment(comment, output_format="html"), "external_source": "plane-mcp-karval"},
                idempotency_key=_derived_idempotency_key(idempotency_key, "comment"),
                attempts=attempts,
            )
            if comment_receipt.get("readback_verified") is not True:
                return {"status": "comment_pending_reconciliation", "state": state_receipt, "comment": comment_receipt}
        return {
            "status": "state_verified" if state_receipt is not None else "comment_verified",
            "state": state_receipt,
            "comment": comment_receipt,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def plane_reconcile_mutation(
        operation: str,
        path_params: dict[str, Any],
        payload: dict[str, Any] | None,
        idempotency_key: str,
        query: dict[str, Any] | None = None,
        attempts: int = 1,
    ) -> dict[str, Any]:
        """Refresh only a recorded mutation's readback; never replay its write."""
        return _client().reconcile_native_mutation(
            operation,
            path_params=_clean_mapping(path_params),
            query=_clean_mapping(query),
            payload=_clean_mapping(payload),
            idempotency_key=idempotency_key,
            attempts=attempts,
            ledger_path=_ledger_path(),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_validate_work_item_contract(document: dict[str, Any]) -> dict[str, Any]:
        """Validate a governed Plane lifecycle artifact without provider calls."""

        errors = validate_document(document)
        return {
            "valid": not errors,
            "errors": errors,
            "contract_version": WORK_ITEM_CONTRACT_VERSION,
            "timing_metrics": calculate_timing_metrics(document) if isinstance(document, dict) else None,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_render_lifecycle_comment(
        comment: dict[str, Any],
        output_format: Literal["html", "markdown"] = "html",
    ) -> dict[str, Any]:
        """Render a complete START/PROGRESS/BLOCKED/REVIEW/FINISH comment."""

        body = render_comment(comment, output_format=output_format)
        return {"body": body, "body_format": output_format}

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def plane_add_comment(
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        comment_html: str,
        idempotency_key: str,
        attempts: int = 1,
        external_source: str = "plane-mcp-karval",
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Append one ordinary HTML comment with idempotency and provider readback."""
        if not str(comment_html or "").strip():
            raise ValueError("comment_html is required")
        return _mutation_receipt(
            operation="issue_comment__add_issue_comment",
            method="POST",
            path_params={"workspace_slug": workspace_slug, "project_id": project_id, "work_item_id": work_item_id},
            payload=_clean_mapping({"comment_html": comment_html, "external_source": external_source, "external_id": external_id}),
            idempotency_key=idempotency_key,
            attempts=attempts,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def plane_add_lifecycle_comment(
        project_id: str,
        work_item_id: str,
        comment: dict[str, Any],
        idempotency_key: str,
        attempts: int = 1,
        workspace_slug: str | None = None,
        external_source: str = "plane-mcp-karval",
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Render and persist one governed lifecycle comment with provider readback."""

        workspace = _workspace(workspace_slug)
        body = render_comment(comment, output_format="html")
        return _mutation_receipt(
            operation="issue_comment__add_issue_comment",
            method="POST",
            path_params={"workspace_slug": workspace, "project_id": project_id, "work_item_id": work_item_id},
            payload=_clean_mapping({"comment_html": body, "external_source": external_source, "external_id": external_id}),
            idempotency_key=idempotency_key,
            attempts=attempts,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_validate_title_contract(
        title: str,
        labels: list[str],
        title_context: str,
        qualifier: str | None = None,
        frozen_snapshot: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Validate canonical Plane title/icon semantics without provider calls."""

        result = validate_title(title, labels, title_context, qualifier)
        snapshot_errors = (
            validate_frozen_snapshot(title, labels, title_context, frozen_snapshot, qualifier)
            if frozen_snapshot is not None
            else []
        )
        descriptor = presentation_descriptor(labels, qualifier) if result.expected or result.valid else None
        return {
            "valid": result.valid and not snapshot_errors,
            "errors": list(result.errors) + snapshot_errors,
            "expected": result.expected,
            "semantic_icon_id": semantic_icon_id(labels, qualifier) if descriptor else None,
            "presentation": descriptor,
            "icon_contract_version": ICON_CONTRACT_VERSION,
            "presentation_contract_version": PRESENTATION_CONTRACT_VERSION,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_normalize_title(
        title: str,
        labels: list[str],
        title_context: str,
        qualifier: str | None = None,
    ) -> dict[str, Any]:
        """Normalize a proposed Plane title according to the semantic icon contract."""

        normalized = normalize_title(title, labels, title_context, qualifier)
        return {
            "title": normalized,
            "semantic_icon_id": semantic_icon_id(labels, qualifier),
            "presentation": presentation_descriptor(labels, qualifier),
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        )
    )
    def plane_prepare_issue_creation(
        payload: dict[str, Any],
        issue_readiness: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Optionally validate issue readiness and return its advisory fingerprint.

        This provider-free helper never creates an issue and is not a required
        predecessor of ``plane_mutation_action``.
        """

        errors = validate_issue_creation_readiness(issue_readiness, payload)
        return {
            "valid": not errors,
            "errors": errors,
            "issue_readiness_fingerprint": (
                readiness_fingerprint(issue_readiness or {}) if not errors else None
            ),
            "issue_readiness_contract_version": ISSUE_READINESS_CONTRACT_VERSION,
        }

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def plane_mutation_action(
        operation: str,
        path_params: dict[str, Any],
        payload: dict[str, Any] | None,
        idempotency_key: str,
        attempts: int = 1,
        query: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute one registry mutation for the trusted local stdio caller.

        State PATCH has normal non-atomic provider semantics: there is no CAS,
        automatic compensation, or automatic retry.
        """

        op = get_operation(operation)
        if op.method not in {"POST", "PATCH", "DELETE"} or not op.mutation:
            raise PermissionError("plane_mutation_action accepts only registered mutation operations")
        return _mutation_receipt(
            operation=operation,
            method=op.method,
            path_params=path_params,
            query=query,
            payload=payload or {},
            attempts=attempts,
            idempotency_key=idempotency_key,
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def plane_reconcile_legacy_module_archive_attempt(
        workspace_slug: str,
        project_id: str,
        resource_id: str,
        key_hash: str,
        approved_live_reconciliation: bool,
        authorization_receipt: dict[str, Any],
        attempts: int,
    ) -> dict[str, Any]:
        """Append read-only provider evidence for one exact legacy archive attempt.

        This never replays the archive.  It is intentionally restricted to the
        historical module archive body defect and requires explicit approval
        bound to the original receipt, contract, target, and evidence.
        """

        return _client().reconcile_legacy_module_archive_attempt(
            workspace_slug=workspace_slug,
            project_id=project_id,
            resource_id=resource_id,
            key_hash=key_hash,
            attempts=attempts,
            approved_live_reconciliation=approved_live_reconciliation,
            authorization_receipt=_clean_mapping(authorization_receipt),
            ledger_path=_ledger_path(),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        )
    )
    def plane_reconcile_module_update_attempt(
        workspace_slug: str,
        project_id: str,
        resource_id: str,
        key_hash: str,
        expected_payload: dict[str, Any],
        approved_live_reconciliation: bool,
        authorization_receipt: dict[str, Any],
        attempts: int,
    ) -> dict[str, Any]:
        """Append exact provider readback for one applied module update receipt."""

        return _client().reconcile_module_update_attempt(
            workspace_slug=workspace_slug,
            project_id=project_id,
            resource_id=resource_id,
            key_hash=key_hash,
            expected_payload=_clean_mapping(expected_payload),
            attempts=attempts,
            approved_live_reconciliation=approved_live_reconciliation,
            authorization_receipt=_clean_mapping(authorization_receipt),
            ledger_path=_ledger_path(),
        )

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def get_current_user() -> Any:
        """Return the current Plane user."""

        return _client().get_current_user()

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def list_projects(
        workspace_slug: str | None = None,
        cursor: str | None = None,
        per_page: int | None = None,
        order_by: str | None = None,
    ) -> Any:
        """List projects through the documented /projects endpoint."""

        query = _clean_mapping({"cursor": cursor, "per_page": per_page, "order_by": order_by})
        return _client().list_projects(_workspace(workspace_slug), **query)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def list_work_items(
        project_id: str,
        workspace_slug: str | None = None,
        cursor: str | None = None,
        per_page: int | None = None,
        order_by: str | None = None,
        state: str | None = None,
        assignees: str | None = None,
    ) -> Any:
        """List work items in one Plane project."""

        query = _clean_mapping(
            {
                "cursor": cursor,
                "per_page": per_page,
                "order_by": order_by,
                "state": state,
                "assignees": assignees,
            }
        )
        return _client().list_work_items(_workspace(workspace_slug), project_id, **query)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def get_work_item(
        project_id: str,
        work_item_id: str,
        workspace_slug: str | None = None,
    ) -> Any:
        """Get one Plane work item and include its direct web URL."""

        return _client().get_work_item(_workspace(workspace_slug), project_id, work_item_id)

    @mcp.tool(
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )
    def search_work_items(
        search: str,
        workspace_slug: str | None = None,
        cursor: str | None = None,
        per_page: int | None = None,
    ) -> Any:
        """Search Plane work items in the workspace."""

        query = _clean_mapping({"search": search, "cursor": cursor, "per_page": per_page})
        return _client().search_work_items(_workspace(workspace_slug), **query)

    return mcp


__all__ = ["create_server"]
