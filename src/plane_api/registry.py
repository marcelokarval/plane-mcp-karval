"""Documented Plane API operation registry."""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hmac
import json
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .manifest import CONTRACT_HASH, CONTRACT_VERSION, MUTATION_COUNT


@dataclass(frozen=True, slots=True)
class Operation:
    action: str
    method: str
    path: str
    summary: str
    surface: str
    mode: str
    mutation: bool
    docs_path: str
    normalized_graph_key: str = ""
    frontmatter: dict[str, Any] = field(default_factory=dict)
    request_schema: dict[str, Any] = field(default_factory=dict)
    response_schema: dict[str, Any] = field(default_factory=dict)
    effect: dict[str, Any] = field(default_factory=dict)
    schema_availability: dict[str, Any] = field(default_factory=dict)
    schema_provenance: dict[str, Any] = field(default_factory=dict)
    semantic_provenance: dict[str, Any] = field(default_factory=dict)
    schema_hash: str = ""


_PAYLOAD = json.loads((Path(__file__).with_name("operation_registry.json")).read_text())
OPERATIONS = tuple(Operation(**row) for row in _PAYLOAD["operations"])
ACTIONS = {operation.action: operation for operation in OPERATIONS}
OPERATION_COUNT = int(_PAYLOAD["operation_count"])
HTTP_OPERATION_COUNT = int(_PAYLOAD["http_operation_count"])
METHOD_COUNTS = dict(_PAYLOAD["method_counts"])


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


MUTATION_CONTRACTS = MappingProxyType(
    {
        str(contract["action"]): _freeze(contract)
        for contract in _PAYLOAD.get("mutation_contracts", [])
    }
)

if len(ACTIONS) != OPERATION_COUNT:
    raise RuntimeError("Plane action registry contains duplicate action names")
if len(MUTATION_CONTRACTS) != MUTATION_COUNT:
    raise RuntimeError("Plane mutation registry denominator does not match the manifest")


def get_operation(action: str) -> Operation:
    try:
        return ACTIONS[str(action)]
    except KeyError as exc:
        raise ValueError(f"Unknown Plane action: {action}") from exc


def get_mutation_contract(action: str):
    """Return the immutable postcondition contract for a mutation action."""
    try:
        return MUTATION_CONTRACTS[str(action)]
    except KeyError as exc:
        raise ValueError(f"Unknown Plane mutation contract: {action}") from exc


_CURSOR_MAX_ACTIONS = 50
_CURSOR_MAX_BYTES = 32 * 1024
_CURSOR_MIN_KEY_BYTES = 32


def _cursor_key(secret: str | bytes) -> bytes:
    if isinstance(secret, str):
        key = secret.encode("utf-8")
    else:
        try:
            key = bytes(secret)
        except (TypeError, ValueError) as exc:
            raise ValueError("cursor secret must be bytes or text") from exc
    if len(key) < _CURSOR_MIN_KEY_BYTES:
        raise ValueError("cursor secret must be at least 32 bytes")
    return key


def _cursor_signature(body: dict[str, Any], secret: str | bytes) -> str:
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    key = _cursor_key(secret)
    return hmac.new(key, canonical.encode("utf-8"), "sha256").hexdigest()


def _encode_cursor(*, filters: dict[str, Any], limit: int, offset: int, secret: str | bytes, key_id: str) -> str:
    body = {
        "contract_version": CONTRACT_VERSION,
        "manifest_hash": CONTRACT_HASH,
        "query": filters["query"],
        "surface": filters["surface"],
        "method": filters["method"],
        "mode": filters["mode"],
        "mutation": filters["mutation"],
        "group": filters["group"],
        "limit": limit,
        "offset": offset,
        "key_id": key_id,
    }
    envelope = {**body, "signature": _cursor_signature(body, secret)}
    encoded = base64.urlsafe_b64encode(
        json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )
    return encoded.decode("ascii").rstrip("=")


def _decode_cursor(cursor: str, *, secret: str | bytes, key_id: str) -> dict[str, Any]:
    if not isinstance(cursor, str) or not cursor or len(cursor) > 4096:
        raise ValueError("cursor is malformed")
    try:
        if any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in cursor):
            raise ValueError
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        envelope = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("cursor is malformed") from exc
    if not isinstance(envelope, dict):
        raise ValueError("cursor is malformed")
    expected_keys = {
        "contract_version", "manifest_hash", "query", "surface", "method", "mode",
        "mutation", "group", "limit", "offset", "key_id", "signature",
    }
    if set(envelope) != expected_keys:
        raise ValueError("cursor is malformed")
    body = {key: envelope[key] for key in expected_keys - {"signature"}}
    if envelope["contract_version"] != CONTRACT_VERSION or envelope["manifest_hash"] != CONTRACT_HASH:
        raise ValueError("cursor manifest mismatch")
    if envelope["key_id"] != key_id or not isinstance(envelope["signature"], str):
        raise ValueError("cursor is malformed")
    expected_signature = _cursor_signature(body, secret)
    if not hmac.compare_digest(envelope["signature"], expected_signature):
        raise ValueError("cursor signature mismatch")
    if not isinstance(envelope["limit"], int) or isinstance(envelope["limit"], bool) or not 1 <= envelope["limit"] <= _CURSOR_MAX_ACTIONS:
        raise ValueError("cursor is malformed")
    if not isinstance(envelope["offset"], int) or isinstance(envelope["offset"], bool) or envelope["offset"] < 0:
        raise ValueError("cursor is malformed")
    if envelope["offset"] > 10000:
        raise ValueError("cursor is out of range")
    if envelope["mode"] not in (None, "read", "descriptor"):
        raise ValueError("cursor is malformed")
    if envelope["mutation"] not in (None, True, False):
        raise ValueError("cursor is malformed")
    if envelope["method"] is not None and envelope["method"] not in {"GET", "POST", "PATCH", "DELETE"}:
        raise ValueError("cursor is malformed")
    return envelope


def list_operations(
    *,
    query: str | None = None,
    mode: str | None = None,
    surface: str | None = None,
    method: str | None = None,
    mutation: bool | None = None,
    group: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    cursor_secret: str | bytes | None = None,
    cursor_key_id: str = "caller",
) -> dict[str, Any]:
    if mode not in (None, "read", "descriptor"):
        raise ValueError("mode must be read or descriptor")
    key_id = str(cursor_key_id or "").strip()
    if not key_id or len(key_id) > 64:
        raise ValueError("cursor key id is malformed")
    if cursor_secret is not None:
        _cursor_key(cursor_secret)
    decoded: dict[str, Any] | None = None
    if cursor is not None:
        if cursor_secret is None or cursor_secret == "":
            raise ValueError("cursor secret is required for pagination")
        decoded = _decode_cursor(cursor, secret=cursor_secret, key_id=key_id)
    cursor_filters: dict[str, Any] | None = None
    if decoded is not None:
        cursor_filters = {
            name: decoded[name]
            for name in ("query", "surface", "method", "mode", "mutation", "group")
        }
    if decoded is not None:
        if cursor_filters is None:  # pragma: no cover - coupled construction guard
            raise ValueError("cursor is malformed")
        expected_filter_keys = {"query", "mode", "surface", "method", "mutation", "group"}
        if set(cursor_filters) != expected_filter_keys:
            raise ValueError("cursor is malformed")
        if cursor_filters["mode"] not in (None, "read", "descriptor"):
            raise ValueError("cursor is malformed")
        if cursor_filters["mutation"] not in (None, True, False):
            raise ValueError("cursor is malformed")
        if cursor_filters["method"] is not None and cursor_filters["method"] not in {
            "GET",
            "POST",
            "PATCH",
            "DELETE",
        }:
            raise ValueError("cursor is malformed")
        supplied_filters = {
            "query": str(query or "").casefold() if query is not None else None,
            "mode": mode,
            "surface": surface,
            "method": str(method).upper() if method is not None else None,
            "mutation": mutation,
            "group": group,
        }
        for name, supplied in supplied_filters.items():
            if supplied is not None and supplied != cursor_filters[name]:
                raise ValueError("cursor filter mismatch")
        filters = dict(cursor_filters)
        requested_limit = decoded["limit"] if limit is None else int(limit)
        if not 1 <= requested_limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        effective_limit = min(requested_limit, _CURSOR_MAX_ACTIONS)
        if effective_limit != decoded["limit"]:
            raise ValueError("cursor limit mismatch")
        offset = decoded["offset"]
    else:
        filters = {
            "query": str(query or "").casefold(),
            "mode": mode,
            "surface": surface,
            "method": str(method).upper() if method is not None else None,
            "mutation": mutation,
            "group": group,
        }
        requested_limit = 100 if limit is None else int(limit)
        offset = 0
    if not 1 <= requested_limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    # The public catalog contract is bounded regardless of filters, mutation
    # mode, or whether the caller supplied a cursor secret.  Requests above the
    # bound are lowered deterministically so wrappers with historical defaults
    # cannot accidentally expose the complete registry.
    effective_limit = min(requested_limit, _CURSOR_MAX_ACTIONS)
    needle = filters["query"]
    rows = [
        operation
        for operation in OPERATIONS
        if (not needle or needle in " ".join((operation.action, operation.summary, operation.path, operation.surface)).casefold())
        and (filters["mode"] is None or operation.mode == filters["mode"])
        and (filters["surface"] is None or operation.surface == filters["surface"])
        and (filters["method"] is None or operation.method == filters["method"])
        and (filters["mutation"] is None or operation.mutation is filters["mutation"])
        and (filters["group"] is None or operation.surface == filters["group"])
    ]
    if decoded and offset >= len(rows):
        raise ValueError("cursor is out of range")
    page = rows[offset : offset + effective_limit]
    if not page and decoded:
        raise ValueError("cursor is out of range")
    next_offset = offset + len(page)
    def make_result(current_page, continuation):
        return {
        "total": len(rows),
        "returned": len(current_page),
        "operation_denominator": OPERATION_COUNT,
        "unique_http_operations": HTTP_OPERATION_COUNT,
        "mutation_denominator": MUTATION_COUNT,
        "contract_version": CONTRACT_VERSION,
        "contract_hash": CONTRACT_HASH,
        "method_counts": dict(METHOD_COUNTS),
        "next_cursor": continuation,
        "actions": [
            {
                "action": operation.action,
                "method": operation.method,
                "path": operation.path,
                "summary": operation.summary,
                "surface": operation.surface,
                "mode": operation.mode,
                "docs_path": operation.docs_path,
                "mutation": operation.mutation,
                **(
                    {
                        "mutation_metadata": {
                            "contract_action": operation.action,
                            "postcondition": {
                                "kind": get_mutation_contract(operation.action)["postcondition"]["kind"],
                                "verification": get_mutation_contract(operation.action)["postcondition"]["verification"],
                            },
                        }
                    }
                    if operation.mutation
                    else {}
                ),
            }
            for operation in current_page
        ],
        }

    def continuation(next_page_offset: int, current_page: list[Operation]) -> str | None:
        if next_page_offset >= len(rows):
            return None
        if cursor_secret is None:
            if mutation is True:
                raise ValueError("cursor secret is required for pagination")
            # Read-only/catalog callers without a cursor key receive one
            # bounded, deliberately non-continuable page.  A caller that
            # supplies a cursor (or requests the mutation catalog) still
            # fails closed above rather than creating an unsigned cursor.
            return None
        return _encode_cursor(
            filters=filters,
            limit=len(current_page),
            offset=next_page_offset,
            secret=cursor_secret,
            key_id=key_id,
        )

    next_cursor = continuation(next_offset, page)
    result = make_result(page, next_cursor)
    # The page size is shortened from the end, deterministically, until both
    # the action and envelope limits hold.  The continuation binds the actual
    # shortened page length, so it cannot skip or duplicate a row.
    while len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _CURSOR_MAX_BYTES and len(page) > 1:
        page = page[:-1]
        next_offset = offset + len(page)
        next_cursor = continuation(next_offset, page)
        result = make_result(page, next_cursor)
    if len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > _CURSOR_MAX_BYTES:
        raise ValueError("operation page exceeds the 32 KiB public bound")
    return result
