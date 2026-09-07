"""Deterministic readiness gate for new Plane work-item creation."""

from __future__ import annotations

import hashlib
import json
from typing import Any


CONTRACT_VERSION = 1
REQUIRED_TEXT = ("objective", "context", "owner_disposition", "priority_rationale")
REQUIRED_LISTS = ("scope", "non_goals", "acceptance_criteria", "dependencies", "validation_plan", "execution_units")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _nonempty_strings(value: Any) -> bool:
    return isinstance(value, list) and all(_text(item) for item in value) and bool(value)


def validate_issue_creation_readiness(readiness: Any, payload: dict[str, Any]) -> list[str]:
    """Return named errors for an issue create request; never inspect secrets."""

    if not isinstance(readiness, dict):
        return ["issue_readiness is required for issue__add_issue"]
    errors: list[str] = []
    if readiness.get("contract_version") != CONTRACT_VERSION:
        errors.append(f"issue_readiness.contract_version must equal {CONTRACT_VERSION}")
    for field in REQUIRED_TEXT:
        if not _text(readiness.get(field)):
            errors.append(f"issue_readiness.{field} is required")
    for field in REQUIRED_LISTS:
        if field == "non_goals":
            if not isinstance(readiness.get(field), list):
                errors.append("issue_readiness.non_goals must be a list, even when empty")
        elif not _nonempty_strings(readiness.get(field)):
            errors.append(f"issue_readiness.{field} must contain at least one bounded item")
    if len(_text(payload.get("name"))) < 12:
        errors.append("payload.name must contain a concrete issue title")
    if len(_text(payload.get("description_html"))) < 160:
        errors.append("payload.description_html must contain an execution-ready body")
    return errors


def readiness_fingerprint(readiness: dict[str, Any]) -> str:
    canonical = json.dumps(readiness, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
