"""Safe-by-default Plane client backed by the official documented API."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen

from .registry import get_mutation_contract, get_operation, list_operations
from .manifest import CONTRACT_HASH, CONTRACT_VERSION


_GOVERNED_SCOPE = "one_work_item_one_operation"
_CREATE_FIELDS = {
    "assignees",
    "labels",
    "type_id",
    "point",
    "name",
    "description_html",
    "description_stripped",
    "priority",
    "start_date",
    "target_date",
    "is_draft",
    "external_source",
    "external_id",
    "parent",
    "state",
    "estimate_point",
}
_UPDATE_FIELDS = set(_CREATE_FIELDS)
_COMMENT_FIELDS = {"comment_html", "external_id", "external_source"}
_READBACK_SCALARS = {
    "name",
    "type_id",
    "point",
    "priority",
    "start_date",
    "target_date",
    "is_draft",
    "external_source",
    "external_id",
    "parent",
    "state",
    "estimate_point",
}
_MAX_READBACK_PAGES = 100
_MAX_READBACK_ROWS = 10_000


class PlaneResponseValidationError(RuntimeError):
    """A provider response had the expected HTTP status but invalid JSON shape."""


class PlaneReadbackValidationError(RuntimeError):
    """A provider readback cannot prove the affected resource identity/effect."""


class PlaneProviderRequestError(RuntimeError):
    """Bounded transport failure information safe to persist in mutation receipts."""

    def __init__(
        self,
        *,
        phase: str,
        http_status: int | None,
        transport_class: str,
        deterministic: bool,
        detail: Mapping[str, Any],
    ) -> None:
        self.phase = phase
        self.http_status = http_status
        self.transport_class = transport_class
        self.deterministic = deterministic
        self.detail = dict(detail)
        super().__init__("Plane provider request failed")


@dataclass(frozen=True, slots=True)
class PlaneConfig:
    base_url: str
    token: str = field(repr=False)
    instance_id: str = ""
    timeout: float = 20.0
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_mapping(cls, config: Mapping[str, Any]) -> "PlaneConfig":
        return cls(
            base_url=str(config["base_url"]),
            token=str(config.get("token") or ""),
            instance_id=str(config.get("instance_id") or ""),
            timeout=float(config.get("timeout", 20.0)),
            headers=dict(config.get("headers") or {}),
        )

    def redacted(self) -> dict[str, Any]:
        return {
            "base_url": self.base_url,
            "token": "[REDACTED]" if self.token else "[MISSING]",
            "instance_id": self.instance_id,
            "timeout": self.timeout,
        }


class PlaneClient:
    """Registry-validated Plane HTTP client with one-shot governed mutations."""

    def __init__(self, config: PlaneConfig | Mapping[str, Any], transport=None):
        if (
            os.environ.get("HERMES_TEST_TRANSPORT_GUARD") == "1"
            and transport is not None
            and getattr(transport, "__hermes_test_fake_transport__", False) is not True
        ):
            raise AssertionError(
                "test transport must be an explicit fake test transport"
            )
        self.config = config if isinstance(config, PlaneConfig) else PlaneConfig.from_mapping(config)
        self._transport = transport

    def headers(self) -> dict[str, str]:
        headers = dict(self.config.headers)
        if self.config.token:
            headers["X-API-Key"] = self.config.token
        return headers

    def official_sdk_status(self) -> dict[str, Any]:
        """Compatibility metadata for callers of the pre-HTTP-client catalog."""
        return {"transport": "stdlib_http", "dependency": None, "policy": "no_provider_sdk"}

    def list_operation_catalog(self, **filters: Any) -> dict[str, Any]:
        result = list_operations(**filters)
        result["official_sdk"] = self.official_sdk_status()
        return result

    def execute_read_action(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        operation = get_operation(action)
        if operation.mutation:
            raise PermissionError("Generic live Plane mutations are blocked; request an operation descriptor")
        return self._request("GET", _expand_path(operation.path, path_params or {}), query=query)

    def preflight_mutation(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None,
        payload: Mapping[str, Any] | None,
        approved_live_mutation: bool = False,
        authorization_receipt: Mapping[str, Any] | None = None,
        attempts: int = 1,
        idempotency_key: str = "",
        allow_runtime_state_transition: bool = False,
    ) -> dict[str, Any]:
        """Compatibility wrapper for the receipt-free trusted-local preflight.

        ``approved_live_mutation``, ``authorization_receipt`` and
        ``allow_runtime_state_transition`` remain accepted only so old direct
        callers do not fail at argument binding. They no longer carry authority
        or unlock a different Plane operation path.
        """
        return self.preflight_native_mutation(
            action,
            path_params=path_params,
            payload=payload,
            attempts=attempts,
            idempotency_key=idempotency_key,
        )


    def preflight_native_mutation(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None,
        payload: Mapping[str, Any] | None,
        attempts: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """Preflight a trusted local MCP caller's single Plane mutation.

        Stdio establishes the caller boundary.  This validates registry-bound
        request data and execution invariants, but deliberately does not claim
        that a human approved the operation.
        """
        operation = get_operation(action)
        if not operation.mutation:
            raise ValueError("preflight is only valid for POST, PATCH, or DELETE registry actions")
        contract = get_mutation_contract(action)
        if contract.get("write_policy") != "allow" or contract.get("expected_terminal") not in {"verified", "verified_absence", "provider_acknowledged"}:
            raise PermissionError(f"Plane mutation contract is not executable: {action}")
        if attempts != 1:
            raise ValueError("exactly one mutation attempt is allowed; automatic retries are disabled")
        key = str(idempotency_key or "").strip()
        if not 16 <= len(key) <= 200:
            raise ValueError("idempotency_key must contain 16 to 200 characters")
        params, body = dict(path_params or {}), dict(payload or {})
        if not _request_body_available(operation) and body:
            raise ValueError("registry operation does not permit a request body")
        if set(body) & set(params):
            raise ValueError("mutation payload must not override path parameters")
        body_schema = (operation.request_schema or {}).get("body") or {}
        documented_fields = {field["name"] for field in body_schema.get("parameters", [])}
        if documented_fields and set(body) - documented_fields:
            raise ValueError("mutation payload contains fields outside the registered body schema")
        path = _expand_path(operation.path, params)
        schema = (operation.request_schema or {}).get("json_schema")
        if isinstance(schema, Mapping):
            # Source docs sometimes retain a renamed path parameter beside the
            # real URI placeholder. It is metadata, not a second required input.
            path_fields = {field["name"] for field in (operation.request_schema or {}).get("path", {}).get("parameters", [])}
            uri_fields = set(re.findall(r"\{([^}]+)\}", operation.path))
            stale_path_fields = path_fields - uri_fields - documented_fields
            schema = {**schema, "required": [field for field in schema.get("required", []) if field not in stale_path_fields]}
            _validate_json_schema({**params, **body}, schema, context="mutation request")
        specification = contract["postcondition"]
        if specification.get("strategy") != "provider_ack":
            selector = (specification.get("targets") or {}).get("selector")
            if not isinstance(selector, Mapping) or not selector.get("source") or not selector.get("field"):
                raise ValueError("Plane mutation contract has no explicit affected-resource selector")
            source, field = str(selector["source"]), str(selector["field"])
            if source == "path" and not str(params.get(field) or "").strip():
                raise ValueError("Plane mutation target selector is empty")
            if source == "payload" and not _target_values(body.get(field)):
                raise ValueError("Plane mutation target selector is empty")
        return {
            "action": operation.action,
            "method": operation.method,
            "path": path,
            "payload_fingerprint": payload_fingerprint(body),
            "key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
        }

    def execute_native_mutation_action(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        attempts: int = 1,
        ledger_path: str | Path,
    ) -> dict[str, Any]:
        """Execute one trusted-local mutation without a synthetic approval receipt."""
        return self.execute_mutation_action(
            action,
            path_params=path_params,
            query=query,
            payload=payload,
            idempotency_key=idempotency_key,
            attempts=attempts,
            ledger_path=ledger_path,
            _trusted_local_client=True,
        )

    def reconcile_native_mutation(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        attempts: int = 1,
        ledger_path: str | Path,
    ) -> dict[str, Any]:
        """Refresh a failed readback without replaying its provider write."""
        operation = get_operation(action)
        params, body = dict(path_params or {}), dict(payload or {})
        preflight = self.preflight_native_mutation(
            action,
            path_params=params,
            payload=body,
            attempts=attempts,
            idempotency_key=idempotency_key,
        )
        key_hash = preflight["key_hash"]
        fingerprint = _fingerprint(
            action, "", "", None,
            {"path_params": params, "query": dict(query or {}), "payload": body},
        )
        store = _require_ledger_path(ledger_path)
        with _locked_ledger(store) as ledger:
            prior = ledger["receipts"].get(key_hash)
            if not isinstance(prior, Mapping):
                raise ValueError("idempotency_key has no local mutation receipt")
            if prior.get("provider_instance_hash") != _provider_instance_hash(self.config):
                raise PermissionError("idempotency_key belongs to a different Plane provider instance")
            if prior.get("fingerprint") != fingerprint:
                raise ValueError("idempotency_key was already used for a different mutation")
            status = str(prior.get("status") or "")
            prior_copy = dict(prior)

        if status == "attempt_started":
            return {
                "action": operation.action,
                "status": "outcome_unknown",
                "provider_mutation_applied": "unknown",
                "readback_verified": False,
                "replayed_write": False,
            }
        if status == "rejected_not_applied":
            return {
                "action": operation.action,
                "status": "rejected_not_applied",
                "provider_mutation_applied": False,
                "readback_verified": False,
                "replayed_write": False,
            }
        if status == "verified":
            return {
                "action": operation.action,
                "status": "reconciled_verified",
                "provider_mutation_applied": bool(prior_copy.get("mutation_applied", True)),
                "readback_verified": True,
                "replayed_write": False,
            }
        if status != "write_succeeded_readback_validation_failed":
            raise RuntimeError("mutation receipt has an unsupported reconciliation status")
        try:
            postcondition = self._evaluate_generic_postcondition(
                operation,
                params,
                prior_copy.get("provider_id"),
                query,
                response_metadata=prior_copy.get("response_metadata"),
                payload=body,
            )
        except (PlaneProviderRequestError, PlaneResponseValidationError, PlaneReadbackValidationError) as exc:
            return {
                "action": operation.action,
                "status": "reconciliation_inconclusive",
                "provider_mutation_applied": True,
                "readback_verified": False,
                "replayed_write": False,
                "error": {"class": type(exc).__name__, "code": _reconciliation_error_code(exc)},
            }
        with _locked_ledger(store) as ledger:
            _cas_receipt(ledger, key_hash, fingerprint, {"status": "verified", "postcondition": postcondition})
        return {
            "action": operation.action,
            "status": "reconciled_verified",
            "provider_mutation_applied": True,
            "readback_verified": True,
            "replayed_write": False,
        }

    def execute_mutation_action(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        approved_live_mutation: bool = False,
        authorization_receipt: Mapping[str, Any] | None = None,
        idempotency_key: str = "",
        attempts: int = 1,
        ledger_path: str | Path,
        allow_runtime_state_transition: bool = False,
        _trusted_local_client: bool = False,
    ) -> dict[str, Any]:
        """Execute one write; authorization and idempotency are independently one-shot.

        The ledger lock only protects state transitions. Provider I/O always occurs
        after the transition has been durably persisted and the lock released.
        """
        operation = get_operation(action)
        params, body = dict(path_params or {}), dict(payload or {})
        preflight = self.preflight_native_mutation(
            action,
            path_params=params,
            payload=body,
            attempts=attempts,
            idempotency_key=idempotency_key,
        )
        key_hash = preflight["key_hash"]
        fingerprint = _fingerprint(action, "", "", None, {"path_params": params, "query": dict(query or {}), "payload": body})
        store = _require_ledger_path(ledger_path)
        # Phase 1: atomically reserve the key before provider I/O.
        with _locked_ledger(store) as ledger:
            conflicts = ledger.get("migration_conflicts", {})
            conflicting_receipts = conflicts.get("receipts", {}) if isinstance(conflicts, Mapping) else {}
            if key_hash in conflicting_receipts:
                raise RuntimeError("idempotency_key collides across legacy mutation ledgers; manual reconciliation is required")
            prior = ledger["receipts"].get(key_hash)
            if prior:
                if _trusted_local_client and not str(prior.get("provider_instance_hash") or "").strip():
                    raise RuntimeError(
                        "existing mutation receipt lacks provider binding; explicit ledger migration is required"
                    )
                if _trusted_local_client and prior["provider_instance_hash"] != _provider_instance_hash(self.config):
                    raise PermissionError("idempotency_key belongs to a different Plane provider instance")
                if prior.get("fingerprint") != fingerprint:
                    raise ValueError("idempotency_key was already used for a different mutation")
                if prior.get("status") == "attempt_started":
                    raise RuntimeError("previous mutation attempt has unknown outcome; strict no-retry policy blocks replay")
                if prior.get("status") == "rejected_not_applied":
                    return _generic_receipt(
                        operation,
                        preflight["path"],
                        None,
                        prior["postcondition"],
                        mutation_applied=False,
                        duplicate=True,
                    )
                if prior.get("status") == "write_succeeded_readback_validation_failed":
                    if operation.action in {"module__update_module_detail", "issue__update_issue_detail"}:
                        duplicate_prior = dict(prior)
                    else:
                        return _generic_receipt(
                            operation,
                            preflight["path"],
                            prior.get("provider_id"),
                            prior["postcondition"],
                            mutation_applied=bool(prior.get("mutation_applied", True)),
                            duplicate=True,
                        )
                duplicate_prior = dict(prior)
            else:
                # Persist before request: ambiguous transport failures are never replayed.
                ledger["receipts"][key_hash] = {"action": action, "fingerprint": fingerprint, "status": "attempt_started", "method": operation.method, "path": preflight["path"], "key_hash": key_hash, "provider_instance_hash": _provider_instance_hash(self.config)}
                duplicate_prior = None

        if duplicate_prior is not None:
            # A duplicate gets fresh provider proof outside the lock.
            try:
                postcondition = self._evaluate_generic_postcondition(operation, params, duplicate_prior.get("provider_id"), query, response_metadata=duplicate_prior.get("response_metadata"), payload=body)
            except (PlaneProviderRequestError, PlaneResponseValidationError, PlaneReadbackValidationError) as exc:
                postcondition = _reconciliation_failure_postcondition(operation, exc)
                with _locked_ledger(store) as ledger:
                    _cas_receipt(ledger, key_hash, fingerprint, {"status": "write_succeeded_readback_validation_failed", "postcondition": postcondition})
                return _generic_receipt(operation, preflight["path"], duplicate_prior.get("provider_id"), postcondition, mutation_applied=bool(duplicate_prior.get("mutation_applied", True)), duplicate=True)
            with _locked_ledger(store) as ledger:
                _cas_receipt(ledger, key_hash, fingerprint, {"status": "verified", "postcondition": postcondition})
            return _generic_receipt(operation, preflight["path"], duplicate_prior.get("provider_id"), postcondition, mutation_applied=False, duplicate=True)

        try:
            response = self._request(operation.method, preflight["path"], query=query, payload=(body if _request_body_available(operation) else None), with_metadata=True, phase=f"write:{operation.action}")
        except PlaneProviderRequestError as exc:
            if not exc.deterministic:
                raise
            postcondition = _request_rejection_postcondition(operation, exc)
            with _locked_ledger(store) as ledger:
                _cas_receipt(ledger, key_hash, fingerprint, {"status": "rejected_not_applied", "postcondition": postcondition})
            return _generic_receipt(operation, preflight["path"], None, postcondition, mutation_applied=False, duplicate=False)

        contract_specification = get_mutation_contract(operation.action)["postcondition"]
        _validate_response_status(response, contract_specification.get("expected_status"), operation.response_schema, context="mutation")
        provider_id = _provider_id(response)
        response_metadata = _response_metadata(response, operation.response_schema)
        try:
            if operation.action == "module__update_module_detail":
                _validate_module_update_ack(_response_body(response), body)
            else:
                _validate_mutation_write_response(operation, response, body)
            if contract_specification.get("strategy") == "provider_ack" and operation.action != "module__update_module_detail":
                _validate_provider_acknowledgement(response, operation.response_schema, contract_specification)
        except PlaneResponseValidationError:
            postcondition = _response_validation_failure_postcondition(operation, phase="write_response")
            with _locked_ledger(store) as ledger:
                _cas_receipt(ledger, key_hash, fingerprint, {"provider_id": provider_id, "response_metadata": response_metadata, "status": "write_succeeded_readback_validation_failed", "mutation_applied": True, "postcondition": postcondition})
            return _generic_receipt(operation, preflight["path"], provider_id, postcondition, mutation_applied=True, duplicate=False)
        with _locked_ledger(store) as ledger:
            _cas_receipt(ledger, key_hash, fingerprint, {"provider_id": provider_id, "response_metadata": response_metadata, "status": "applied_pending_readback"})
        try:
            postcondition = self._evaluate_generic_postcondition(
                operation,
                params,
                provider_id,
                query,
                response=response,
                payload=body,
            )
        except (PlaneProviderRequestError, PlaneResponseValidationError, PlaneReadbackValidationError) as exc:
            postcondition = _reconciliation_failure_postcondition(operation, exc)
            with _locked_ledger(store) as ledger:
                _cas_receipt(ledger, key_hash, fingerprint, {
                        "status": "write_succeeded_readback_validation_failed",
                        "mutation_applied": True,
                        "postcondition": postcondition,
                    })
            return _generic_receipt(
                operation,
                preflight["path"],
                provider_id,
                postcondition,
                mutation_applied=True,
                duplicate=False,
            )
        with _locked_ledger(store) as ledger:
            _cas_receipt(ledger, key_hash, fingerprint, {"status": "verified", "postcondition": postcondition})
        return _generic_receipt(
            operation,
            preflight["path"],
            provider_id,
            postcondition,
            mutation_applied=True,
            duplicate=False,
        )

    def reconcile_legacy_module_archive_attempt(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        resource_id: str,
        key_hash: str,
        attempts: int,
        approved_live_reconciliation: bool,
        authorization_receipt: Mapping[str, Any] | None,
        ledger_path: str | Path,
    ) -> dict[str, Any]:
        """Append provider-backed no-effect evidence to one legacy archive attempt.

        This deliberately does not replay or otherwise mutate Plane.  It is a
        narrowly scoped repair for a receipt left at ``attempt_started`` by the
        historical body serialization defect.
        """
        action = "module__archive_module"
        operation = get_operation(action)
        params = {
            "workspace_slug": str(workspace_slug or "").strip(),
            "project_id": str(project_id or "").strip(),
            "resource_id": str(resource_id or "").strip(),
        }
        if not all(params.values()):
            raise ValueError("workspace_slug, project_id, and resource_id are required")
        if attempts != 1:
            raise ValueError("exactly one reconciliation attempt is allowed")
        if approved_live_reconciliation is not True:
            raise PermissionError("explicit live reconciliation approval is required")
        key_hash = str(key_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise ValueError("key_hash must be exactly 64 lowercase hexadecimal characters")
        expected_path = _expand_path(operation.path, params)
        expected_fingerprint = _fingerprint(
            action, "", "", None,
            {"path_params": params, "query": {}, "payload": {}},
        )
        expected_payload_fingerprint = payload_fingerprint({})
        provider_instance_hash = _provider_instance_hash(self.config)
        receipt = dict(authorization_receipt or {})
        authorization_id = str(receipt.get("authorization_id") or "").strip()
        if (
            receipt.get("action") != action
            or str(receipt.get("method") or "").upper() != "POST"
            or receipt.get("approved_live_reconciliation") is not True
            or receipt.get("authorization_scope") != "one_legacy_attempt_one_target"
            or dict(receipt.get("path_params") or {}) != params
            or receipt.get("key_hash") != key_hash
            or receipt.get("provider_instance_hash") != provider_instance_hash
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", authorization_id)
            or receipt.get("legacy_path") != expected_path
            or receipt.get("legacy_method") != "POST"
            or receipt.get("legacy_fingerprint") != expected_fingerprint
            or receipt.get("legacy_payload_fingerprint") != expected_payload_fingerprint
            or receipt.get("historical_wire_body_present") is not True
            or receipt.get("contract_version") != CONTRACT_VERSION
            or receipt.get("contract_hash") != CONTRACT_HASH
            or receipt.get("operator_disposition") != "replacement_authorized_after_effect_absence"
        ):
            raise PermissionError("reconciliation authorization does not bind this legacy archive attempt")

        store = _require_ledger_path(ledger_path)
        with _locked_ledger(store) as ledger:
            authorizations = ledger.setdefault("reconciliation_authorizations", {})
            if not isinstance(authorizations, dict):
                raise RuntimeError("legacy reconciliation authorization ledger is invalid")
            if authorization_id in authorizations:
                raise PermissionError("reconciliation authorization_id was already consumed")
            authorizations[authorization_id] = {"key_hash": key_hash, "action": action}
            prior = ledger["receipts"].get(key_hash)
            if not isinstance(prior, Mapping):
                raise ValueError("legacy attempt receipt was not found")
            if (
                prior.get("status") != "attempt_started"
                or prior.get("action") != action
                or str(prior.get("method") or "").upper() != "POST"
                or prior.get("path") != expected_path
                or prior.get("fingerprint") != expected_fingerprint
            ):
                raise PermissionError("legacy receipt does not exactly match the governed archive attempt")
            frozen_prior = dict(prior)
        try:
            evidence = self._prove_module_archive_not_applied(params)
            disposition = "effect_not_present_at_reconciliation_time"
            current_effect = "absent_at_observation"
            readback_verified = True
            safe_error: dict[str, str] | None = None
        except (PlaneProviderRequestError, PlaneResponseValidationError, PlaneReadbackValidationError, RuntimeError) as exc:
            evidence = {"provider_proof": "inconclusive"}
            disposition = "inconclusive"
            current_effect = "unknown"
            readback_verified = False
            safe_error = {"class": type(exc).__name__, "code": _reconciliation_error_code(exc)}
        observed_at = datetime.now(timezone.utc).isoformat()
        event_id = hashlib.sha256(json.dumps({"key_hash": key_hash, "authorization_id": authorization_id, "observed_at": observed_at, "evidence": evidence}, sort_keys=True).encode()).hexdigest()
        with _locked_ledger(store) as ledger:
            prior = ledger["receipts"].get(key_hash)
            if not isinstance(prior, Mapping) or dict(prior) != frozen_prior:
                raise RuntimeError("legacy receipt changed during reconciliation")
            entry = {
                "event_id": event_id,
                "observed_at": observed_at,
                "evidence_version": 1,
                "kind": "legacy_archive_attempt_reconciliation",
                "action": action,
                "path": expected_path,
                "fingerprint": expected_fingerprint,
                "bindings": {
                    "key_hash": key_hash,
                    "authorization_id": authorization_id,
                    "provider_instance_hash": provider_instance_hash,
                    "legacy_method": "POST",
                    "legacy_payload_fingerprint": expected_payload_fingerprint,
                    "historical_wire_body_present": True,
                    "contract_version": CONTRACT_VERSION,
                    "contract_hash": CONTRACT_HASH,
                    "path_params": params,
                },
                "disposition": "effect_not_present_at_reconciliation_time",
                "evidence": evidence,
            }
            # Append separately; the original historical receipt stays byte-for-byte intact.
            events = ledger.setdefault("reconciliation_events", [])
            if not isinstance(events, list):
                raise RuntimeError("legacy reconciliation event ledger is invalid")
            current_authorizations = ledger.get("reconciliation_authorizations", {})
            if not isinstance(current_authorizations, Mapping):
                raise RuntimeError("legacy reconciliation authorization ledger is invalid")
            incomplete_ids = sorted(
                candidate_id for candidate_id, binding in current_authorizations.items()
                if isinstance(binding, Mapping)
                and binding.get("key_hash") == key_hash
                and candidate_id != authorization_id
                and not any(isinstance(event, Mapping) and (event.get("bindings") or {}).get("authorization_id") == candidate_id for event in events)
            )
            if incomplete_ids:
                entry["prior_incomplete_authorization_ids"] = incomplete_ids
            entry["disposition"] = disposition
            if safe_error:
                entry["error"] = safe_error
            events.append(entry)
            return {
                "governed": True,
                "action": action,
                "key_hash": key_hash,
                "disposition": disposition,
                "duplicate": False,
                "original_outcome": "unknown",
                "current_effect": current_effect,
                "ledger_mutation_applied": True,
                "provider_mutation_applied": False,
                "readback_verified": readback_verified,
                "evidence": evidence,
                **({"error": safe_error} if safe_error else {}),
            }

    def reconcile_module_update_attempt(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        resource_id: str,
        key_hash: str,
        expected_payload: Mapping[str, Any],
        attempts: int,
        approved_live_reconciliation: bool,
        authorization_receipt: Mapping[str, Any] | None,
        ledger_path: str | Path,
    ) -> dict[str, Any]:
        """Append exact provider proof for an applied module update receipt."""

        action = "module__update_module_detail"
        operation = get_operation(action)
        params = {
            "workspace_slug": str(workspace_slug or "").strip(),
            "project_id": str(project_id or "").strip(),
            "resource_id": str(resource_id or "").strip(),
        }
        payload = dict(expected_payload or {})
        if not all(params.values()) or not payload:
            raise ValueError("module reconciliation requires target and expected_payload")
        allowed = set((operation.request_schema.get("json_schema") or {}).get("properties") or ())
        if not set(payload) <= allowed or set(payload) & {"workspace_slug", "project_id", "resource_id"}:
            raise ValueError("module reconciliation payload contains unsupported fields")
        if attempts != 1 or approved_live_reconciliation is not True:
            raise PermissionError("exactly one explicitly approved reconciliation attempt is required")
        key_hash = str(key_hash or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", key_hash):
            raise ValueError("key_hash must be exactly 64 lowercase hexadecimal characters")
        expected_path = _expand_path(operation.path, params)
        expected_fingerprint = _fingerprint(
            action, "", "", None,
            {"path_params": params, "query": {}, "payload": payload},
        )
        provider_instance_hash = _provider_instance_hash(self.config)
        receipt = dict(authorization_receipt or {})
        authorization_id = str(receipt.get("authorization_id") or "").strip()
        if (
            receipt.get("action") != action
            or str(receipt.get("method") or "").upper() != "PATCH"
            or receipt.get("approved_live_reconciliation") is not True
            or receipt.get("authorization_scope") != "one_mutation_attempt_one_target"
            or dict(receipt.get("path_params") or {}) != params
            or receipt.get("key_hash") != key_hash
            or receipt.get("payload_fingerprint") != payload_fingerprint(payload)
            or receipt.get("provider_instance_hash") != provider_instance_hash
            or receipt.get("contract_version") != CONTRACT_VERSION
            or receipt.get("contract_hash") != CONTRACT_HASH
            or receipt.get("operator_disposition") != "current_state_matches_intended_update"
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", authorization_id)
        ):
            raise PermissionError("reconciliation authorization does not bind this module update attempt")

        store = _require_ledger_path(ledger_path)
        with _locked_ledger(store) as ledger:
            authorizations = ledger.setdefault("reconciliation_authorizations", {})
            if not isinstance(authorizations, dict):
                raise RuntimeError("reconciliation authorization ledger is invalid")
            if authorization_id in authorizations:
                raise PermissionError("reconciliation authorization_id was already consumed")
            authorizations[authorization_id] = {"key_hash": key_hash, "action": action}
            prior = ledger["receipts"].get(key_hash)
            if not isinstance(prior, Mapping) or (
                prior.get("status") != "write_succeeded_readback_validation_failed"
                or prior.get("action") != action
                or str(prior.get("method") or "").upper() != "PATCH"
                or prior.get("path") != expected_path
                or prior.get("fingerprint") != expected_fingerprint
                or prior.get("mutation_applied") is not True
            ):
                raise PermissionError("module update receipt does not match an applied unverified attempt")
            frozen_prior = dict(prior)

        detail_operation = get_operation("module__get_module_detail")
        try:
            response = self._request(
                "GET", _expand_path(detail_operation.path, params),
                with_metadata=True, phase="reconciliation_module_update_detail",
            )
            _validate_response_status(
                response, detail_operation.response_schema.get("status"),
                detail_operation.response_schema, context="module update reconciliation",
            )
            body = _response_body(response)
            _validate_response_shape(
                body, {"response_shape": "detail"}, detail_operation.response_schema,
                context="module update reconciliation",
            )
            if str(body.get("id") or "") != params["resource_id"]:
                raise PlaneReadbackValidationError("module update reconciliation identity mismatch")
            mismatches = [field for field, expected in payload.items() if body.get(field) != expected]
            if mismatches:
                raise PlaneReadbackValidationError(
                    "module update reconciliation effect mismatch: " + ", ".join(sorted(mismatches))
                )
            disposition = "effect_present_at_reconciliation_time"
            readback_verified = True
            evidence = {
                "provider_proof": "exact_module_detail_matches_expected_update",
                "readback_action": detail_operation.action,
                "target_id": params["resource_id"],
                "verified_fields": sorted(payload),
            }
            safe_error = None
        except (PlaneProviderRequestError, PlaneResponseValidationError, PlaneReadbackValidationError, RuntimeError) as exc:
            disposition = "inconclusive"
            readback_verified = False
            evidence = {"provider_proof": "inconclusive"}
            safe_error = {"class": type(exc).__name__, "code": _reconciliation_error_code(exc)}

        observed_at = datetime.now(timezone.utc).isoformat()
        event = {
            "event_id": hashlib.sha256(
                json.dumps({"key_hash": key_hash, "authorization_id": authorization_id, "observed_at": observed_at}, sort_keys=True).encode()
            ).hexdigest(),
            "observed_at": observed_at,
            "evidence_version": 1,
            "kind": "module_update_attempt_reconciliation",
            "action": action,
            "path": expected_path,
            "fingerprint": expected_fingerprint,
            "bindings": {
                "key_hash": key_hash,
                "authorization_id": authorization_id,
                "provider_instance_hash": provider_instance_hash,
                "contract_version": CONTRACT_VERSION,
                "contract_hash": CONTRACT_HASH,
                "path_params": params,
                "payload_fingerprint": payload_fingerprint(payload),
            },
            "disposition": disposition,
            "evidence": evidence,
            **({"error": safe_error} if safe_error else {}),
        }
        with _locked_ledger(store) as ledger:
            prior = ledger["receipts"].get(key_hash)
            if not isinstance(prior, Mapping) or dict(prior) != frozen_prior:
                raise RuntimeError("module update receipt changed during reconciliation")
            events = ledger.setdefault("reconciliation_events", [])
            if not isinstance(events, list):
                raise RuntimeError("reconciliation event ledger is invalid")
            events.append(event)
        return {
            "governed": True,
            "action": action,
            "key_hash": key_hash,
            "disposition": disposition,
            "original_outcome": "write_succeeded_response_validation_failed",
            "current_effect": "present_at_observation" if readback_verified else "unknown",
            "ledger_mutation_applied": True,
            "provider_mutation_applied": False,
            "readback_verified": readback_verified,
            "evidence": evidence,
            **({"error": safe_error} if safe_error else {}),
        }

    def _prove_module_archive_not_applied(self, params: Mapping[str, str]) -> dict[str, Any]:
        """Require exhaustive archived absence plus an exact active detail read."""
        archive_action = get_operation("module__archive_module")
        specification = get_mutation_contract(archive_action.action)["postcondition"]
        archived_action = get_operation("module__list_archived_modules")
        active_action = get_operation("module__list_modules")
        target = str(params["resource_id"])
        cursor: str | None = None
        seen_cursors: set[str] = set()
        pages = 0
        rows_seen = 0
        pagination = specification.get("pagination") or {}
        max_pages = int(pagination.get("max_pages") or _MAX_READBACK_PAGES)
        max_rows = int(pagination.get("max_rows") or _MAX_READBACK_ROWS)
        while True:
            pages += 1
            if pages > max_pages:
                raise RuntimeError("archive reconciliation evidence is not exhaustive")
            response = self._request(
                "GET", _expand_path(archived_action.path, params),
                query=_readback_query(specification, cursor), with_metadata=True,
                phase="reconciliation_archived_absence",
            )
            _validate_response_status(response, specification.get("readback_expected_status"), archived_action.response_schema, context="archive reconciliation")
            body = _response_body(response)
            _validate_response_shape(body, specification, archived_action.response_schema, context="archive reconciliation")
            _validate_identity_bindings(body, specification, params)
            rows = _rows(body)
            if rows is None:
                raise PlaneReadbackValidationError("archive reconciliation evidence has no archived module list")
            rows_seen += len(rows)
            if rows_seen > max_rows:
                raise RuntimeError("archive reconciliation evidence is not exhaustive")
            if _readback_contains_targets(body, {target}, specification):
                raise PlaneReadbackValidationError("archive reconciliation shows the target is already archived")
            if not _has_next_page(body, specification):
                break
            cursor = _next_cursor(body, specification)
            if not cursor or cursor in seen_cursors:
                raise RuntimeError("archive reconciliation evidence is not exhaustive")
            seen_cursors.add(cursor)

        active_cursor: str | None = None
        active_seen_cursors: set[str] = set()
        active_pages = 0
        active_rows = 0
        active_found = False
        while True:
            active_pages += 1
            if active_pages > max_pages:
                raise RuntimeError("archive reconciliation active evidence is not exhaustive")
            active_response = self._request(
                "GET", _expand_path(active_action.path, params),
                query=_readback_query(specification, active_cursor), with_metadata=True,
                phase="reconciliation_active_presence",
            )
            _validate_response_status(active_response, active_action.response_schema.get("status"), active_action.response_schema, context="archive reconciliation")
            active_body = _response_body(active_response)
            _validate_module_reconciliation_collection(active_body, context="archive reconciliation active modules")
            active_rows_list = _rows(active_body)
            if active_rows_list is None:
                raise PlaneReadbackValidationError("archive reconciliation evidence has no active module list")
            active_rows += len(active_rows_list)
            if active_rows > max_rows:
                raise RuntimeError("archive reconciliation active evidence is not exhaustive")
            active_found = active_found or _readback_contains_targets(active_body, {target}, specification)
            if not _has_next_page(active_body, specification):
                break
            active_cursor = _next_cursor(active_body, specification)
            if not active_cursor or active_cursor in active_seen_cursors:
                raise RuntimeError("archive reconciliation active evidence is not exhaustive")
            active_seen_cursors.add(active_cursor)
        if not active_found:
            raise PlaneReadbackValidationError("archive reconciliation active module list does not contain target")
        return {
            "provider_proof": "target_absent_from_exhaustive_archived_list_and_present_in_active_module_list",
            "causality": "current provider state only; operator explicitly authorized replacement after effect-absence proof",
            "archived_list_action": archived_action.action,
            "archived_list_pages": pages,
            "archived_list_rows": rows_seen,
            "active_list_action": active_action.action,
            "active_list_pages": active_pages,
            "active_list_rows": active_rows,
            "target_id": target,
        }

    def _evaluate_generic_postcondition(
        self,
        operation,
        params: Mapping[str, Any],
        provider_id: str | None,
        query: Mapping[str, Any] | None,
        *,
        response: Any | None = None,
        response_metadata: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve and strictly validate a registry postcondition.

        The provider response is deliberately consumed here.  Only bounded
        metadata is returned to callers; raw mutation/readback objects never
        become part of a receipt.
        """
        contract = get_mutation_contract(operation.action)
        specification = contract["postcondition"]
        kind = str(specification["kind"])
        if contract.get("write_policy") != "allow" or contract.get("expected_terminal") not in {"verified", "verified_absence", "provider_acknowledged"}:
            raise PermissionError(f"Plane mutation contract is not executable: {operation.action}")
        if response is not None:
            _validate_response_status(
                response,
                specification.get("expected_status"),
                operation.response_schema,
                context="mutation",
            )
            if operation.action == "module__update_module_detail":
                _validate_module_update_ack(_response_body(response), payload or {})
            else:
                _validate_mutation_write_response(operation, response, payload or {})
            if kind == "provider_ack" and operation.action != "module__update_module_detail":
                _validate_provider_acknowledgement(response, operation.response_schema, specification)
        else:
            if operation.action == "module__update_module_detail":
                _validate_persisted_module_update_ack(response_metadata, specification)
            else:
                _validate_persisted_write_evidence(response_metadata, specification, operation.response_schema)
            if kind == "provider_ack" and operation.action != "module__update_module_detail":
                _validate_persisted_acknowledgement(response_metadata, specification)
        if operation.action == "module__update_module_detail":
            return self._prove_module_update_effect(params, payload or {}, kind, specification)
        if operation.action == "issue__update_issue_detail":
            return self._prove_module_update_effect(
                params, payload or {}, kind, specification,
                detail_action="issue__get_issue_detail",
            )
        get_action = specification.get("get_action")
        if operation.action == "issue__add_issue":
            # The source registry already documents the immutable detail GET.
            # Creation is verified against that exact provider-issued id, not a
            # mutable collection scan.
            get_action = "issue__get_issue_detail"
            detail_operation = get_operation_for_postcondition(get_action)
            specification = {
                **specification,
                "path": detail_operation.path,
                "readback_expected_status": detail_operation.response_schema.get("status"),
                "response_shape": detail_operation.response_schema.get("shape"),
                "strategy": "detail_state",
            }
        if not get_action:
            if operation.method == "DELETE":
                if kind != "provider_ack":
                    raise RuntimeError("Plane DELETE contract has no true absence readback")
            return {
                "kind": kind,
                "state": "provider_acknowledged" if kind == "provider_ack" else "verified",
                "verification": specification["verification"],
                "readback_performed": False,
            }

        targets = _mutation_targets(specification, params, payload or {}, response, provider_id)
        target_id = next(iter(targets), None)
        path = _postcondition_path(specification, params, target_id, payload)
        if operation.action == "issue__add_issue":
            path = _expand_path(
                specification["path"], {**params, "resource_id": target_id}
            )
        if path is None:
            raise RuntimeError("Plane mutation postcondition has no documented readback path")
        get_operation = get_operation_for_postcondition(get_action)
        if get_operation.method != "GET" or (
            operation.action != "issue__add_issue"
            and get_operation.path != specification.get("path")
        ):
            raise RuntimeError("Plane mutation postcondition readback route is not registry-bound")
        _validate_postcondition_bindings(specification, params, path, payload)
        readback = self._request(
            "GET", path, query=_readback_query(specification), allow_not_found=True,
            with_metadata=True, phase="readback",
        )
        if kind == "membership_state" and contract["expected_terminal"] == "verified_absence":
            readback = self._verify_absence_readback(
                readback,
                get_operation,
                specification,
                params,
                query,
                targets,
            )
            state = "verified_absence"
        else:
            readback = self._verify_retrieve_readback(
                readback,
                get_operation,
                specification,
                params,
                query,
                targets,
                payload or {},
            )
            state = "verified"
        result = {
            "kind": kind,
            "state": state,
            "verification": specification["verification"],
            "readback_performed": True,
            "readback_action": str(get_action),
        }
        if specification.get("response_shape"):
            result["response_shape"] = specification["response_shape"]
        return result

    def _prove_module_update_effect(
        self,
        params: Mapping[str, Any],
        payload: Mapping[str, Any],
        kind: str,
        specification: Mapping[str, Any],
        *,
        detail_action: str = "module__get_module_detail",
    ) -> dict[str, Any]:
        """Verify PATCH effects against an exact registry-bound detail GET."""

        detail_operation = get_operation(detail_action)
        path = _expand_path(detail_operation.path, params)
        response = self._request(
            "GET", path, with_metadata=True, phase=f"readback:{detail_action}"
        )
        _validate_response_status(
            response, detail_operation.response_schema.get("status"),
            detail_operation.response_schema, context="module update readback",
        )
        body = _response_body(response)
        _validate_response_shape(
            body, {"response_shape": "detail"}, detail_operation.response_schema,
            context="module update readback",
        )
        if str(body.get("id") or "") != str(params.get("resource_id") or ""):
            raise PlaneReadbackValidationError("module update readback identity mismatch")
        mismatches = [field for field, expected in payload.items() if body.get(field) != expected]
        if mismatches:
            raise PlaneReadbackValidationError(
                "module update readback effect mismatch: " + ", ".join(sorted(mismatches))
            )
        return {
            "kind": kind,
            "state": "provider_acknowledged",
            "verification": specification["verification"],
            "readback_performed": True,
            "readback_action": detail_operation.action,
            "response_shape": "detail",
            "verified_fields": sorted(payload),
        }

    def _verify_retrieve_readback(
        self,
        response: Any,
        get_operation,
        specification: Mapping[str, Any],
        params: Mapping[str, Any],
        query: Mapping[str, Any] | None,
        targets: set[str],
        payload: Mapping[str, Any],
    ) -> Any:
        current = response
        seen_cursors: set[str] = set()
        pages = 0
        rows_seen = 0
        pagination = specification.get("pagination") or {}
        max_pages = int(pagination.get("max_pages") or _MAX_READBACK_PAGES)
        max_rows = int(pagination.get("max_rows") or _MAX_READBACK_ROWS)
        while True:
            pages += 1
            if pages > max_pages:
                raise RuntimeError("Plane mutation readback pagination exceeded page budget")
            _validate_response_status(
                current,
                specification.get("readback_expected_status"),
                get_operation.response_schema,
                context="readback",
            )
            body = _response_body(current)
            _validate_response_shape(
                body,
                specification,
                get_operation.response_schema,
                context="readback",
                operation_action=get_operation.action,
            )
            _validate_identity_bindings(body, specification, params)
            rows_seen += len(_rows(body) or [])
            if rows_seen > max_rows:
                raise RuntimeError("Plane mutation readback pagination exceeded row budget")
            if _readback_contains_targets(body, targets, specification, payload=payload):
                if specification.get("strategy") == "detail_state":
                    _validate_detail_effects(body, payload, specification)
                return current
            if _is_collection_shape(specification.get("response_shape")) and _has_next_page(body, specification):
                cursor = _next_cursor(body, specification)
                if not cursor or cursor in seen_cursors:
                    raise RuntimeError("Plane mutation readback pagination is unresolved")
                seen_cursors.add(cursor)
                current = self._request(
                    "GET",
                    _postcondition_path(specification, params, next(iter(targets), None), payload) or "",
                    query=_readback_query(specification, cursor),
                    allow_not_found=True,
                    with_metadata=True,
                )
                continue
            raise PlaneReadbackValidationError(
                "Plane mutation readback did not contain the matching target identity"
            )

    def _verify_absence_readback(
        self,
        response: Any,
        get_operation,
        specification: Mapping[str, Any],
        params: Mapping[str, Any],
        query: Mapping[str, Any] | None,
        targets: set[str],
    ) -> Any:
        current = response
        seen_cursors: set[str] = set()
        pages = 0
        rows_seen = 0
        pagination = specification.get("pagination") or {}
        max_pages = int(pagination.get("max_pages") or _MAX_READBACK_PAGES)
        max_rows = int(pagination.get("max_rows") or _MAX_READBACK_ROWS)
        while True:
            pages += 1
            if pages > max_pages:
                raise RuntimeError("Plane mutation absence readback exceeded page budget")
            status = _response_status(current)
            if status == 404:
                return current
            _validate_response_status(
                current,
                specification.get("readback_expected_status"),
                get_operation.response_schema,
                context="absence readback",
            )
            body = _response_body(current)
            if _is_collection_shape(specification.get("response_shape")):
                _validate_absence_collection_envelope(body, specification, context="absence readback")
            else:
                _validate_response_shape(
                    body,
                    specification,
                    get_operation.response_schema,
                    context="absence readback",
                    operation_action=get_operation.action,
                )
            _validate_identity_bindings(body, specification, params)
            if not _is_collection_shape(specification.get("response_shape")):
                raise RuntimeError(
                    "Plane mutation absence readback requires the documented detail endpoint to return 404"
                )
            rows_seen += len(_rows(body) or [])
            if rows_seen > max_rows:
                raise RuntimeError("Plane mutation absence readback exceeded row budget")
            if _readback_contains_targets(body, targets, specification, payload=None):
                raise PlaneReadbackValidationError(
                    "Plane mutation deletion readback shows the target is still present"
                )
            if _is_collection_shape(specification.get("response_shape")) and _has_next_page(body, specification):
                cursor = _next_cursor(body, specification)
                if not cursor or cursor in seen_cursors:
                    raise RuntimeError("Plane mutation absence readback pagination is unresolved")
                seen_cursors.add(cursor)
                current = self._request(
                    "GET",
                    _postcondition_path(specification, params, next(iter(targets), None)) or "",
                    query=_readback_query(specification, cursor),
                    allow_not_found=True,
                    with_metadata=True,
                )
                continue
            return current

    def _readback(self, operation, params: Mapping[str, Any], provider_id: str | None, query: Mapping[str, Any] | None) -> Any:
        path = _readback_path(operation, params, provider_id)
        # Compatibility helper retained for callers of the old private seam;
        # mutation query parameters are never forwarded into a readback.
        return None if path is None else self._request("GET", path, query={})

    def operation_descriptor(
        self,
        action: str,
        *,
        path_params: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        operation = get_operation(action)
        return {
            "dry_run": True,
            "would_mutate": operation.mutation,
            "action": operation.action,
            "method": operation.method,
            "path_template": operation.path,
            "path": _expand_path(operation.path, path_params or {}),
            "query": dict(query or {}),
            "payload_descriptor": _payload_descriptor(payload or {}),
            "authorization_receipt_template": {
                "action": operation.action,
                "method": operation.method,
                "approved_live_mutation": True,
                "authorization_scope": "one_operation_one_target",
                "path_params": dict(path_params or {}),
                "payload_fingerprint": payload_fingerprint(payload or {}),
                "key_hash": "sha256(idempotency_key)",
                "authorization_id": "operator-issued-single-use-id",
                "provider_instance_hash": _provider_instance_hash(self.config),
                "workspace_slug": (path_params or {}).get("workspace_slug"),
                "project_id": (path_params or {}).get("project_id"),
            },
            "docs_path": operation.docs_path,
            "docs_url": f"https://developers.plane.so{operation.docs_path}",
        }

    def get_current_user(self) -> Any:
        return self.execute_read_action("user__get_current_user")

    def list_projects(self, workspace_slug: str, **query: Any) -> Any:
        return self.execute_read_action(
            "project__list_projects", path_params={"workspace_slug": workspace_slug}, query=query
        )

    def list_work_items(self, workspace_slug: str, project_id: str, **query: Any) -> Any:
        return self.execute_read_action(
            "issue__list_issues",
            path_params={"workspace_slug": workspace_slug, "project_id": project_id},
            query=query,
        )

    def get_work_item(self, workspace_slug: str, project_id: str, work_item_id: str, **query: Any) -> Any:
        item = self.execute_read_action(
            "issue__get_issue_detail",
            path_params={"workspace_slug": workspace_slug, "project_id": project_id, "resource_id": work_item_id},
            query=query,
        )
        if not isinstance(item, Mapping):
            return item
        return {
            **item,
            "web_url": _work_item_web_url(
                self.config.base_url, workspace_slug, project_id, work_item_id
            ),
        }

    def search_work_items(self, workspace_slug: str, **query: Any) -> Any:
        return self.execute_read_action(
            "issue__search_issues", path_params={"workspace_slug": workspace_slug}, query=query
        )

    def governed_create_work_item(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        payload: Mapping[str, Any],
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        idempotency_key: str,
        attempts: int,
        ledger_path: str | Path | None = None,
    ) -> dict[str, Any]:
        return self._governed_work_item_mutation(
            method="POST",
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=None,
            payload=payload,
            approved_live_mutation=approved_live_mutation,
            authorization_scope=authorization_scope,
            authorized_workspace_slug=authorized_workspace_slug,
            authorized_project_id=authorized_project_id,
            authorized_work_item_id=None,
            idempotency_key=idempotency_key,
            attempts=attempts,
            ledger_path=ledger_path,
        )

    def governed_update_work_item(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        payload: Mapping[str, Any],
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        authorized_work_item_id: str,
        idempotency_key: str,
        attempts: int,
        ledger_path: str | Path | None = None,
    ) -> dict[str, Any]:
        return self._governed_work_item_mutation(
            method="PATCH",
            workspace_slug=workspace_slug,
            project_id=project_id,
            work_item_id=work_item_id,
            payload=payload,
            approved_live_mutation=approved_live_mutation,
            authorization_scope=authorization_scope,
            authorized_workspace_slug=authorized_workspace_slug,
            authorized_project_id=authorized_project_id,
            authorized_work_item_id=authorized_work_item_id,
            idempotency_key=idempotency_key,
            attempts=attempts,
            ledger_path=ledger_path,
        )

    def governed_add_work_item_comment(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        work_item_id: str,
        payload: Mapping[str, Any],
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        authorized_work_item_id: str,
        authorization_receipt: Mapping[str, Any],
        idempotency_key: str,
        attempts: int,
        ledger_path: str | Path,
    ) -> dict[str, Any]:
        workspace = str(workspace_slug or "").strip()
        project = str(project_id or "").strip()
        item = str(work_item_id or "").strip()
        if approved_live_mutation is not True:
            raise PermissionError("explicit live mutation approval is required")
        if authorization_scope != _GOVERNED_SCOPE:
            raise PermissionError("authorization scope must be one_work_item_one_operation")
        if attempts != 1:
            raise ValueError("exactly one mutation attempt is allowed")
        if workspace != str(authorized_workspace_slug or "").strip():
            raise PermissionError("authorized workspace does not match target")
        if project != str(authorized_project_id or "").strip():
            raise PermissionError("authorized project does not match target")
        if item != str(authorized_work_item_id or "").strip():
            raise PermissionError("authorized work item does not match target")
        if not workspace or not project or not item:
            raise ValueError("workspace_slug, project_id and work_item_id are required")

        clean_payload = dict(payload or {})
        unknown = sorted(set(clean_payload) - _COMMENT_FIELDS)
        if unknown:
            raise ValueError(f"unsupported comment payload fields: {', '.join(unknown)}")
        if not str(clean_payload.get("comment_html") or "").strip():
            raise ValueError("comment payload requires non-empty comment_html")
        key = str(idempotency_key or "").strip()
        if len(key) < 16 or len(key) > 200:
            raise ValueError("idempotency_key must contain 16 to 200 characters")

        action = "add_work_item_comment"
        fingerprint = _fingerprint(action, workspace, project, item, clean_payload)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        receipt = dict(authorization_receipt or {})
        authorization_id = str(receipt.get("authorization_id") or "").strip()
        expected_params = {"workspace_slug": workspace, "project_id": project, "work_item_id": item}
        if (
            receipt.get("action") != action
            or str(receipt.get("method") or "").upper() != "POST"
            or receipt.get("approved_live_mutation") is not True
            or receipt.get("authorization_scope") != _GOVERNED_SCOPE
            or dict(receipt.get("path_params") or {}) != expected_params
            or receipt.get("payload_fingerprint") != payload_fingerprint(clean_payload)
            or receipt.get("key_hash") != key_hash
            or receipt.get("provider_instance_hash") != _provider_instance_hash(self.config)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", authorization_id)
        ):
            raise PermissionError("comment authorization receipt does not bind this governed mutation")
        store = _require_ledger_path(ledger_path)
        comments_path = (
            f"/api/v1/workspaces/{quote(workspace, safe='')}/projects/"
            f"{quote(project, safe='')}/work-items/{quote(item, safe='')}/comments/"
        )

        with _locked_ledger(store) as ledger:
            authorizations = ledger.setdefault("comment_authorizations", {})
            if not isinstance(authorizations, dict):
                raise RuntimeError("Plane comment authorization ledger has invalid shape")
            if authorization_id in authorizations:
                raise PermissionError("comment authorization_id was already consumed")
            authorizations[authorization_id] = {"key_hash": key_hash, "provider_instance_hash": _provider_instance_hash(self.config)}
            previous = ledger["receipts"].get(key_hash)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError(
                        "idempotency_key was already used for a different mutation"
                    )
                pending = dict(previous)
            else:
                ledger["receipts"][key_hash] = {
                    "action": action, "workspace_slug": workspace, "project_id": project,
                    "work_item_id": item, "fingerprint": fingerprint, "key_hash": key_hash,
                    "provider_instance_hash": _provider_instance_hash(self.config), "status": "attempt_started",
                }
                pending = None
        if pending is not None:
            comment_id = str(pending.get("provider_id") or "")
            if not comment_id:
                raise RuntimeError("previous comment mutation attempt has unknown outcome; strict no-retry policy blocks replay")
            readback = self._governed_request("GET", f"{comments_path}{quote(comment_id, safe='')}/")
            _verify_comment_readback(comment_id, clean_payload, readback)
            return _comment_receipt(workspace, project, item, comment_id, readback, base_url=self.config.base_url, mutation_applied=False, duplicate=True)
        response = self._governed_request("POST", comments_path, payload=clean_payload)
        if not isinstance(response, Mapping) or not str(response.get("id") or "").strip():
            raise RuntimeError("Plane comment mutation did not return a comment id")
        comment_id = str(response["id"])
        with _locked_ledger(store) as ledger:
            _cas_receipt(ledger, key_hash, fingerprint, {
                "provider_id": comment_id, "status": "applied_pending_readback",
            })
        readback = self._governed_request("GET", f"{comments_path}{quote(comment_id, safe='')}/")
        _verify_comment_readback(comment_id, clean_payload, readback)
        with _locked_ledger(store) as ledger:
            _cas_receipt(ledger, key_hash, fingerprint, {"status": "verified", "readback_verified": True})
        return _comment_receipt(
            workspace, project, item, comment_id, readback,
            base_url=self.config.base_url, mutation_applied=True, duplicate=False,
        )

    def governed_bind_work_item_module(
        self,
        *,
        workspace_slug: str,
        project_id: str,
        module_id: str,
        work_item_id: str,
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        authorized_module_id: str,
        authorized_work_item_id: str,
        idempotency_key: str,
        attempts: int,
        ledger_path: str | Path | None = None,
    ) -> dict[str, Any]:
        workspace = str(workspace_slug or "").strip()
        project = str(project_id or "").strip()
        module = str(module_id or "").strip()
        item = str(work_item_id or "").strip()
        if approved_live_mutation is not True:
            raise PermissionError("explicit live mutation approval is required")
        if authorization_scope != _GOVERNED_SCOPE:
            raise PermissionError("authorization scope must be one_work_item_one_operation")
        if attempts != 1:
            raise ValueError("exactly one mutation attempt is allowed")
        if workspace != str(authorized_workspace_slug or "").strip():
            raise PermissionError("authorized workspace does not match target")
        if project != str(authorized_project_id or "").strip():
            raise PermissionError("authorized project does not match target")
        if module != str(authorized_module_id or "").strip():
            raise PermissionError("authorized module does not match target")
        if item != str(authorized_work_item_id or "").strip():
            raise PermissionError("authorized work item does not match target")
        if not workspace or not project or not module or not item:
            raise ValueError(
                "workspace_slug, project_id, module_id and work_item_id are required"
            )
        key = str(idempotency_key or "").strip()
        if len(key) < 16 or len(key) > 200:
            raise ValueError("idempotency_key must contain 16 to 200 characters")

        action = "bind_work_item_module"
        fingerprint = _fingerprint(
            action, workspace, project, item, {"module_id": module}
        )
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        store = Path(
            ledger_path or Path.home() / ".hermes/state/plane-governed-mutations.json"
        )
        path = (
            f"/api/v1/workspaces/{quote(workspace, safe='')}/projects/"
            f"{quote(project, safe='')}/modules/{quote(module, safe='')}/module-issues/"
        )

        def receipt(
            *, mutation_applied: bool, duplicate: bool, provider_existing: bool
        ) -> dict[str, Any]:
            return {
                "action": action,
                "workspace_slug": workspace,
                "project_id": project,
                "module_id": module,
                "work_item_id": item,
                "mutation_applied": mutation_applied,
                "duplicate": duplicate,
                "provider_existing": provider_existing,
                "readback_verified": True,
            }

        def binding_exists() -> bool:
            query: dict[str, Any] = {"per_page": 100}
            seen_cursors: set[str] = set()
            while True:
                response = self._governed_request("GET", path, query=query)
                if item in _module_work_item_ids(response):
                    return True
                if not isinstance(response, Mapping) or response.get(
                    "next_page_results"
                ) is not True:
                    return False
                cursor = str(response.get("next_cursor") or "").strip()
                if not cursor or cursor in seen_cursors:
                    raise RuntimeError("Plane module binding pagination is invalid")
                seen_cursors.add(cursor)
                query = {"per_page": 100, "cursor": cursor}

        mutation_applied = False
        duplicate = False
        provider_existing = False
        with _locked_ledger(store) as ledger:
            previous = ledger["receipts"].get(key_hash)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError(
                        "idempotency_key was already used for a different mutation"
                    )
                duplicate = True
                provider_existing = bool(previous.get("provider_existing"))
            elif binding_exists():
                ledger["receipts"][key_hash] = {
                    "action": action,
                    "workspace_slug": workspace,
                    "project_id": project,
                    "module_id": module,
                    "work_item_id": item,
                    "provider_id": item,
                    "provider_existing": True,
                    "fingerprint": fingerprint,
                    "readback_verified": True,
                    "status": "provider_existing_verified",
                }
                return receipt(
                    mutation_applied=False,
                    duplicate=False,
                    provider_existing=True,
                )
            else:
                self._governed_request("POST", path, payload={"issues": [item]})
                ledger["receipts"][key_hash] = {
                    "action": action,
                    "workspace_slug": workspace,
                    "project_id": project,
                    "module_id": module,
                    "work_item_id": item,
                    "provider_id": item,
                    "provider_existing": False,
                    "fingerprint": fingerprint,
                    "readback_verified": False,
                    "status": "applied_pending_readback",
                }
                # The provider write may have succeeded even if this process dies before
                # readback. Flush the receipt while the lock is still held so a retry
                # performs only GET and can never repeat the POST.
                _persist_ledger(store, ledger)
                mutation_applied = True

        if not binding_exists():
            raise RuntimeError("Plane module binding readback mismatch")
        with _locked_ledger(store) as ledger:
            persisted = ledger["receipts"].get(key_hash)
            if not persisted or persisted.get("fingerprint") != fingerprint:
                raise RuntimeError("Plane module binding receipt is missing after readback")
            persisted["readback_verified"] = True
            persisted["status"] = "verified"
        return receipt(
            mutation_applied=mutation_applied,
            duplicate=duplicate,
            provider_existing=provider_existing,
        )

    def _governed_work_item_mutation(
        self,
        *,
        method: str,
        workspace_slug: str,
        project_id: str,
        work_item_id: str | None,
        payload: Mapping[str, Any],
        approved_live_mutation: bool,
        authorization_scope: str,
        authorized_workspace_slug: str,
        authorized_project_id: str,
        authorized_work_item_id: str | None,
        idempotency_key: str,
        attempts: int,
        ledger_path: str | Path | None,
    ) -> dict[str, Any]:
        workspace = str(workspace_slug or "").strip()
        project = str(project_id or "").strip()
        item = str(work_item_id or "").strip() or None
        if approved_live_mutation is not True:
            raise PermissionError("explicit live mutation approval is required")
        if authorization_scope != _GOVERNED_SCOPE:
            raise PermissionError("authorization scope must be one_work_item_one_operation")
        if attempts != 1:
            raise ValueError("exactly one mutation attempt is allowed")
        if workspace != str(authorized_workspace_slug or "").strip():
            raise PermissionError("authorized workspace does not match target")
        if project != str(authorized_project_id or "").strip():
            raise PermissionError("authorized project does not match target")
        if method == "PATCH" and item != str(authorized_work_item_id or "").strip():
            raise PermissionError("authorized work item does not match target")
        if not workspace or not project:
            raise ValueError("workspace_slug and project_id are required")
        if method == "PATCH" and not item:
            raise ValueError("work_item_id is required for update")

        clean_payload = dict(payload or {})
        if not clean_payload:
            raise ValueError("payload must not be empty")
        allowed_fields = _CREATE_FIELDS if method == "POST" else _UPDATE_FIELDS
        unknown = sorted(set(clean_payload) - allowed_fields)
        if unknown:
            raise ValueError(f"unsupported work item payload fields: {', '.join(unknown)}")
        if method == "POST" and not str(clean_payload.get("name") or "").strip():
            raise ValueError("create payload requires a non-empty name")
        key = str(idempotency_key or "").strip()
        if len(key) < 16 or len(key) > 200:
            raise ValueError("idempotency_key must contain 16 to 200 characters")

        action = "create_work_item" if method == "POST" else "update_work_item"
        fingerprint = _fingerprint(action, workspace, project, item, clean_payload)
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()
        store = Path(ledger_path or Path.home() / ".hermes/state/plane-governed-mutations.json")
        path = (
            f"/api/v1/workspaces/{quote(workspace, safe='')}/projects/"
            f"{quote(project, safe='')}/work-items/"
        )
        if item:
            path += f"{quote(item, safe='')}/"

        with _locked_ledger(store) as ledger:
            previous = ledger["receipts"].get(key_hash)
            if previous:
                if previous.get("fingerprint") != fingerprint:
                    raise ValueError("idempotency_key was already used for a different mutation")
                provider_id = str(previous.get("provider_id") or "")
                readback = self._governed_request(
                    "GET",
                    (
                        f"/api/v1/workspaces/{quote(workspace, safe='')}/projects/"
                        f"{quote(project, safe='')}/work-items/{quote(provider_id, safe='')}/"
                    ),
                )
                _verify_readback(provider_id, clean_payload, readback)
                return _mutation_receipt(
                    action,
                    workspace,
                    project,
                    provider_id,
                    readback,
                    base_url=self.config.base_url,
                    mutation_applied=False,
                    duplicate=True,
                )

            response = self._governed_request(method, path, payload=clean_payload)
            if not isinstance(response, Mapping) or not str(response.get("id") or "").strip():
                raise RuntimeError("Plane mutation did not return a work item id")
            provider_id = str(response["id"])
            # Persist the provider id before readback. If Plane applied the write but the
            # GET is transiently unavailable, retrying the same key performs only GET.
            ledger["receipts"][key_hash] = {
                "action": action,
                "workspace_slug": workspace,
                "project_id": project,
                "provider_id": provider_id,
                "fingerprint": fingerprint,
            }
            readback = self._governed_request(
                "GET",
                (
                    f"/api/v1/workspaces/{quote(workspace, safe='')}/projects/"
                    f"{quote(project, safe='')}/work-items/{quote(provider_id, safe='')}/"
                ),
            )
            _verify_readback(provider_id, clean_payload, readback)
            return _mutation_receipt(
                action,
                workspace,
                project,
                provider_id,
                readback,
                base_url=self.config.base_url,
                mutation_applied=True,
                duplicate=False,
            )

    def _governed_request(
        self,
        method: str,
        path: str,
        *,
        payload: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> Any:
        if method not in {"GET", "POST", "PATCH"}:
            raise PermissionError("only governed work item create/update operations are supported")
        url = _build_url(
            self.config.base_url,
            path,
            None if self._transport and hasattr(self._transport, "request") else query,
        )
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "hermes-plane-api/1.0",
            **self.headers(),
        }
        try:
            if self._transport is not None and hasattr(self._transport, "request"):
                response = self._transport.request(
                    method,
                    url,
                    headers=headers,
                    params=query or {},
                    json_body=payload,
                    timeout=self.config.timeout,
                )
                return _response_body(response)
            body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
            request = Request(url, data=body, headers=headers, method=method)
            response = (
                self._transport(request, self.config.timeout)
                if self._transport is not None
                else urlopen(request, timeout=self.config.timeout)  # noqa: S310
            )
            return _decode(response)
        except HTTPError as exc:
            raise RuntimeError(f"Plane governed mutation failed with HTTP {exc.code}") from exc
        except URLError as exc:
            raise RuntimeError("Plane governed mutation failed") from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        allow_not_found: bool = False,
        with_metadata: bool = False,
        phase: str = "request",
    ) -> Any:
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise ValueError("unsupported Plane HTTP method")
        url = _build_url(self.config.base_url, path, None if self._transport and hasattr(self._transport, "request") else query)
        headers = {"Accept": "application/json", "User-Agent": "hermes-plane-api/1.0", **self.headers()}
        try:
            if self._transport is not None and hasattr(self._transport, "request"):
                response = self._transport.request(method, url, headers=headers, params=query or {}, json_body=payload, timeout=self.config.timeout)
                status = _response_status(response)
                if status is not None and status >= 400:
                    if allow_not_found and status == 404:
                        return {"status_code": 404}
                    raise _provider_error_from_response(response, phase=phase)
                return response if with_metadata else _response_body(response)
            body = None if payload is None else json.dumps(dict(payload)).encode("utf-8")
            request = Request(url, data=body, headers={**headers, **({"Content-Type": "application/json"} if body is not None else {})}, method=method)
            response = self._transport(request, self.config.timeout) if self._transport is not None else urlopen(request, timeout=self.config.timeout)  # noqa: S310
            body = _decode(response)
            if with_metadata:
                return {"status_code": response.getcode(), "body": body}
            return body
        except HTTPError as exc:
            if allow_not_found and exc.code == 404:
                return {"status_code": 404}
            raise PlaneProviderRequestError(
                phase=phase,
                http_status=exc.code,
                transport_class="http",
                deterministic=_deterministic_write_rejection(phase, exc.code),
                detail=_sanitize_http_error_detail(exc),
            ) from exc
        except URLError as exc:
            raise PlaneProviderRequestError(
                phase=phase,
                http_status=None,
                transport_class="network",
                deterministic=False,
                detail={"message": "provider request could not be completed"},
            ) from exc


@contextmanager
def _locked_ledger(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            try:
                ledger = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            except (OSError, json.JSONDecodeError) as exc:
                raise RuntimeError("Plane mutation idempotency ledger is unreadable") from exc
            if not isinstance(ledger, dict):
                raise RuntimeError("Plane mutation idempotency ledger has invalid shape")
            receipts = ledger.setdefault("receipts", {})
            if not isinstance(receipts, dict):
                raise RuntimeError("Plane mutation idempotency receipts have invalid shape")
            try:
                yield ledger
            finally:
                _persist_ledger(path, ledger)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def migrate_legacy_ledgers(target: str | Path, sources: Sequence[str | Path]) -> Path:
    """Merge compatible historical ledgers without discarding collisions.

    A conflicting key is preserved in migration metadata and blocks reuse of
    only that key.  Unrelated operations continue through the canonical ledger.
    """
    destination = _require_ledger_path(target)
    source_paths = [Path(source).expanduser() for source in sources]
    source_paths = [path for path in source_paths if path.is_file() and path.resolve() != destination.resolve()]
    if not source_paths:
        return destination
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for source in source_paths:
        try:
            raw = source.read_text(encoding="utf-8").strip()
            value = json.loads(raw) if raw else {}
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"legacy Plane mutation ledger is unreadable: {source}") from exc
        if not isinstance(value, dict) or not isinstance(value.get("receipts", {}), dict):
            raise RuntimeError(f"legacy Plane mutation ledger has invalid shape: {source}")
        loaded.append((source, value))
    with _locked_ledger(destination) as ledger:
        migration = ledger.setdefault("migration", {})
        conflicts = ledger.setdefault("migration_conflicts", {})
        if not isinstance(migration, dict) or not isinstance(conflicts, dict):
            raise RuntimeError("Plane mutation ledger migration metadata has invalid shape")
        for section in ("receipts", "authorizations", "reconciliation_authorizations"):
            destination_section = ledger.setdefault(section, {})
            section_conflicts = conflicts.setdefault(section, {})
            if not isinstance(destination_section, dict) or not isinstance(section_conflicts, dict):
                raise RuntimeError(f"Plane mutation ledger {section} has invalid migration shape")
            for source, payload in loaded:
                values = payload.get(section, {})
                if not isinstance(values, dict):
                    raise RuntimeError(f"legacy Plane mutation ledger {section} has invalid shape: {source}")
                for key, value in values.items():
                    if key not in destination_section:
                        destination_section[key] = value
                    elif destination_section[key] != value:
                        conflict_sources = section_conflicts.setdefault(str(key), [])
                        if str(source) not in conflict_sources:
                            conflict_sources.append(str(source))
        sources_seen = {str(value) for value in migration.get("sources", []) if isinstance(value, str)}
        sources_seen.update(str(source) for source, _ in loaded)
        migration["sources"] = sorted(sources_seen)
        migration["version"] = 1
    return destination


def _persist_ledger(path: Path, ledger: Mapping[str, Any]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(
            json.dumps(ledger, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _cas_receipt(
    ledger: Mapping[str, Any], key_hash: str, fingerprint: str, updates: Mapping[str, Any]
) -> None:
    """Apply a post-I/O transition only to the reservation we originally made."""
    receipts = ledger.get("receipts")
    if not isinstance(receipts, dict):
        raise RuntimeError("Plane mutation receipt disappeared during state transition")
    receipt = receipts.get(key_hash)
    if not isinstance(receipt, dict) or receipt.get("fingerprint") != fingerprint:
        raise RuntimeError("Plane mutation receipt changed during provider operation")
    receipt.update(dict(updates))


def _fingerprint(
    action: str,
    workspace_slug: str,
    project_id: str,
    work_item_id: str | None,
    payload: Mapping[str, Any],
) -> str:
    canonical = json.dumps(
        {
            "action": action,
            "workspace_slug": workspace_slug,
            "project_id": project_id,
            "work_item_id": work_item_id,
            "payload": dict(payload),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _provider_instance_hash(config: PlaneConfig) -> str:
    """Stable, non-secret provider identity suitable for authorization binding."""
    parts = urlsplit(config.base_url)
    canonical = json.dumps(
        {"scheme": parts.scheme.lower(), "host": (parts.hostname or "").lower(), "port": parts.port, "instance_id": config.instance_id},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_ledger_path(value: str | Path | None) -> Path:
    if value is None or not str(value).strip():
        raise ValueError("explicit runtime ledger_path is required for governed mutation state")
    return Path(value)


def _request_body_available(operation: Any) -> bool:
    """Return the registry's explicit body availability, failing closed if absent."""
    request_schema = getattr(operation, "request_schema", None)
    if not isinstance(request_schema, Mapping):
        return False
    body = request_schema.get("body")
    return isinstance(body, Mapping) and body.get("available") is True


def _module_work_item_ids(response: Any) -> set[str]:
    if isinstance(response, list):
        rows = response
    elif isinstance(response, Mapping) and isinstance(response.get("results"), list):
        rows = response["results"]
    else:
        rows = []
    return {
        str(row.get("id") or row.get("issue") or row.get("work_item") or "").strip()
        for row in rows
        if isinstance(row, Mapping)
        and str(row.get("id") or row.get("issue") or row.get("work_item") or "").strip()
    }


def _verify_module_binding_readback(work_item_id: str, readback: Any) -> None:
    if work_item_id not in _module_work_item_ids(readback):
        raise RuntimeError("Plane module binding readback mismatch")


def _verify_readback(
    provider_id: str, payload: Mapping[str, Any], readback: Any
) -> None:
    if not isinstance(readback, Mapping):
        raise RuntimeError("Plane mutation readback returned an invalid response")
    if str(readback.get("id") or "") != provider_id:
        raise RuntimeError("Plane mutation readback did not match the provider id")
    mismatched = sorted(
        key
        for key in set(payload) & _READBACK_SCALARS
        if key in readback and readback.get(key) != payload.get(key)
    )
    if mismatched:
        raise RuntimeError(
            f"Plane mutation readback mismatch for fields: {', '.join(mismatched)}"
        )


def _mutation_receipt(
    action: str,
    workspace_slug: str,
    project_id: str,
    provider_id: str,
    readback: Mapping[str, Any],
    *,
    base_url: str,
    mutation_applied: bool,
    duplicate: bool,
) -> dict[str, Any]:
    return {
        "governed": True,
        "action": action,
        "workspace_slug": workspace_slug,
        "project_id": project_id,
        "provider_id": provider_id,
        "web_url": _work_item_web_url(
            base_url, workspace_slug, project_id, provider_id
        ),
        "sequence_id": readback.get("sequence_id"),
        "updated_at": readback.get("updated_at"),
        "mutation_applied": mutation_applied,
        "duplicate": duplicate,
        "readback_verified": True,
    }


def _verify_comment_readback(
    comment_id: str, payload: Mapping[str, Any], readback: Any
) -> None:
    if not isinstance(readback, Mapping):
        raise RuntimeError("Plane comment readback returned an invalid response")
    if str(readback.get("id") or "") != comment_id:
        raise RuntimeError("Plane comment readback did not match the provider id")
    mismatched = sorted(
        key
        for key in _COMMENT_FIELDS
        if key in payload and key in readback and readback.get(key) != payload.get(key)
    )
    if mismatched:
        raise RuntimeError(
            f"Plane comment readback mismatch for fields: {', '.join(mismatched)}"
        )


def _comment_receipt(
    workspace_slug: str,
    project_id: str,
    work_item_id: str,
    comment_id: str,
    readback: Mapping[str, Any],
    *,
    base_url: str,
    mutation_applied: bool,
    duplicate: bool,
) -> dict[str, Any]:
    return {
        "governed": True,
        "action": "add_work_item_comment",
        "workspace_slug": workspace_slug,
        "project_id": project_id,
        "work_item_id": work_item_id,
        "comment_id": comment_id,
        "web_url": _work_item_web_url(
            base_url, workspace_slug, project_id, work_item_id
        ),
        "updated_at": readback.get("updated_at"),
        "mutation_applied": mutation_applied,
        "duplicate": duplicate,
        "readback_verified": True,
    }


def _work_item_web_url(
    base_url: str, workspace_slug: str, project_id: str, work_item_id: str
) -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("Plane base_url must be an absolute HTTP(S) URL")
    origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    return (
        f"{origin}/{quote(str(workspace_slug), safe='')}/projects/"
        f"{quote(str(project_id), safe='')}/issues/"
        f"{quote(str(work_item_id), safe='')}"
    )


def _required(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"path parameter is required: {name}")
    return quote(text, safe="")


def _expand_path(template: str, params: Mapping[str, Any]) -> str:
    path = template
    import re

    for name in re.findall(r"\{([^}]+)\}", template):
        if name not in params:
            raise ValueError(f"path parameter is required: {name}")
        path = path.replace("{" + name + "}", _required(params[name], name))
    return path


def _build_url(base: str, path: str, query: Mapping[str, Any] | None) -> str:
    url = urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    clean = {str(key): str(value) for key, value in (query or {}).items() if value is not None}
    return url if not clean else f"{url}?{urlencode(clean)}"


def _decode(response: Any) -> Any:
    if isinstance(response, (dict, list)):
        return response
    raw = response.read() if hasattr(response, "read") else response
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return None if raw in (None, "") else json.loads(str(raw))


_ERROR_DETAIL_KEYS = frozenset({"code", "field", "message", "detail", "errors", "non_field_errors"})
_SENSITIVE_ERROR_KEY_FRAGMENTS = ("token", "authorization", "password", "secret", "api_key", "apikey", "x-api-key")
_ERROR_DETAIL_MAX_DEPTH = 4
_ERROR_DETAIL_MAX_ITEMS = 16
_ERROR_DETAIL_MAX_STRING = 512
_ERROR_DETAIL_MAX_BYTES = 4096
_ERROR_FIELD_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def _sanitize_http_error_detail(exc: HTTPError) -> dict[str, Any]:
    """Decode an HTTP error body once and retain only bounded public fields."""
    try:
        raw = exc.read()
        value = json.loads(raw.decode("utf-8")) if isinstance(raw, bytes) else json.loads(str(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {"message": "provider rejected request"}
    detail = _bounded_error_envelope(value)
    return detail


def _bounded_error_envelope(value: Any) -> dict[str, Any]:
    detail = _sanitize_error_detail(value)
    if not isinstance(detail, Mapping):
        detail = {"detail": detail}
    encoded = json.dumps(detail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return dict(detail) if len(encoded) <= _ERROR_DETAIL_MAX_BYTES else {"message": "provider rejected request"}


def _provider_error_from_response(response: Any, *, phase: str) -> PlaneProviderRequestError:
    status = _response_status(response)
    body = _response_body(response)
    return PlaneProviderRequestError(
        phase=phase,
        http_status=status,
        transport_class="http",
        deterministic=_deterministic_write_rejection(phase, status),
        detail=(
            _bounded_error_envelope(body)
            if isinstance(body, (Mapping, list))
            else {"message": "provider rejected request"}
        ),
    )


def _deterministic_write_rejection(phase: str, status: int | None) -> bool:
    """Only validation statuses with an unambiguous no-effect contract are terminal.

    Archive 409 is deliberately ambiguous: Plane may have applied the archive
    while racing a duplicate request, so it remains burned and requires proof.
    """
    if not phase.startswith("write:"):
        return False
    action = phase.partition(":")[2]
    if status in {400, 422}:
        return True
    if status == 409 and action != "module__archive_module":
        return False
    return False


def _reconciliation_error_code(error: Exception) -> str:
    if isinstance(error, PlaneProviderRequestError):
        return "provider_request_inconclusive"
    if isinstance(error, PlaneResponseValidationError):
        return "provider_schema_inconclusive"
    if isinstance(error, PlaneReadbackValidationError):
        return "provider_membership_inconclusive"
    return "pagination_or_runtime_inconclusive"


def _sanitize_error_detail(value: Any, *, depth: int = 0) -> Any:
    if depth > _ERROR_DETAIL_MAX_DEPTH:
        return "<truncated>"
    if isinstance(value, str):
        return value[:_ERROR_DETAIL_MAX_STRING]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_sanitize_error_detail(item, depth=depth + 1) for item in value[:_ERROR_DETAIL_MAX_ITEMS]]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        field_errors: dict[str, Any] = {}
        for key, item in list(value.items())[:_ERROR_DETAIL_MAX_ITEMS]:
            name = str(key)
            lowered = name.casefold().replace("-", "_")
            if any(fragment in lowered for fragment in _SENSITIVE_ERROR_KEY_FRAGMENTS):
                continue
            if name in _ERROR_DETAIL_KEYS:
                if name == "errors" and isinstance(item, Mapping):
                    result[name] = _sanitize_validation_fields(item, depth=depth + 1)
                else:
                    result[name] = _sanitize_error_detail(item, depth=depth + 1)
            elif _ERROR_FIELD_NAME.fullmatch(name):
                field_errors[name] = _sanitize_error_detail(item, depth=depth + 1)
        if field_errors:
            existing = result.get("errors")
            if isinstance(existing, Mapping):
                result["errors"] = {**existing, **field_errors}
            else:
                result["errors"] = field_errors
        return result or {"message": "provider rejected request"}
    return "<unavailable>"


def _sanitize_validation_fields(value: Mapping[str, Any], *, depth: int) -> dict[str, Any]:
    """Keep only bounded, non-sensitive field identifiers from validation maps."""
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:_ERROR_DETAIL_MAX_ITEMS]:
        name = str(key)
        lowered = name.casefold().replace("-", "_")
        if (
            _ERROR_FIELD_NAME.fullmatch(name)
            and not any(fragment in lowered for fragment in _SENSITIVE_ERROR_KEY_FRAGMENTS)
        ):
            result[name] = _sanitize_error_detail(item, depth=depth + 1)
    return result or {"_": "<unavailable>"}


def _payload_descriptor(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "keys": sorted(str(key) for key in payload),
        "shape": {str(key): {"type": type(value).__name__, **({"length": len(value)} if isinstance(value, (str, list, dict)) else {})} for key, value in payload.items()},
    }


def payload_fingerprint(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Private compatibility seam for callers that have not yet moved to the public
# receipt helper. New MCP surfaces must use ``payload_fingerprint``.
_payload_fingerprint = payload_fingerprint


def _provider_id(response: Any) -> str | None:
    body = _response_body(response)
    if isinstance(body, Mapping):
        for key in ("id", "uuid", "resource_id"):
            value = str(body.get(key) or "").strip()
            if value:
                return value
        rows = body.get("results")
        if isinstance(rows, list):
            for row in rows:
                value = _provider_id(row)
                if value:
                    return value
    return None


def _response_body(response: Any) -> Any:
    """Unwrap the small fake/transport envelope without exposing it."""
    if isinstance(response, Mapping) and "body" in response and _has_http_status(response):
        return response.get("body")
    if isinstance(response, Mapping) and _has_http_status(response) and len(response) == 1:
        return None
    return response


def _response_status(response: Any) -> int | None:
    if not isinstance(response, Mapping):
        return None
    for key in ("status_code", "http_status", "status"):
        value = response.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                if key == "status":
                    continue
                raise RuntimeError("Plane provider response has an invalid HTTP status")
    return None


def _has_http_status(response: Mapping[str, Any]) -> bool:
    return _response_status(response) is not None


def _response_metadata(response: Any, response_schema: Mapping[str, Any] | None = None) -> dict[str, Any]:
    body = _response_body(response)
    declared_shape = (response_schema or {}).get("shape")
    metadata: dict[str, Any] = {
        "status_code": _response_status(response),
        "body_present": body is not None,
        # This records only bounded structural evidence.  It is persisted
        # after the HTTP status is accepted even when later JSON-schema
        # validation fails, so operators can reconcile without retaining the
        # provider payload.
        "body_shape": _body_shape(body, declared_shape),
    }
    if isinstance(body, Mapping) and "acknowledged" in body:
        metadata["acknowledged"] = body.get("acknowledged") is True
    return metadata


def _body_shape(body: Any, declared_shape: str | None = None) -> str:
    if body is None or body == {}:
        return "empty"
    if isinstance(body, list):
        return declared_shape if declared_shape in {"collection", "relation_membership"} else "collection"
    if isinstance(body, Mapping) and isinstance(body.get("results"), list):
        return declared_shape if declared_shape in {"collection", "relation_membership"} else "collection"
    if isinstance(body, Mapping) and body.get("id") is not None:
        return "detail"
    if isinstance(body, Mapping):
        return "object"
    return "unknown"


def _expected_statuses(expected: Any, response_schema: Mapping[str, Any] | None) -> set[int]:
    if expected is None and isinstance(response_schema, Mapping):
        expected = response_schema.get("status")
    if expected is None:
        return set()
    values = expected if isinstance(expected, (list, tuple, set)) else [expected]
    try:
        return {int(value) for value in values}
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Plane mutation contract has invalid expected status semantics") from exc


def _validate_response_status(
    response: Any,
    expected: Any,
    response_schema: Mapping[str, Any] | None,
    *,
    context: str,
) -> None:
    statuses = _expected_statuses(expected, response_schema)
    actual = _response_status(response)
    if statuses and actual is None:
        raise RuntimeError(f"Plane {context} response did not include an HTTP status")
    if actual is not None and statuses and actual not in statuses:
        raise RuntimeError(f"Plane {context} response status {actual} is not one of {sorted(statuses)}")


def _schema_required(response_schema: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(response_schema, Mapping):
        return []
    json_schema = response_schema.get("json_schema")
    # Older locally generated registries nested this object under ``schema``.
    # The checked-in contract is direct; the fallback keeps malformed/legacy
    # inputs fail-closed rather than silently dropping required fields.
    if not isinstance(json_schema, Mapping):
        schema = response_schema.get("schema")
        json_schema = schema.get("json_schema") if isinstance(schema, Mapping) else None
    if not isinstance(json_schema, Mapping):
        return []
    return [str(value) for value in (json_schema.get("required") or [])]


def _validate_json_schema(value: Any, schema: Mapping[str, Any], *, context: str) -> None:
    # ``null`` is valid only when the declared schema explicitly includes it.
    # Required-field presence and value nullability are separate guarantees:
    # a required non-nullable field must reject both an omitted key and JSON
    # null, including on a persisted duplicate/retry readback.
    if value is None:
        schema_type = schema.get("type")
        if schema_type == "null":
            return
        if isinstance(schema_type, (list, tuple, set)) and "null" in schema_type:
            return
        any_of = schema.get("anyOf") or schema.get("oneOf")
        if isinstance(any_of, (list, tuple)) and any(
            isinstance(option, Mapping) and option.get("type") == "null"
            for option in any_of
        ):
            return
        raise PlaneResponseValidationError(f"Plane {context} response JSON field has invalid type")
    schema_type = schema.get("type")
    if isinstance(schema_type, (list, tuple, set)):
        for candidate in schema_type:
            if candidate == "null":
                continue
            try:
                _validate_json_schema(
                    value, {**schema, "type": candidate}, context=context
                )
                return
            except PlaneResponseValidationError:
                continue
        raise PlaneResponseValidationError(
            f"Plane {context} response JSON field has invalid type"
        )
    if schema_type == "object":
        if not isinstance(value, Mapping):
            raise PlaneResponseValidationError(f"Plane {context} response JSON shape is not object")
        properties = schema.get("properties") or {}
        missing = [
            field
            for field in schema.get("required") or ()
            if field not in value and _schema_alias(value, str(field)) is None
        ]
        if missing:
            raise PlaneResponseValidationError(
                f"Plane {context} response is missing documented fields: {', '.join(map(str, missing))}"
            )
        for field, field_schema in properties.items():
            alias = _schema_alias(value, str(field))
            if isinstance(field_schema, Mapping) and (field in value or alias is not None):
                _validate_json_schema(
                    value[field] if field in value else value[alias],
                    field_schema,
                    context=context,
                )
    elif schema_type == "array":
        if not isinstance(value, list):
            raise PlaneResponseValidationError(f"Plane {context} response JSON shape is not array")
        item_schema = schema.get("items")
        if isinstance(item_schema, Mapping):
            for item in value:
                _validate_json_schema(item, item_schema, context=context)
    elif schema_type == "string" and not isinstance(value, str):
        raise PlaneResponseValidationError(f"Plane {context} response JSON field has invalid type")
    elif schema_type == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        raise PlaneResponseValidationError(f"Plane {context} response JSON field has invalid type")
    elif schema_type == "number" and (not isinstance(value, (int, float)) or isinstance(value, bool)):
        raise PlaneResponseValidationError(f"Plane {context} response JSON field has invalid type")
    elif schema_type == "boolean" and not isinstance(value, bool):
        raise PlaneResponseValidationError(f"Plane {context} response JSON field has invalid type")


def _schema_alias(value: Mapping[str, Any], field: str) -> str | None:
    """Resolve only reviewed aliases; the caller still validates their value."""
    aliases = {
        "description": ("description_html", "description_stripped"),
    }
    return next((alias for alias in aliases.get(field, ()) if alias in value), None)


def _validate_mutation_write_response(
    operation: Any,
    response: Any,
    payload: Mapping[str, Any],
) -> None:
    """Validate only the provider evidence needed before a postcondition GET.

    Plane's comment endpoint can return a minimal ``{"id": ...}`` body despite
    its published example including actor and rendered-content fields. The
    membership readback remains mandatory and verifies that identifier. A 204
    endpoint has no body contract, so an empty body is valid provider evidence.
    """
    body = _response_body(response)
    if operation.action == "issue_comment__add_issue_comment":
        if not isinstance(body, Mapping) or not str(body.get("id") or "").strip():
            raise PlaneResponseValidationError("comment mutation response lacks a provider id")
        return
    response_schema = operation.response_schema or {}
    if response_schema.get("available") is False and body in (None, {}, ""):
        return
    _validate_response_shape(
        body,
        {"response_shape": response_schema.get("shape")},
        response_schema,
        context="mutation",
    )


def _validate_response_shape(
    body: Any,
    specification: Mapping[str, Any],
    response_schema: Mapping[str, Any] | None,
    *,
    context: str,
    operation_action: str | None = None,
) -> None:
    shape = specification.get("response_shape")
    if shape is None:
        shape = response_schema.get("shape") if isinstance(response_schema, Mapping) else None
    if shape == "empty":
        if body not in (None, {}):
            raise PlaneResponseValidationError(f"Plane {context} response shape is not empty")
        return
    if shape in {"detail", "object"}:
        if not isinstance(body, Mapping) or not body:
            raise PlaneResponseValidationError(f"Plane {context} response shape is not {shape}")
    elif shape in {"collection", "relation_membership"}:
        if not isinstance(body, list) and not (
            isinstance(body, Mapping) and isinstance(body.get("results"), list)
        ):
            raise PlaneResponseValidationError(f"Plane {context} response shape is not {shape}")
    elif shape == "relation_map":
        if not isinstance(body, Mapping):
            raise PlaneResponseValidationError(f"Plane {context} response shape is not relation_map")
    elif shape is not None and not isinstance(body, (Mapping, list)):
        raise PlaneResponseValidationError(f"Plane {context} response shape is invalid")

    if isinstance(response_schema, Mapping):
        schema = response_schema.get("json_schema")
        if isinstance(schema, Mapping):
            schema = _provider_compatible_response_schema(schema, operation_action=operation_action)
            if shape in {"collection", "relation_membership"}:
                if schema.get("type") == "object":
                    _validate_json_schema(body, schema, context=context)
                else:
                    rows = body if isinstance(body, list) else body.get("results", []) if isinstance(body, Mapping) else []
                    item_schema = schema.get("items")
                    if (
                        isinstance(item_schema, Mapping)
                        and item_schema.get("type") == "object"
                        and "results" in (item_schema.get("properties") or {})
                        and isinstance(body, Mapping)
                        and "results" in body
                    ):
                        # Some documented collection examples wrap an array
                        # item schema around the pagination envelope.
                        _validate_json_schema(body, item_schema, context=context)
                        return
                    if isinstance(item_schema, Mapping) and item_schema.get("type") == "array" and isinstance(body, Mapping):
                        item_schema = item_schema.get("items")
                    if isinstance(item_schema, Mapping):
                        for row in rows:
                            _validate_json_schema(row, item_schema, context=context)
            else:
                _validate_json_schema(body, schema, context=context)


def _validate_absence_collection_envelope(
    body: Any,
    specification: Mapping[str, Any],
    *,
    context: str,
) -> None:
    """Validate collection evidence needed to prove a mutation target is absent.

    An absence readback must not reject a valid page because an unrelated
    surviving representation has drifted from the provider's documented detail
    schema.  It still requires a resolvable collection and its documented
    pagination continuation signal so the caller can exhaust every page.
    """
    if specification.get("response_shape") == "relation_map":
        if not isinstance(body, Mapping) or not all(isinstance(value, list) for value in body.values()):
            raise PlaneResponseValidationError(f"Plane {context} response shape is not a relation map")
        return
    if isinstance(body, list):
        return
    if not isinstance(body, Mapping) or not isinstance(body.get("results"), list):
        raise PlaneResponseValidationError(f"Plane {context} response shape is not a collection")
    pagination = specification.get("pagination") or {}
    next_flag = str(pagination.get("next_page_flag") or "next_page_results")
    if not isinstance(body.get(next_flag), bool):
        raise PlaneResponseValidationError(
            f"Plane {context} response lacks a boolean pagination continuation flag"
        )


def _provider_compatible_response_schema(
    schema: Mapping[str, Any], *, operation_action: str | None = None
) -> Mapping[str, Any]:
    """Apply reviewed Plane response compatibility without weakening identity.

    Plane's module detail/update responses return the optional display and date
    fields as JSON null in normal operation, although the published examples
    currently type them as strings.  Recognize the module-detail schema by its
    stable required-field signature, then relax only those three optional
    value types.  Required keys, module identity, counters, status, and
    timestamps remain strictly validated.
    """

    if operation_action == "issue_comment__list_issue_comments":
        properties = schema.get("properties")
        results = properties.get("results") if isinstance(properties, Mapping) else None
        items = results.get("items") if isinstance(results, Mapping) else None
        required_fields = items.get("required") if isinstance(items, Mapping) else None
        if isinstance(properties, Mapping) and isinstance(results, Mapping) and isinstance(items, Mapping) and isinstance(required_fields, list) and "name" in required_fields:
            compatible_items = {**dict(items), "required": [field for field in required_fields if field != "name"]}
            compatible_results = {**dict(results), "items": compatible_items}
            schema = {**dict(schema), "properties": {**dict(properties), "results": compatible_results}}

    required = set(schema.get("required") or ())
    module_signature = {
        "id",
        "name",
        "status",
        "total_issues",
        "completed_issues",
        "cancelled_issues",
        "started_issues",
        "unstarted_issues",
        "backlog_issues",
    }
    if not module_signature <= required:
        return schema
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return schema
    relaxed = dict(properties)
    changed = False
    for field in ("description", "start_date", "target_date"):
        field_schema = relaxed.get(field)
        if isinstance(field_schema, Mapping) and field_schema.get("type") == "string":
            relaxed[field] = {**field_schema, "type": ["string", "null"]}
            changed = True
    return {**schema, "properties": relaxed} if changed else schema


def _validate_provider_acknowledgement(
    response: Any,
    response_schema: Mapping[str, Any] | None,
    specification: Mapping[str, Any],
) -> None:
    body = _response_body(response)
    _validate_response_shape(body, specification, response_schema, context="acknowledgement")
    if isinstance(body, Mapping) and "acknowledged" in body and body.get("acknowledged") is not True:
        raise PlaneResponseValidationError("Plane provider acknowledgement is missing or false")
    shape = specification.get("response_shape")
    if shape not in {None, "empty"} and body in (None, {}):
        raise PlaneResponseValidationError("Plane provider acknowledgement body is missing")
    if shape == "empty" and body not in (None, {}):
        raise PlaneResponseValidationError("Plane provider acknowledgement response shape is not empty")


def _validate_module_update_ack(body: Any, payload: Mapping[str, Any]) -> None:
    """Validate Plane's observed sparse module PATCH representation."""

    if not isinstance(body, Mapping) or not body:
        raise PlaneResponseValidationError("Plane module update acknowledgement is not an object")
    mismatches = [field for field, expected in payload.items() if body.get(field) != expected]
    if mismatches:
        raise PlaneResponseValidationError(
            "Plane module update acknowledgement effect mismatch: "
            + ", ".join(sorted(mismatches))
        )


def _validate_persisted_module_update_ack(
    metadata: Mapping[str, Any] | None,
    specification: Mapping[str, Any],
) -> None:
    """Validate safe metadata from Plane's observed sparse PATCH object."""

    if not isinstance(metadata, Mapping):
        raise RuntimeError("persisted Plane module update evidence is missing")
    if metadata.get("status_code") != specification.get("expected_status"):
        raise RuntimeError("persisted Plane module update status does not match contract")
    if metadata.get("body_shape") != "object" or metadata.get("body_present") is not True:
        raise RuntimeError("persisted Plane module update acknowledgement is not an object")


def _validate_persisted_acknowledgement(
    metadata: Mapping[str, Any] | None,
    specification: Mapping[str, Any],
) -> None:
    if not isinstance(metadata, Mapping):
        raise RuntimeError("Plane provider acknowledgement metadata is missing")
    if metadata.get("status_code") != specification.get("expected_status"):
        raise RuntimeError("Plane provider acknowledgement persisted status does not match contract")
    if metadata.get("body_shape") != specification.get("response_shape"):
        raise RuntimeError("Plane provider acknowledgement persisted response shape does not match contract")
    if metadata.get("body_present") is False and specification.get("response_shape") not in {None, "empty"}:
        raise RuntimeError("Plane provider acknowledgement metadata is missing")
    if metadata.get("acknowledged") is False:
        raise RuntimeError("Plane provider acknowledgement is missing or false")


def _validate_persisted_write_evidence(
    metadata: Mapping[str, Any] | None,
    specification: Mapping[str, Any],
    response_schema: Mapping[str, Any] | None,
) -> None:
    if not isinstance(metadata, Mapping):
        raise RuntimeError("persisted Plane write evidence is missing")
    expected = _expected_statuses(specification.get("expected_status"), response_schema)
    if metadata.get("status_code") not in expected:
        raise RuntimeError("persisted Plane write evidence status does not match contract")
    expected_shape = str((response_schema or {}).get("shape") or "unknown")
    if metadata.get("body_shape") != expected_shape:
        raise RuntimeError("persisted Plane write evidence shape does not match contract")


def get_operation_for_postcondition(action: str):
    try:
        return get_operation(action)
    except ValueError as exc:
        raise RuntimeError("Plane mutation postcondition references an unknown GET action") from exc


def _validate_postcondition_bindings(
    specification: Mapping[str, Any],
    params: Mapping[str, Any],
    expanded_path: str,
    payload: Mapping[str, Any] | None = None,
) -> None:
    path = str(specification.get("path") or "")
    if not path or "{" in expanded_path or "}" in expanded_path:
        raise RuntimeError("Plane mutation postcondition path binding is unresolved")
    for binding in specification.get("bindings") or ():
        mutation_name = str(binding.get("mutation") or "")
        if mutation_name and mutation_name in params and not str(params[mutation_name]).strip():
            raise RuntimeError("Plane mutation postcondition path binding is empty")
        payload_name = str(binding.get("payload") or "")
        if payload_name and not str((payload or {}).get(payload_name) or "").strip():
            raise RuntimeError("Plane mutation postcondition payload binding is empty")


def _is_collection_shape(shape: Any) -> bool:
    return shape in {"collection", "relation_membership", "relation_map"}


def _rows(response_body: Any) -> list[Any] | None:
    if isinstance(response_body, list):
        return response_body
    if isinstance(response_body, Mapping) and isinstance(response_body.get("results"), list):
        return response_body["results"]
    return None


def _validate_module_reconciliation_collection(body: Any, *, context: str) -> None:
    """Keep envelope/pagination strict while accepting nullable module details.

    Reconciliation proves membership, not the optional display/date fields of
    a module. Provider schemas have those optional fields nullable in practice.
    """
    if not isinstance(body, Mapping) or not isinstance(body.get("results"), list):
        raise PlaneResponseValidationError(f"Plane {context} response shape is not collection")
    for name in ("count", "total_count", "total_results", "total_pages"):
        if name in body and not isinstance(body[name], int):
            raise PlaneResponseValidationError(f"Plane {context} {name} is not an integer")
    for name in ("next_page_results", "prev_page_results"):
        if name in body and not isinstance(body[name], bool):
            raise PlaneResponseValidationError(f"Plane {context} {name} is not a boolean")
    for name in ("next_cursor", "prev_cursor"):
        if name in body and body[name] is not None and not isinstance(body[name], str):
            raise PlaneResponseValidationError(f"Plane {context} {name} is not a string")
    for row in body["results"]:
        if not isinstance(row, Mapping) or not str(row.get("id") or "").strip():
            raise PlaneResponseValidationError(f"Plane {context} module item lacks stable id")


def _validate_identity_bindings(
    body: Any,
    specification: Mapping[str, Any],
    params: Mapping[str, Any],
) -> None:
    """Reject a representation that claims a different bound container."""
    bindings = specification.get("bindings") or ()
    rows = _rows(body)
    representations = rows if rows is not None else [body]
    for binding in bindings:
        mutation_name = str(binding.get("mutation") or "")
        readback_name = str(binding.get("readback") or "")
        if mutation_name not in params or not readback_name:
            continue
        expected = str(params[mutation_name]).strip()
        for representation in representations:
            if not isinstance(representation, Mapping) or readback_name not in representation:
                continue
            actual = str(representation.get(readback_name) or "").strip()
            if actual != expected:
                raise PlaneReadbackValidationError(
                    f"Plane mutation readback binding mismatch for {readback_name}"
                )


def _row_identifiers(row: Any) -> set[str]:
    if not isinstance(row, Mapping):
        return set()
    return {
        str(row.get(name)).strip()
        for name in ("id", "uuid", "resource_id", "issue", "work_item", "member", "value_id")
        if str(row.get(name) or "").strip()
    }


def _readback_contains_targets(
    body: Any,
    targets: set[str],
    specification: Mapping[str, Any],
    *,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    targets = {str(target).strip() for target in targets if str(target).strip()}
    if not targets:
        raise PlaneReadbackValidationError("Plane mutation readback has no target identity to match")
    shape = specification.get("response_shape")
    matcher = specification.get("matcher") or {}
    field = str(matcher.get("field") or "id")
    if shape == "relation_map":
        if not isinstance(body, Mapping):
            raise PlaneReadbackValidationError("Plane relation-map readback is not an object")
        relation_type = None
        if (specification.get("membership") or {}).get("relation_type_source") == "payload":
            relation_type = str((payload or {}).get("relation_type") or "").strip()
        groups = [body.get(relation_type)] if relation_type else list(body.values())
        found = {
            str(identifier).strip()
            for group in groups
            if isinstance(group, list)
            for identifier in group
            if str(identifier).strip()
        }
        if specification.get("membership", {}).get("target_presence") == "present":
            return targets <= found
        return bool(targets & found)
    if shape in {"collection", "relation_membership"}:
        rows = _rows(body)
        if rows is None:
            raise PlaneReadbackValidationError("Plane mutation list readback has no resolvable results")
        found = {
            identifier
            for row in rows
            for identifier in _row_identifiers(row)
        }
        found.update(
            str(row.get(field)).strip()
            for row in rows
            if isinstance(row, Mapping) and str(row.get(field) or "").strip()
        )
        return targets <= found if specification.get("membership", {}).get("target_presence") == "present" else bool(targets & found)
    if isinstance(body, Mapping):
        value = str(body.get(field) or body.get("id") or body.get("uuid") or "").strip()
        return value in targets
    return False


def _validate_detail_effects(
    body: Any,
    payload: Mapping[str, Any],
    specification: Mapping[str, Any],
) -> None:
    if not isinstance(body, Mapping):
        raise PlaneReadbackValidationError("Plane detail readback cannot verify effects")
    declared = set((specification.get("effects") or {}).get("payload_fields") or ())
    missing = sorted(field for field in declared if field in payload and field not in body)
    if missing:
        raise PlaneReadbackValidationError(f"Plane detail readback is missing effect fields: {', '.join(missing)}")
    mismatched = sorted(
        field for field in declared
        if field in payload and (field not in body or body.get(field) != payload.get(field))
    )
    if mismatched:
        raise PlaneReadbackValidationError(f"Plane detail readback effect mismatch: {', '.join(mismatched)}")


def _has_next_page(body: Any, specification: Mapping[str, Any]) -> bool:
    pagination = specification.get("pagination") or {}
    flag = pagination.get("next_page_flag")
    return bool(flag and isinstance(body, Mapping) and body.get(flag) is True)


def _next_cursor(body: Any, specification: Mapping[str, Any]) -> str | None:
    if not isinstance(body, Mapping):
        return None
    value = body.get("next_cursor")
    return str(value).strip() if value is not None else None


def _readback_query(specification: Mapping[str, Any], cursor: str | None = None) -> dict[str, Any]:
    query = dict(specification.get("fixed_query") or {})
    pagination = specification.get("pagination") or {}
    if cursor is not None:
        parameter = str(pagination.get("cursor_parameter") or "cursor")
        query[parameter] = cursor
    return query


def _mutation_target_id(params: Mapping[str, Any], provider_id: str | None) -> str | None:
    if provider_id:
        return str(provider_id)
    for name in (
        "resource_id",
        "work_item_id",
        "issue_id",
        "comment_id",
        "attachment_id",
        "member_id",
        "label_id",
        "option_id",
        "page_id",
        "property_id",
        "state_id",
        "type_id",
        "module_id",
        "cycle_id",
    ):
        value = str(params.get(name) or "").strip()
        if value:
            return value
    # The registry retains provider-specific names that are not all present in
    # the compact compatibility list above.  A path-bound *_id is still a
    # concrete target, whereas arbitrary payload identifiers are not.
    for name, value in reversed(tuple(params.items())):
        if str(name).endswith("_id"):
            text = str(value or "").strip()
            if text:
                return text
    return None


def _target_values(value: Any) -> list[str]:
    if isinstance(value, (list, tuple, set)):
        return [item for child in value for item in _target_values(child)]
    if isinstance(value, Mapping):
        for name in ("id", "uuid", "resource_id", "issue", "work_item", "member", "value_id"):
            if str(value.get(name) or "").strip():
                return [str(value[name]).strip()]
        return []
    text = str(value or "").strip()
    return [text] if text else []


def _mutation_targets(
    specification: Mapping[str, Any],
    params: Mapping[str, Any],
    payload: Mapping[str, Any],
    response: Any | None,
    provider_id: str | None,
) -> set[str]:
    targets = specification.get("targets") or {}
    selector = targets.get("selector")
    if not isinstance(selector, Mapping) or not selector.get("source") or not selector.get("field"):
        raise RuntimeError("Plane mutation contract has no explicit affected-resource selector")
    source = str(selector["source"])
    field = str(selector["field"])
    body = _response_body(response) if response is not None else None
    selected: list[Any] = []
    if source == "path":
        if field in params:
            selected.append(params[field])
    elif source == "payload":
        if field in payload:
            selected.append(payload[field])
    elif source == "write_response":
        if isinstance(body, Mapping) and field in body:
            selected.append(body[field])
        elif isinstance(body, Mapping) and isinstance(body.get("results"), list):
            selected.extend(row.get(field) for row in body["results"] if isinstance(row, Mapping) and field in row)
        elif provider_id:
            # On an idempotent duplicate the original write body is not
            # replayed.  The persisted provider id is usable only for the
            # explicitly declared write-response identity selector.
            selected.append(provider_id)
    else:
        raise RuntimeError("Plane mutation contract has an invalid affected-resource selector")
    resolved = {item for value in selected for item in _target_values(value)}
    if not resolved:
        raise RuntimeError("Plane mutation target selector is empty or ambiguous")
    return resolved


def _postcondition_path(
    specification: Mapping[str, Any],
    params: Mapping[str, Any],
    provider_id: str | None,
    payload: Mapping[str, Any] | None = None,
) -> str | None:
    template = str(specification.get("path") or "")
    if not template:
        return None
    values = dict(params)
    for binding in specification.get("bindings") or ():
        mutation_name = str(binding.get("mutation") or "")
        readback_name = str(binding.get("readback") or "")
        if readback_name and mutation_name in values:
            values[readback_name] = values[mutation_name]
        elif readback_name and binding.get("payload") and str(binding["payload"]) in (payload or {}):
            values[readback_name] = (payload or {})[str(binding["payload"])]
        elif readback_name and provider_id and readback_name in template:
            values[readback_name] = provider_id
    # A create response can be the only documented source for the new detail
    # id.  Bind only an unresolved, non-container id slot from the explicit
    # postcondition target policy; never fill an arbitrary container id.
    if provider_id:
        import re

        for name in re.findall(r"\{([^}]+)\}", template):
            if name not in values and name.endswith("_id") and name not in {"project_id", "workspace_id"}:
                values[name] = provider_id
    try:
        return _expand_path(template, values)
    except ValueError:
        return None


def _verified_absence(
    response: Any,
    provider_id: str | None,
    specification: Mapping[str, Any],
) -> bool:
    if isinstance(response, Mapping):
        status = response.get("status_code", response.get("http_status", response.get("status")))
        if str(status) == "404":
            return True
    if isinstance(response, list):
        rows = response
    elif isinstance(response, Mapping) and isinstance(response.get("results"), list):
        rows = response["results"]
    else:
        rows = None
    if rows is not None:
        target = str(provider_id or "").strip()
        identifiers = {
            str(row.get(name) or "").strip()
            for row in rows
            if isinstance(row, Mapping)
            for name in ("id", "uuid", "resource_id", "issue", "work_item", "member")
            if str(row.get(name) or "").strip()
        }
        return not target or target not in identifiers
    if response is None:
        return False
    if isinstance(response, Mapping) and response.get("id") is not None:
        return False
    return False


def _readback_path(operation: Any, params: Mapping[str, Any], provider_id: str | None) -> str | None:
    """Locate a documented GET representation; never invent a provider route."""
    target = _expand_path(operation.path, params).rstrip("/")
    matches: list[tuple[int, str]] = []
    for candidate in list_operations(limit=500)["actions"]:
        if candidate["method"] != "GET":
            continue
        values = dict(params)
        if provider_id:
            for name in ("resource_id", "issue_id", "work_item_id", "comment_id", "id"):
                values.setdefault(name, provider_id)
        try:
            expanded = _expand_path(candidate["path"], values).rstrip("/")
        except ValueError:
            continue
        if expanded == target or (provider_id and expanded == f"{target}/{quote(provider_id, safe='')}"):
            matches.append((len(expanded), expanded + "/"))
    return max(matches, default=(0, None))[1]


def _response_validation_failure_postcondition(
    operation: Any, *, phase: str
) -> dict[str, Any]:
    """Build a bounded durable state for a confirmed write with schema drift."""
    specification = get_mutation_contract(operation.action)["postcondition"]
    result: dict[str, Any] = {
        "kind": str(specification["kind"]),
        "state": "write_succeeded_readback_validation_failed",
        "verification": "reconciliation_required",
        "readback_performed": phase == "readback",
        "failure_phase": phase,
        "error_code": "response_schema_validation_failed",
        "reconciliation_required": True,
    }
    if phase == "readback" and specification.get("get_action"):
        result["readback_action"] = str(specification["get_action"])
    return result


def _request_rejection_postcondition(
    operation: Any, error: PlaneProviderRequestError
) -> dict[str, Any]:
    specification = get_mutation_contract(operation.action)["postcondition"]
    return {
        "kind": str(specification["kind"]),
        "state": "rejected_not_applied",
        "verification": "provider_rejection",
        "readback_performed": False,
        "failure_phase": error.phase,
        "error_code": "provider_validation_rejected",
        "http_status": error.http_status,
        "error": dict(error.detail),
        "reconciliation_required": False,
    }


def _reconciliation_failure_postcondition(operation: Any, error: Exception) -> dict[str, Any]:
    result = _response_validation_failure_postcondition(operation, phase="readback")
    if operation.action == "issue__add_issue":
        result["readback_action"] = "issue__get_issue_detail"
    if isinstance(error, PlaneProviderRequestError):
        result["error_code"] = "provider_readback_request_failed"
        result["http_status"] = error.http_status
        result["error"] = dict(error.detail)
    elif isinstance(error, PlaneReadbackValidationError):
        result["error_code"] = "readback_identity_validation_failed"
    return result


def _generic_receipt(
    operation: Any,
    path: str,
    provider_id: str | None,
    postcondition: Mapping[str, Any],
    *,
    mutation_applied: bool,
    duplicate: bool,
) -> dict[str, Any]:
    """Return only bounded operation metadata; provider readback never escapes."""
    bounded = {
        str(key): postcondition[key]
        for key in (
            "kind",
            "state",
            "verification",
            "readback_performed",
            "readback_action",
            "response_shape",
            "failure_phase",
            "error_code",
            "reconciliation_required",
            "http_status",
            "error",
        )
        if key in postcondition
    }
    terminal = bounded.get("state") in {
        "verified",
        "verified_absence",
        "provider_acknowledged",
    }
    receipt = {
        "governed": True,
        "action": operation.action,
        "method": operation.method,
        "provider_id": provider_id,
        "mutation_applied": mutation_applied,
        "duplicate": duplicate,
        # Kept as a compatibility field, but it now means the declared
        # postcondition reached a terminal state, not that a payload leaked.
        "readback_verified": terminal,
        "postcondition_verified": terminal,
        "postcondition": bounded,
    }
    _validate_public_receipt(receipt)
    if len(json.dumps(receipt, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 32 * 1024:
        raise RuntimeError("Plane mutation receipt exceeds the 32 KiB public bound")
    return receipt


def _validate_public_receipt(value: Any, *, depth: int = 0) -> None:
    if depth > 5:
        raise RuntimeError("Plane mutation receipt exceeds depth bound")
    if isinstance(value, str):
        if len(value) > 512:
            raise RuntimeError("Plane mutation receipt exceeds string bound")
        return
    if isinstance(value, list):
        if len(value) > 32:
            raise RuntimeError("Plane mutation receipt exceeds list bound")
        for item in value:
            _validate_public_receipt(item, depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > 32:
            raise RuntimeError("Plane mutation receipt exceeds object bound")
        for key, item in value.items():
            _validate_public_receipt(str(key), depth=depth + 1)
            _validate_public_receipt(item, depth=depth + 1)
        return


def _redact_sensitive(value: Any) -> Any:
    names = {"token", "api_key", "authorization", "password", "secret", "x-api-key"}
    if isinstance(value, Mapping):
        return {str(key): "<redacted>" if str(key).casefold() in names else _redact_sensitive(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_sensitive(item) for item in value]
    return value
