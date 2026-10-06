"""Durable native mutation execution; never infer delivery from an opaque error."""
from __future__ import annotations

import hashlib
import random
import time
import uuid
from datetime import datetime, timezone

from . import client as c

class OwnershipLost(RuntimeError):
    """A stale process cannot append evidence to a replacement owner's receipt."""


VERSION = 2
MAX_TRANSPORT_ATTEMPTS = 3
MAX_HISTORY = 32
CORRELATED_CREATES = {"issue_comment__add_issue_comment", "issue__add_issue"}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def recovery_policy(action):
    """Total conservative inventory: default is never generic PATCH/POST replay."""
    operation = c.get_operation(action)
    if action in CORRELATED_CREATES:
        return "exact_correlated_create"
    return "acknowledged_readback_only" if operation.mutation else "bounded_read"


def effect_evidence(row, payload):
    """Separate correlation attribution from desired-state proof without raw values."""
    from html.parser import HTMLParser
    class Text(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
    def text(value):
        parser = Text()
        parser.feed(str(value or ""))
        return " ".join(" ".join(parser.parts).split())
    def identifiers(values):
        return {str(value.get("id")) if isinstance(value, dict) else str(value) for value in values}
    evidence = {}
    for field, value in payload.items():
        observed = row.get(field)
        if field in {"description_html", "comment_html"}:
            evidence[field] = "text_equivalent" if text(observed) == text(value) else "different"
        elif field in {"labels", "assignees"} and isinstance(value, list) and isinstance(observed, list):
            evidence[field] = "verified" if identifiers(value) == identifiers(observed) else "different"
        elif field in c._READBACK_SCALARS or field == "name":
            evidence[field] = "verified" if field in row and observed == value else "different"
        else:
            evidence[field] = "not_evaluated"
    return evidence


def result(operation, receipt, *, duplicate=False):
    postcondition = receipt.get("postcondition")
    if postcondition:
        value = c._generic_receipt(operation, receipt["path"], receipt.get("provider_id"), postcondition,
                                   mutation_applied=bool(receipt.get("mutation_applied")), duplicate=duplicate)
    else:
        value = {"action": operation.action, "mutation_applied": receipt.get("mutation_applied", "unknown"),
                 "readback_verified": False, "duplicate": duplicate}
    value.update(status=receipt["status"], operation_id=receipt["operation_id"],
                 recovery_policy=recovery_policy(operation.action),
                 recovery_evidence=receipt.get("recovery_evidence"),
                 desired_state_evidence=receipt.get("desired_state_evidence"),
                 desired_state_verified=receipt.get("desired_state_verified", False),
                 replayed_write=False, attempt_history=receipt.get("attempt_history", []),
                 next_action=("resume_same_key" if receipt["status"] == "not_sent_retryable" else
                              "read_only_recovery" if receipt["status"] in {"outcome_unknown", "recovery_required"} else
                              "wait_for_owner" if receipt["status"] == "in_progress" else "inspect_receipt"))
    return value


def read(client, path, *, query=None):
    """Ordinary GET retry budget; no ledger or external write is introduced."""
    deadline = time.monotonic() + 2.0
    for attempt in range(MAX_TRANSPORT_ATTEMPTS):
        try:
            return client._request("GET", path, query=query)
        except c.PlaneProviderRequestError as exc:
            retryable = exc.transport_class == "network" or exc.http_status in {429, 502, 503, 504}
            delay = max(.1 * (2 ** attempt) + random.uniform(0, .05), exc.detail.get("retry_after_seconds", 0))
            if not retryable or attempt + 1 == MAX_TRANSPORT_ATTEMPTS or delay > deadline - time.monotonic():
                raise
            time.sleep(delay)


def legacy_successor(ledger, key, legacy, fingerprint, provider, body):
    """Select only an explicitly correlated successor bound to its historical receipt."""
    if (legacy.get("status") != "attempt_started"
            or legacy.get("key_hash") != key
            or legacy.get("fingerprint") != fingerprint
            or legacy.get("provider_instance_hash") != provider
            or not body.get("external_id") or not body.get("external_source")):
        raise PermissionError("legacy recovery requires original explicit correlation and request binding")
    successor = ledger.get("native_recoveries", {}).get(key)
    if successor and (successor.get("executor_version") != VERSION
            or successor.get("fingerprint") != fingerprint
            or successor.get("provider_instance_hash") != provider
            or successor.get("key_hash") != key
            or successor.get("legacy_original_key_hash") != key):
        raise PermissionError("legacy successor binding mismatch")
    return successor


def authorize_reattempt(client, action, *, path_params, payload, idempotency_key, ledger_path,
                        operator_acknowledged_duplicate_risk, reason, query=None):
    """Explicit local authorization, not absence proof and never a provider write."""
    if operator_acknowledged_duplicate_risk is not True:
        raise PermissionError("explicit duplicate-risk acknowledgment is required")
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 200:
        raise ValueError("reason must contain 8 to 200 characters")
    if action not in CORRELATED_CREATES:
        raise PermissionError("operator reattempt is limited to correlated creates")
    params, body = dict(path_params or {}), dict(payload or {})
    preflight = client.preflight_native_mutation(action, path_params=params, payload=body,
                                               attempts=1, idempotency_key=idempotency_key)
    fingerprint = c._fingerprint(action, "", "", None,
        {"path_params": params, "query": dict(query or {}), "payload": body})
    with c._locked_ledger(c._require_ledger_path(ledger_path)) as ledger:
        prior = ledger["receipts"].get(preflight["key_hash"])
        if prior and prior.get("executor_version") != VERSION:
            prior = legacy_successor(ledger, preflight["key_hash"], prior, fingerprint,
                                     c._provider_instance_hash(client.config), body)
        if not prior or prior.get("executor_version") != VERSION:
            raise PermissionError("operator reattempt requires a bound native version-2 receipt")
        if prior.get("provider_instance_hash") != c._provider_instance_hash(client.config) or prior.get("fingerprint") != fingerprint:
            raise PermissionError("operator reattempt target/provider/request binding mismatch")
        if prior.get("lease_until", 0) > time.time():
            raise PermissionError("active owner lease blocks operator reattempt")
        evidence = prior.get("recovery_evidence") or {}
        if (prior.get("status") != "outcome_unknown" or prior.get("mutation_applied") is True
                or evidence.get("complete") is not True or evidence.get("match_count") != 0
                or evidence.get("failure_code") is not None
                or time.time() - evidence.get("observed_at_epoch", 0) > 300):
            raise PermissionError("fresh complete absent correlation scan is required; absence is not no-write proof")
        authorizations = prior.setdefault("reattempt_authorizations", [])
        if len(authorizations) >= 8 or any(not item.get("consumed_at") for item in authorizations):
            raise PermissionError("pending authorization or reattempt budget exhausted")
        authorization = {"id": uuid.uuid4().hex, "timestamp": timestamp(), "duplicate_risk_acknowledged": True,
                         "reason_sha256": hashlib.sha256(reason.encode()).hexdigest(),
                         "linked_operation_id": prior["operation_id"], "consumed_at": None,
                         "absence_evidence": dict(evidence)}
        authorizations.append(authorization)
        prior.setdefault("uncertain_attempts", []).append({"status": prior["status"],
            "attempt_history": list(prior.get("attempt_history", [])),
            "uncertain_write_attempts": list(prior.get("uncertain_write_attempts", [])),
            "timestamp": timestamp(),
            "authorization_id": authorization["id"]})
        prior.update(status="authorized_retry", updated_at=timestamp())
        return {"status": "reattempt_authorized", "operation_id": prior["operation_id"],
                "authorization_id": authorization["id"], "provider_write_performed": False,
                "next_action": "execute_same_key_once"}


def execute(client, action, *, path_params=None, query=None, payload=None, idempotency_key="", attempts=1,
            ledger_path, recover=False):
    operation = c.get_operation(action)
    params, original = dict(path_params or {}), dict(payload or {})
    preflight = client.preflight_native_mutation(action, path_params=params, payload=original,
                                                attempts=attempts, idempotency_key=idempotency_key)
    key = preflight["key_hash"]
    fingerprint = c._fingerprint(action, "", "", None,
                                 {"path_params": params, "query": dict(query or {}), "payload": original})
    provider = c._provider_instance_hash(client.config)
    operation_id = hashlib.sha256(f"{provider}:{key}:{fingerprint}".encode()).hexdigest()
    body = dict(original)
    if action in CORRELATED_CREATES and not body.get("external_id"):
        body["external_id"] = "plane-mcp:" + operation_id
        if not body.get("external_source"):
            body["external_source"] = "plane-mcp-karval"
    store = c._require_ledger_path(ledger_path)
    owner = uuid.uuid4().hex
    receipt_section = "receipts"
    with c._locked_ledger(store) as ledger:
        conflicts = ledger.get("migration_conflicts", {}).get("receipts", {})
        if key in conflicts:
            raise RuntimeError("idempotency_key collides across legacy mutation ledgers; manual reconciliation is required")
        prior = ledger["receipts"].get(key)
        if prior:
            if prior.get("provider_instance_hash") != provider:
                raise PermissionError("idempotency_key belongs to a different Plane provider instance")
            if prior.get("fingerprint") != fingerprint:
                raise ValueError("idempotency_key was already used for a different mutation")
            if prior.get("executor_version") != VERSION:
                if not (prior.get("status") == "attempt_started" and action in CORRELATED_CREATES
                        and original.get("external_id") and original.get("external_source")):
                    return None  # Historical UNKNOWN is never reclassified as unsent.
                successor = legacy_successor(ledger, key, prior, fingerprint, provider, original)
                if not successor and not recover:
                    return None
                receipt_section = "native_recoveries"
                ledger.setdefault(receipt_section, {})
                prior = successor or {
                    **prior, "executor_version": VERSION, "operation_id": operation_id,
                    "status": "recovery_required", "attempt_history": [], "lease_until": 0,
                    "key_hash": key, "legacy_original_key_hash": key,
                    "path": preflight["path"], "legacy_read_only_recovery": True}
            receipt = dict(prior)
            if prior.get("lease_until", 0) > time.time():
                return result(operation, {**receipt, "status": "in_progress"}, duplicate=True)
            if prior["status"] == "verified":
                return result(operation, receipt, duplicate=True)
            if prior["status"] == "rejected_not_applied":
                return result(operation, receipt, duplicate=True)
            if prior["status"] in {"reserved", "sending"}:
                if prior["status"] == "sending":
                    boundary = dict(prior.get("durable_send_attempt") or {
                        "send_attempt_id": hashlib.sha256(
                            f"{key}:{prior.get('fence')}:{prior.get('updated_at')}".encode()).hexdigest(),
                        "timestamp": prior.get("updated_at") or prior.get("created_at"),
                        "phase": f"write:{action}",
                        "active_authorization_id": prior.get("active_authorization_id"),
                        "boundary": "sending_receipt_without_completion"})
                    boundary.update(delivery="uncertain", observation="expired_sending_lease",
                                    observed_at=timestamp())
                    writes = list(prior.get("uncertain_write_attempts", []))
                    if not any(item.get("send_attempt_id") == boundary["send_attempt_id"] for item in writes):
                        writes.append(boundary)
                    receipt["uncertain_write_attempts"] = writes
                else:
                    receipt["reserved_crash_boundary"] = {
                        "timestamp": prior.get("updated_at") or prior.get("created_at"),
                        "boundary": "reserved_before_durable_send", "observed_at": timestamp()}
                receipt["status"] = "recovery_required"
            if prior["status"] == "authorized_retry":
                if recover:
                    return result(operation, receipt, duplicate=True)
                pending = [entry for entry in receipt.get("reattempt_authorizations", []) if not entry.get("consumed_at")]
                if len(pending) != 1:
                    raise PermissionError("authorized retry has no unique unused authorization")
                pending[0]["consumed_at"] = timestamp()
                receipt["active_authorization_id"] = pending[0]["id"]
                receipt["status"] = "reserved"
                # New explicitly authorized attempt, never a reinterpretation of old delivery.
                receipt["recovery_evidence"] = None
            if recover and receipt["status"] == "not_sent_retryable":
                return result(operation, receipt, duplicate=True)
        else:
            if recover:
                raise ValueError("idempotency_key has no local mutation receipt")
            receipt = {"executor_version": VERSION, "action": action, "method": operation.method,
                       "path": preflight["path"], "fingerprint": fingerprint, "key_hash": key,
                       "provider_instance_hash": provider, "operation_id": operation_id,
                       "status": "reserved", "attempt_history": [], "created_at": timestamp()}
        receipt.update(owner=owner, fence=int(receipt.get("fence", 0)) + 1,
                       lease_until=time.time() + max(60, client.config.timeout * 8), updated_at=timestamp())
        ledger[receipt_section][key] = receipt
    fence = receipt["fence"]

    def save(**updates):
        with c._locked_ledger(store) as ledger:
            current = ledger[receipt_section][key]
            if current.get("owner") != owner or current.get("fence") != fence:
                raise OwnershipLost("mutation owner fenced by recovery")
            if current.get("lease_until", 0) <= time.time():
                raise OwnershipLost("mutation lease expired; read-only recovery required")
            current.update(updates, updated_at=timestamp())
            receipt.update(current)

    def call(method, path, **kwargs):
        deadline = time.monotonic() + 2.0
        for number in range(MAX_TRANSPORT_ATTEMPTS):
            with c._locked_ledger(store) as ledger:
                cooldown = ledger.get("provider_cooldowns", {}).get(provider, 0)
            wait = max(0.0, cooldown - time.time())
            if wait > max(0.0, deadline - time.monotonic()):
                raise c.PlaneProviderRequestError(phase=kwargs.get("phase", "request"), http_status=None,
                    transport_class="backoff", deterministic=False,
                    detail={"delivery": "not_sent", "retry_after_seconds": min(60.0, wait)})
            if wait:
                time.sleep(wait)
            try:
                response = client._request(method, path, **kwargs)
            except c.PlaneProviderRequestError as exc:
                not_sent = exc.detail.get("delivery") == "not_sent"
                history = (receipt.get("attempt_history", []) + [{"timestamp": timestamp(), "phase": kwargs.get("phase", "recovery"),
                           "transport_class": exc.transport_class, "http_status": exc.http_status,
                           "errno": exc.detail.get("errno"), "delivery": "not_sent" if not_sent else "uncertain"}])[-MAX_HISTORY:]
                save(attempt_history=history)
                delay = max(min(.1 * (2 ** number) + random.uniform(0, .05), .5),
                            exc.detail.get("retry_after_seconds", 0))
                if exc.http_status == 429:
                    with c._locked_ledger(store) as ledger:
                        cooldowns = ledger.setdefault("provider_cooldowns", {})
                        cooldowns[provider] = max(cooldowns.get(provider, 0), time.time() + delay)
                retry = not_sent or (method == "GET" and (exc.http_status in {429, 502, 503, 504} or exc.transport_class == "network"))
                if retry and number + 1 < MAX_TRANSPORT_ATTEMPTS and delay <= deadline - time.monotonic():
                    time.sleep(delay)
                    continue
                raise
            save(attempt_history=(receipt.get("attempt_history", []) + [{"timestamp": timestamp(),
                 "phase": kwargs.get("phase", "recovery"), "http_status": c._response_status(response), "delivery": "acknowledged"}])[-MAX_HISTORY:])
            return response

    response = None
    try:
        if receipt["status"] in {"outcome_unknown", "recovery_required", "correlation_conflict"}:
            if action not in CORRELATED_CREATES:
                save(status="recovery_required", lease_until=0)
                return result(operation, receipt, duplicate=True)
            matches = {}
            cursor, seen, rows_count = None, set(), 0
            row_ids, declared_totals = set(), {}
            declared_pages = None
            evidence = {"complete": False, "pages": 0, "rows": 0, "match_count": 0, "failure_code": None}
            save(recovery_evidence=evidence)
            path = preflight["path"]
            for _ in range(c._MAX_READBACK_PAGES):
                response = call("GET", path, query={"cursor": cursor} if cursor else {}, with_metadata=True, phase="recovery:correlation")
                page = c._response_body(response)
                if not isinstance(page, dict):
                    raise c.PlaneReadbackValidationError("correlation requires cursor envelope")
                c._validate_module_reconciliation_collection(page, context="correlation")
                rows = page["results"]
                if type(page.get("next_page_results")) is not bool:
                    raise c.PlaneReadbackValidationError("missing typed terminal pagination evidence")
                more, next_cursor = page["next_page_results"], page.get("next_cursor")
                if more and (not next_cursor or next_cursor in seen):
                    raise c.PlaneReadbackValidationError("contradictory correlation pagination")
                # Plane emits a computed next_cursor even when its authoritative
                # next_page_results is false. Accept it only with complete global
                # count/page evidence, validated below before publishing completeness.
                if not more and next_cursor and not all(
                        field in page for field in ("total_results", "total_count", "total_pages")):
                    raise c.PlaneReadbackValidationError("unproven terminal cursor")
                for field in ("count", "total_results", "total_count", "total_pages"):
                    if field in page and (type(page[field]) is not int or page[field] < 0):
                        raise c.PlaneReadbackValidationError("invalid correlation count")
                if "count" in page and page["count"] != len(rows):
                    raise c.PlaneReadbackValidationError("correlation page count mismatch")
                for field in ("total_results", "total_count"):
                    if field in page:
                        value = page[field]
                        if declared_totals and value != next(iter(declared_totals.values())):
                            raise c.PlaneReadbackValidationError("inconsistent correlation global totals")
                        declared_totals[field] = value
                if "total_pages" in page:
                    if declared_pages is not None and declared_pages != page["total_pages"]:
                        raise c.PlaneReadbackValidationError("inconsistent correlation total pages")
                    declared_pages = page["total_pages"]
                page_number = evidence["pages"] + 1
                if declared_pages is not None and (declared_pages < page_number
                        or (not more and declared_pages != page_number)
                        or (more and declared_pages <= page_number)):
                    raise c.PlaneReadbackValidationError("correlation total pages mismatch")
                rows_count += len(rows)
                if rows_count > c._MAX_READBACK_ROWS:
                    raise c.PlaneReadbackValidationError("correlation row budget exceeded")
                for row in rows:
                    row_id = str(row["id"])
                    if row_id in row_ids:
                        raise c.PlaneReadbackValidationError("duplicate correlation row identity")
                    row_ids.add(row_id)
                    if (row.get("external_id") == body.get("external_id")
                            and (not body.get("external_source") or row.get("external_source") == body["external_source"])):
                        if not row.get("id"):
                            raise c.PlaneReadbackValidationError("correlation identity missing")
                        # Workspace is scoped by the endpoint: row.workspace is a UUID, not a slug.
                        for field, param in (("project", "project_id"), ("project_id", "project_id"),
                                             ("issue", "work_item_id"), ("issue_id", "work_item_id"),
                                             ("work_item", "work_item_id"), ("work_item_id", "work_item_id")):
                            if field in row and str(row[field]) != str(params.get(param)):
                                raise c.PlaneReadbackValidationError("correlation target mismatch")
                        matches[str(row["id"])] = row
                evidence.update(pages=evidence["pages"] + 1, rows=rows_count, match_count=len(matches))
                save(recovery_evidence=dict(evidence))
                if more:
                    seen.add(next_cursor)
                    cursor = next_cursor
                    continue
                break
            else:
                raise c.PlaneReadbackValidationError("correlation page budget exceeded")
            if any(total != rows_count for total in declared_totals.values()):
                raise c.PlaneReadbackValidationError("correlation total does not match collected rows")
            evidence.update(complete=True, match_count=len(matches), observed_at_epoch=time.time())
            save(recovery_evidence=dict(evidence))
            if len(matches) != 1:
                save(status="correlation_conflict" if matches else "outcome_unknown", lease_until=0)
                return result(operation, receipt, duplicate=True)
            matched = next(iter(matches.values()))
            effects = effect_evidence(matched, original)
            save(desired_state_evidence=effects,
                 desired_state_verified=all(state == "verified" for state in effects.values()))
            save(provider_id=next(iter(matches)), mutation_applied=True, status="verified",
                 postcondition={"kind": "correlated_create", "state": "verified",
                                "verification": "complete_exact_external_id_lookup", "readback_performed": True},
                 lease_until=0)
            return result(operation, receipt, duplicate=True)
        elif receipt["status"] in {"reserved", "not_sent_retryable"} and not recover:
            save(status="sending", durable_send_attempt={
                "send_attempt_id": uuid.uuid4().hex, "timestamp": timestamp(),
                "phase": f"write:{action}", "boundary": "prepared_for_transport",
                "active_authorization_id": receipt.get("active_authorization_id")})
            try:
                response = call(operation.method, preflight["path"], query=query,
                                payload=body if c._request_body_available(operation) else None,
                                with_metadata=True, phase=f"write:{action}")
            except c.PlaneProviderRequestError as exc:
                if exc.detail.get("delivery") == "not_sent":
                    save(status="not_sent_retryable", mutation_applied=False, lease_until=0)
                elif exc.deterministic:
                    save(status="rejected_not_applied", mutation_applied=False,
                         postcondition=c._request_rejection_postcondition(operation, exc), lease_until=0)
                else:
                    write_evidence = {**receipt["attempt_history"][-1],
                                      "send_attempt_id": receipt["durable_send_attempt"]["send_attempt_id"],
                                      "durable_send_boundary": dict(receipt["durable_send_attempt"]),
                                      "active_authorization_id": receipt.get("active_authorization_id")}
                    save(status="outcome_unknown", lease_until=0,
                         uncertain_write_attempts=receipt.get("uncertain_write_attempts", []) + [write_evidence])
                return result(operation, receipt)
            # Persist ACK before validation/readback. Never resend even an invalid ACK.
            save(status="applied_pending_readback", mutation_applied=True, ack_validated=False,
                 provider_id=c._provider_id(response),
                 response_metadata=c._response_metadata(response, operation.response_schema))
            try:
                spec = c.get_mutation_contract(action)["postcondition"]
                c._validate_response_status(response, spec.get("expected_status"), operation.response_schema, context="mutation")
                if action == "module__update_module_detail":
                    c._validate_module_update_ack(c._response_body(response), body)
                else:
                    c._validate_mutation_write_response(operation, response, body)
                if spec.get("strategy") == "provider_ack" and action != "module__update_module_detail":
                    c._validate_provider_acknowledgement(response, operation.response_schema, spec)
            except c.PlaneResponseValidationError:
                save(status="write_succeeded_readback_validation_failed",
                     postcondition=c._response_validation_failure_postcondition(operation, phase="write_response"), lease_until=0)
                return result(operation, receipt)
            metadata = dict(receipt["response_metadata"])
            if action != "module__update_module_detail":
                metadata["body_shape"] = operation.response_schema.get("shape", metadata.get("body_shape"))
            save(ack_validated=True, response_metadata=metadata)
        if not receipt.get("ack_validated"):
            save(status="write_succeeded_readback_validation_failed", lease_until=0)
            return result(operation, receipt, duplicate=True)
        import copy
        read_client = copy.copy(client)
        read_client._request = call
        postcondition = read_client._evaluate_generic_postcondition(operation, params, receipt.get("provider_id"), query,
                        response=response if receipt["status"] == "applied_pending_readback" and not recover else None,
                        response_metadata=receipt.get("response_metadata"), payload=original)
        save(status="verified", postcondition=postcondition, lease_until=0)
    except OwnershipLost:
        return result(operation, {**receipt, "status": "recovery_required"}, duplicate=True)
    except (c.PlaneProviderRequestError, RuntimeError, ValueError) as exc:
        if receipt.get("recovery_evidence"):
            evidence = dict(receipt["recovery_evidence"])
            evidence["failure_code"] = c._reconciliation_error_code(exc)
            save(recovery_evidence=evidence)
        save(status="write_succeeded_readback_validation_failed" if receipt.get("mutation_applied") else "outcome_unknown",
             recovery_error={"class": type(exc).__name__, "code": c._reconciliation_error_code(exc)}, lease_until=0)
    return result(operation, receipt, duplicate=recover)
