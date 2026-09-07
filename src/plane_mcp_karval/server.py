"""Karval-owned Plane MCP server.

The public MCP surface is intentionally small and registry-first.  Full Plane
coverage lives in the checked-in operation registry, not in a dependency on the
upstream Plane MCP package.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
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

DEFAULT_LEDGER = "~/.codex/state/plane-mcp-karval-mutations.json"


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


def _workspace(value: str | None = None) -> str:
    workspace = str(value or "").strip() or _default_workspace()
    if not workspace:
        raise RuntimeError("workspace_slug is required; set PLANE_WORKSPACE_SLUG or pass it explicitly")
    return workspace


def _clean_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return {str(key): item for key, item in dict(value or {}).items() if item is not None}


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
    approved_live_mutation: bool,
    authorization_receipt: Mapping[str, Any],
    attempts: int,
    idempotency_key: str,
    query: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return _client().execute_mutation_action(
        operation,
        path_params=_clean_mapping(path_params),
        query=_clean_mapping(query),
        payload=_clean_mapping(payload),
        approved_live_mutation=approved_live_mutation,
        authorization_receipt=_clean_mapping(authorization_receipt),
        idempotency_key=idempotency_key,
        attempts=attempts,
        ledger_path=Path(DEFAULT_LEDGER).expanduser(),
    )


def create_server() -> FastMCP:
    """Create the Plane MCP server without starting a transport."""

    mcp = FastMCP(
        "plane-mcp-karval",
        version="1.0.0",
        instructions=(
            "Use plane_catalog to discover the full Plane registry. Use "
            "plane_read_action for registered GET actions. Use "
            "plane_action_descriptor before any mutation and plane_mutation_action "
            "only after one explicit operation/target approval."
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

        return _client().operation_descriptor(
            operation,
            path_params=_clean_mapping(path_params),
            query=_clean_mapping(query),
            payload=_clean_mapping(payload),
        )

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
    def plane_add_lifecycle_comment(
        project_id: str,
        work_item_id: str,
        comment: dict[str, Any],
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        authorized_work_item_id: str,
        authorization_receipt: dict[str, Any],
        idempotency_key: str,
        attempts: int = 1,
        workspace_slug: str | None = None,
        external_source: str = "plane-mcp-karval",
        external_id: str | None = None,
    ) -> dict[str, Any]:
        """Render and persist one governed lifecycle comment with provider readback."""

        workspace = _workspace(workspace_slug)
        body = render_comment(comment, output_format="html")
        return _client().governed_add_work_item_comment(
            workspace_slug=workspace,
            project_id=project_id,
            work_item_id=work_item_id,
            payload=_clean_mapping(
                {
                    "comment_html": body,
                    "external_source": external_source,
                    "external_id": external_id,
                }
            ),
            approved_live_mutation=approved_live_mutation,
            authorization_scope=authorization_scope,
            authorized_workspace_slug=authorized_workspace_slug,
            authorized_project_id=authorized_project_id,
            authorized_work_item_id=authorized_work_item_id,
            authorization_receipt=_clean_mapping(authorization_receipt),
            idempotency_key=idempotency_key,
            attempts=attempts,
            ledger_path=Path(DEFAULT_LEDGER).expanduser(),
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
        """Validate issue readiness and return the canonical mutation receipt hash.

        This is deliberately provider-free: it gives callers the exact
        ``issue_readiness_fingerprint`` required by ``plane_mutation_action``
        without exposing a second route for issue creation.
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
        approved_live_mutation: bool,
        authorization_receipt: dict[str, Any],
        idempotency_key: str,
        attempts: int = 1,
        query: dict[str, Any] | None = None,
        issue_readiness: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute any registered Plane mutation with strict approval and readback."""

        op = get_operation(operation)
        if op.method not in {"POST", "PATCH", "DELETE"} or not op.mutation:
            raise PermissionError("plane_mutation_action accepts only registered mutation operations")
        if operation == "issue__add_issue":
            errors = validate_issue_creation_readiness(issue_readiness, payload or {})
            if errors:
                raise PermissionError("issue creation readiness failed: " + "; ".join(errors))
            if str(authorization_receipt.get("issue_readiness_fingerprint") or "").strip() != readiness_fingerprint(issue_readiness or {}):
                raise PermissionError("authorization receipt issue_readiness_fingerprint does not match")
        return _mutation_receipt(
            operation=operation,
            method=op.method,
            path_params=path_params,
            query=query,
            payload=payload or {},
            approved_live_mutation=approved_live_mutation,
            authorization_receipt=authorization_receipt,
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
            ledger_path=Path(DEFAULT_LEDGER).expanduser(),
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
            ledger_path=Path(DEFAULT_LEDGER).expanduser(),
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
