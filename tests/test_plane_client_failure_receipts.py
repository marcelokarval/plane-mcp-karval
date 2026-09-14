from __future__ import annotations

import json
import hashlib
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

from plane_api.client import (
    PlaneClient,
    PlaneResponseValidationError,
    _provider_compatible_response_schema,
    _provider_instance_hash,
    _request_body_available,
    payload_fingerprint,
)
from plane_api.registry import get_operation
from plane_api.manifest import CONTRACT_HASH, CONTRACT_VERSION


ACTION = "issue__add_issue"
PARAMS = {"workspace_slug": "workspace", "project_id": "project"}
PAYLOAD = {"name": "HERMES-205 fixture"}
ISSUE = {
    "assignees": [], "created_at": "2026-08-24T00:00:00Z", "description": "",
    "id": "issue-1", "labels": [], "name": "HERMES-205 fixture", "priority": "none",
    "sequence_id": 1, "updated_at": "2026-08-24T00:00:00Z",
}


class QueueTransport:
    __hermes_test_fake_transport__ = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, request, timeout):
        self.calls.append((request.get_method(), request.full_url, dict(request.header_items()), request.data))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeResponse:
    def __init__(self, status: int, body: dict):
        self.status = status
        self.body = body

    def getcode(self):
        return self.status

    def read(self):
        return json.dumps(self.body).encode("utf-8")


class RequestQueueTransport:
    __hermes_test_fake_transport__ = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **_kwargs):
        self.calls.append((method, url))
        return self.responses.pop(0)


class CapturingRequestTransport(RequestQueueTransport):
    def __init__(self, responses):
        super().__init__(responses)
        self.request_kwargs = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        self.request_kwargs.append(kwargs)
        return self.responses.pop(0)


def http_error(status: int, body: bytes) -> HTTPError:
    return HTTPError("https://provider.invalid/?token=must-not-leak", status, "failure", {}, BytesIO(body))


_approval_counter = 0


def invoke(client: PlaneClient, ledger: Path, key: str = "hermes-205-idempotency-key"):
    global _approval_counter
    _approval_counter += 1
    return client.execute_mutation_action(
        ACTION,
        path_params=PARAMS,
        payload=PAYLOAD,
        approved_live_mutation=True,
        authorization_receipt={
            "action": ACTION,
            "method": "POST",
            "approved_live_mutation": True,
            "authorization_scope": "one_operation_one_target",
            "path_params": PARAMS,
            "payload_fingerprint": payload_fingerprint(PAYLOAD),
            "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "authorization_id": f"approval-{_approval_counter:08d}",
            "provider_instance_hash": _provider_instance_hash(client.config),
            **PARAMS,
        },
        attempts=1,
        idempotency_key=key,
        ledger_path=ledger,
    )


def archive_authorization(params: dict[str, str]) -> dict[str, object]:
    return {
        "action": "module__archive_module",
        "method": "POST",
        "approved_live_mutation": True,
        "authorization_scope": "one_operation_one_target",
        "path_params": params,
        "payload_fingerprint": payload_fingerprint({}),
        "key_hash": hashlib.sha256(b"hermes-211-archive-no-body").hexdigest(),
        "authorization_id": "archive-approval-0001",
        "provider_instance_hash": _provider_instance_hash(PlaneClient({"base_url": "https://plane.invalid"}).config),
        "workspace_slug": params["workspace_slug"],
        "project_id": params["project_id"],
    }


def test_archive_module_uses_no_request_body_when_the_registry_marks_body_unavailable(tmp_path: Path, monkeypatch) -> None:
    """Regression: the documented 204 archive endpoint must not receive ``{}``."""
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    transport = CapturingRequestTransport([{"status_code": 204, "body": None}])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    monkeypatch.setattr(client, "_evaluate_generic_postcondition", lambda *args, **kwargs: {"state": "verified"})

    receipt = client.execute_mutation_action(
        "module__archive_module",
        path_params=params,
        payload={},
        approved_live_mutation=True,
        authorization_receipt=archive_authorization(params),
        attempts=1,
        idempotency_key="hermes-211-archive-no-body",
        ledger_path=tmp_path / "ledger.json",
    )

    assert receipt["mutation_applied"] is True
    assert transport.request_kwargs[0]["json_body"] is None


def test_comment_write_accepts_provider_minimal_identity_before_readback(tmp_path: Path, monkeypatch) -> None:
    params = {"workspace_slug": "workspace", "project_id": "project", "work_item_id": "issue-1"}
    transport = RequestQueueTransport([{"status_code": 201, "body": {"id": "comment-1"}}])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    monkeypatch.setattr(client, "_evaluate_generic_postcondition", lambda *args, **kwargs: {"state": "verified"})

    receipt = client.execute_native_mutation_action(
        "issue_comment__add_issue_comment",
        path_params=params,
        payload={"comment_html": "<p>minimal provider response</p>", "external_source": "plane-mcp-karval"},
        idempotency_key="comment-minimal-identity-key",
        ledger_path=tmp_path / "ledger.json",
    )

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is True
    assert transport.calls == [("POST", "https://plane.invalid/api/v1/workspaces/workspace/projects/project/work-items/issue-1/comments/")]


@pytest.mark.parametrize(
    "action",
    [
        "members__remove_workspace_member",
        "project__archive_project",
        "cycle__archive_cycle",
        "module__archive_module",
    ],
)
def test_every_documented_bodyless_post_is_classified_as_having_no_wire_body(action: str) -> None:
    assert get_operation(action).method == "POST"
    assert _request_body_available(get_operation(action)) is False


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("members__remove_workspace_member", {"workspace_slug": "workspace", "resource_id": "member-1"}),
        ("project__archive_project", {"workspace_slug": "workspace", "project_id": "project-1"}),
        ("cycle__archive_cycle", {"workspace_slug": "workspace", "project_id": "project", "cycle_id": "cycle-1"}),
        ("module__archive_module", {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}),
    ],
)
def test_all_bodyless_posts_omit_body_and_content_type_with_stdlib_transport(
    tmp_path: Path, action: str, params: dict[str, str], monkeypatch
) -> None:
    key = f"hermes-211-bodyless-{action}"
    transport = QueueTransport([FakeResponse(204, {})])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    monkeypatch.setattr(client, "_evaluate_generic_postcondition", lambda *args, **kwargs: {"state": "verified"})
    client.execute_mutation_action(
        action, path_params=params, payload={}, approved_live_mutation=True,
        authorization_receipt={
            "action": action, "method": "POST", "approved_live_mutation": True,
            "authorization_scope": "one_operation_one_target", "path_params": params,
            "payload_fingerprint": payload_fingerprint({}), "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "authorization_id": f"approval-{action}-0001", **{name: value for name, value in params.items() if name in {"workspace_slug", "project_id"}},
            "provider_instance_hash": _provider_instance_hash(client.config),
        }, attempts=1, idempotency_key=key, ledger_path=tmp_path / f"{action}.json",
    )
    _, _, headers, body = transport.calls[0]
    assert body is None
    assert "Content-type" not in headers


def test_legacy_archive_attempt_can_be_reconciled_only_after_provider_proves_not_applied(tmp_path: Path) -> None:
    """The capability did not exist before HERMES-211; this is the RED contract."""
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    key = "hermes-211-legacy-attempt"
    key_hash = __import__("hashlib").sha256(key.encode()).hexdigest()
    fingerprint = __import__("plane_api.client", fromlist=["_fingerprint"])._fingerprint(
        "module__archive_module", "", "", None,
        {"path_params": params, "query": {}, "payload": {}},
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"receipts": {key_hash: {
        "action": "module__archive_module", "method": "POST",
        "path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/",
        "fingerprint": fingerprint, "status": "attempt_started",
    }}}))
    archived_empty = {
        "count": 0, "extra_stats": None, "grouped_by": None, "next_cursor": "",
        "next_page_results": False, "prev_cursor": "", "prev_page_results": False,
        "results": [], "sub_grouped_by": None, "total_count": 0,
        "total_pages": 0, "total_results": 0,
    }
    active_module = {**archived_empty, "count": 1, "total_count": 1, "total_results": 1,
        "results": [{"id": "module-1", "name": "active module", "created_at": "2026-08-25T00:00:00Z",
                     "description": "", "start_date": "2026-08-25", "status": "planned", "target_date": "2026-08-26"}]}
    client = PlaneClient(
        {"base_url": "https://plane.invalid"},
        RequestQueueTransport([
            {"status_code": 200, "body": archived_empty},
            {"status_code": 200, "body": active_module},
        ]),
    )

    receipt = client.reconcile_legacy_module_archive_attempt(  # type: ignore[attr-defined]
        workspace_slug="workspace", project_id="project", resource_id="module-1",
        key_hash=key_hash, attempts=1, approved_live_reconciliation=True,
        authorization_receipt={
            "action": "module__archive_module", "method": "POST",
            "approved_live_reconciliation": True,
            "authorization_scope": "one_legacy_attempt_one_target",
            "path_params": params, "key_hash": key_hash,
            "authorization_id": "reconcile-approval-0001",
            "provider_instance_hash": _provider_instance_hash(client.config),
            "legacy_path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/",
            "legacy_method": "POST", "legacy_fingerprint": fingerprint,
            "legacy_payload_fingerprint": payload_fingerprint({}), "historical_wire_body_present": True,
            "contract_version": CONTRACT_VERSION, "contract_hash": CONTRACT_HASH,
            "operator_disposition": "replacement_authorized_after_effect_absence",
        }, ledger_path=ledger,
    )
    assert receipt["disposition"] == "effect_not_present_at_reconciliation_time"
    persisted = json.loads(ledger.read_text())
    assert persisted["receipts"][key_hash]["status"] == "attempt_started"
    event = persisted["reconciliation_events"][0]
    assert event["bindings"]["key_hash"] == key_hash
    assert event["evidence_version"] == 1
    assert event["evidence"]["archived_list_pages"] == 1


@pytest.mark.parametrize(
    ("receipt_change", "error"),
    [
        ({"status": "verified"}, "legacy receipt does not exactly match"),
        ({"fingerprint": "wrong"}, "legacy receipt does not exactly match"),
        ({"path": "/wrong"}, "legacy receipt does not exactly match"),
    ],
)
def test_reconciliation_rejects_non_legacy_or_incompletely_bound_receipts(
    tmp_path: Path, receipt_change: dict[str, object], error: str
) -> None:
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    key = "hermes-211-negative-attempt"
    key_hash = __import__("hashlib").sha256(key.encode()).hexdigest()
    fingerprint = __import__("plane_api.client", fromlist=["_fingerprint"])._fingerprint(
        "module__archive_module", "", "", None,
        {"path_params": params, "query": {}, "payload": {}},
    )
    stored = {
        "action": "module__archive_module", "method": "POST", "status": "attempt_started",
        "path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/",
        "fingerprint": fingerprint,
    }
    stored.update(receipt_change)
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"receipts": {key_hash: stored}}))
    authorization = {
        "action": "module__archive_module", "method": "POST",
        "approved_live_reconciliation": True,
        "authorization_scope": "one_legacy_attempt_one_target", "path_params": params,
        "key_hash": key_hash,
        "authorization_id": "reconcile-approval-0002",
        "provider_instance_hash": _provider_instance_hash(PlaneClient({"base_url": "https://plane.invalid"}).config),
        "legacy_path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/",
        "legacy_method": "POST", "legacy_fingerprint": fingerprint,
        "legacy_payload_fingerprint": payload_fingerprint({}), "historical_wire_body_present": True,
        "contract_version": CONTRACT_VERSION, "contract_hash": CONTRACT_HASH,
        "operator_disposition": "replacement_authorized_after_effect_absence",
    }

    with pytest.raises(PermissionError, match=error):
        PlaneClient({"base_url": "https://plane.invalid"}).reconcile_legacy_module_archive_attempt(
            **params, key_hash=key_hash, attempts=1, approved_live_reconciliation=True,
            authorization_receipt=authorization, ledger_path=ledger,
        )


def test_reconciliation_rejects_non_exhaustive_archived_evidence() -> None:
    incomplete = {
        "count": 1, "extra_stats": None, "grouped_by": None, "next_cursor": "",
        "next_page_results": True, "prev_cursor": "", "prev_page_results": False,
        "results": [], "sub_grouped_by": None, "total_count": 1,
        "total_pages": 2, "total_results": 1,
    }
    client = PlaneClient(
        {"base_url": "https://plane.invalid"},
        RequestQueueTransport([{"status_code": 200, "body": incomplete}]),
    )

    with pytest.raises(RuntimeError, match="not exhaustive"):
        client._prove_module_archive_not_applied({
            "workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1",
        })


def test_post_400_is_a_sanitized_terminal_rejection(tmp_path: Path) -> None:
    transport = QueueTransport([
        http_error(400, b'{"code":"invalid","field":"name","message":"bad","token":"secret"}')
    ])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid", "token": "api-secret"}, transport), tmp_path / "ledger.json")

    assert receipt["mutation_applied"] is False
    assert receipt["readback_verified"] is False
    assert receipt["postcondition"]["state"] == "rejected_not_applied"
    assert receipt["postcondition"]["error"] == {
        "code": "invalid", "field": "name", "message": "bad"
    }
    assert len(transport.calls) == 1


def test_post_400_preserves_safe_plane_validation_field_names(tmp_path: Path) -> None:
    transport = QueueTransport([
        http_error(
            400,
            b'{"priority":["Choose a valid priority"],"api_key":"must-not-leak"}',
        )
    ])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["postcondition"]["error"] == {
        "errors": {"priority": ["Choose a valid priority"]}
    }
    assert "must-not-leak" not in json.dumps(receipt)
    persisted = json.loads((tmp_path / "ledger.json").read_text())
    assert persisted["receipts"]
    assert "secret" not in json.dumps({"receipt": receipt, "ledger": persisted})
    duplicate = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")
    assert duplicate["duplicate"] is True
    assert len(transport.calls) == 1


def test_post_400_malformed_and_nested_sensitive_values_are_bounded(tmp_path: Path) -> None:
    transport = QueueTransport([
        http_error(400, b"<html>token=must-not-leak</html>"),
    ])
    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["postcondition"]["error"] == {"message": "provider rejected request"}
    assert "must-not-leak" not in json.dumps(receipt)
    assert len(json.dumps(receipt).encode()) < 32 * 1024

    nested = QueueTransport([
        http_error(400, b'{"errors":[{"message":"bad","PASSWORD":"must-not-leak"}]}')
    ])
    nested_receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, nested), tmp_path / "nested.json")
    assert nested_receipt["postcondition"]["error"] == {"errors": [{"message": "bad"}]}
    assert "must-not-leak" not in json.dumps(nested_receipt)


def test_request_transport_400_is_rejected_without_claiming_a_write(tmp_path: Path) -> None:
    transport = RequestQueueTransport([
        {"status_code": 400, "body": {"message": "invalid", "authorization": "must-not-leak"}}
    ])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["mutation_applied"] is False
    assert receipt["postcondition"]["state"] == "rejected_not_applied"
    assert "must-not-leak" not in json.dumps(receipt)


def test_request_transport_raw_html_400_collapses_to_generic_marker(tmp_path: Path) -> None:
    transport = RequestQueueTransport([
        {"status_code": 400, "body": "<html>sentinel-must-not-leak</html>"}
    ])
    ledger = tmp_path / "ledger.json"

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), ledger)

    persisted = json.loads(ledger.read_text())
    assert receipt["postcondition"]["error"] == {"message": "provider rejected request"}
    assert "sentinel-must-not-leak" not in json.dumps({"receipt": receipt, "ledger": persisted})


@pytest.mark.parametrize("failure", [
    http_error(429, b'{"message":"slow down"}'),
    http_error(500, b'{"message":"provider failed"}'),
    URLError("network unavailable"),
])
def test_ambiguous_write_failure_is_persisted_and_never_replayed(tmp_path: Path, failure: BaseException) -> None:
    transport = QueueTransport([failure])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    ledger = tmp_path / "ledger.json"

    with pytest.raises(RuntimeError):
        invoke(client, ledger)
    with pytest.raises(RuntimeError):
        invoke(client, ledger)

    assert len(transport.calls) == 1


def test_issue_create_uses_registry_detail_readback(tmp_path: Path) -> None:
    transport = QueueTransport([
        FakeResponse(201, ISSUE),
        FakeResponse(200, ISSUE),
    ])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is True
    assert receipt["postcondition"]["readback_action"] == "issue__get_issue_detail"
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1][1].endswith("/work-items/issue-1/")


@pytest.mark.parametrize("readback_failure", [
    http_error(500, b'{"message":"unavailable"}'),
    URLError("network unavailable"),
])
def test_accepted_write_with_readback_transport_failure_requires_reconciliation(
    tmp_path: Path, readback_failure: BaseException
) -> None:
    transport = QueueTransport([FakeResponse(201, ISSUE), readback_failure])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    ledger = tmp_path / "ledger.json"

    receipt = invoke(client, ledger)
    duplicate = invoke(client, ledger)

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is False
    assert receipt["postcondition"]["reconciliation_required"] is True
    assert duplicate["duplicate"] is True
    assert duplicate["mutation_applied"] is True
    assert len(transport.calls) == 2
    assert receipt["postcondition"]["readback_action"] == "issue__get_issue_detail"


def test_accepted_write_with_identity_mismatch_requires_reconciliation(tmp_path: Path) -> None:
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, {**ISSUE, "id": "wrong"})])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is False
    assert receipt["postcondition"]["error_code"] == "readback_identity_validation_failed"


def test_accepted_write_with_readback_schema_failure_requires_reconciliation(tmp_path: Path) -> None:
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, {"id": "issue-1"})])

    receipt = invoke(PlaneClient({"base_url": "https://plane.invalid"}, transport), tmp_path / "ledger.json")

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is False
    assert receipt["postcondition"]["error_code"] == "response_schema_validation_failed"


def test_legacy_authorization_fields_do_not_gate_new_idempotency_keys(tmp_path: Path) -> None:
    key = "hermes-211-authorization-key"
    client = PlaneClient({"base_url": "https://plane.invalid"}, QueueTransport([
        http_error(400, b'{"message":"invalid"}'),
        http_error(400, b'{"message":"invalid"}'),
    ]))
    receipt = {
        "action": ACTION, "method": "POST", "approved_live_mutation": True,
        "authorization_scope": "one_operation_one_target", "path_params": PARAMS,
        "payload_fingerprint": payload_fingerprint(PAYLOAD),
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "authorization-h211-0001", **PARAMS,
        "provider_instance_hash": _provider_instance_hash(client.config),
    }
    client.execute_mutation_action(ACTION, path_params=PARAMS, payload=PAYLOAD,
        approved_live_mutation=True, authorization_receipt=receipt, attempts=1,
        idempotency_key=key, ledger_path=tmp_path / "ledger.json")
    client.execute_mutation_action(ACTION, path_params=PARAMS, payload=PAYLOAD,
        approved_live_mutation=True, authorization_receipt=receipt, attempts=1,
        idempotency_key="hermes-211-different-key", ledger_path=tmp_path / "ledger.json")
    assert client._transport is not None
    assert len(client._transport.calls) == 2


def test_module_update_accepts_provider_nullable_optional_detail_fields(tmp_path: Path) -> None:
    params = {
        "workspace_slug": "workspace",
        "project_id": "project",
        "resource_id": "module-1",
    }
    payload = {"status": "cancelled"}
    key = "hermes-211-module-nullable-detail"
    module = {
        "backlog_issues": 0,
        "cancelled_issues": 0,
        "completed_issues": 0,
        "created_at": "2026-08-25T00:00:00Z",
        "description": None,
        "id": "module-1",
        "name": "Demo module",
        "start_date": None,
        "started_issues": 0,
        "status": "cancelled",
        "target_date": None,
        "total_issues": 0,
        "unstarted_issues": 0,
        "updated_at": "2026-08-25T00:00:01Z",
    }
    client = PlaneClient(
        {"base_url": "https://plane.invalid"},
        QueueTransport([
            FakeResponse(200, {
                "name": "Demo module", "description": None, "start_date": None,
                "target_date": None, "status": "cancelled", "lead": None,
                "external_source": None, "external_id": None,
            }),
            FakeResponse(200, module),
        ]),
    )
    authorization = {
        "action": "module__update_module_detail",
        "method": "PATCH",
        "approved_live_mutation": True,
        "authorization_scope": "one_operation_one_target",
        "path_params": params,
        "payload_fingerprint": payload_fingerprint(payload),
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "module-nullable-detail-0001",
        "provider_instance_hash": _provider_instance_hash(client.config),
        "workspace_slug": params["workspace_slug"],
        "project_id": params["project_id"],
    }

    receipt = client.execute_mutation_action(
        "module__update_module_detail",
        path_params=params,
        payload=payload,
        approved_live_mutation=True,
        authorization_receipt=authorization,
        attempts=1,
        idempotency_key=key,
        ledger_path=tmp_path / "ledger.json",
    )

    assert receipt["mutation_applied"] is True
    assert receipt["postcondition_verified"] is True
    assert receipt["postcondition"]["state"] == "provider_acknowledged"
    assert receipt["readback_verified"] is True
    assert len(client._transport.calls) == 2


def test_module_update_reconciliation_appends_exact_effect_proof(tmp_path: Path) -> None:
    params = {
        "workspace_slug": "workspace",
        "project_id": "project",
        "resource_id": "module-1",
    }
    payload = {"status": "cancelled"}
    key_hash = hashlib.sha256(b"hermes-211-module-update-live").hexdigest()
    fingerprint = __import__("plane_api.client", fromlist=["_fingerprint"])._fingerprint(
        "module__update_module_detail", "", "", None,
        {"path_params": params, "query": {}, "payload": payload},
    )
    path = "/api/v1/workspaces/workspace/projects/project/modules/module-1/"
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({
        "receipts": {key_hash: {
            "action": "module__update_module_detail",
            "method": "PATCH",
            "path": path,
            "fingerprint": fingerprint,
            "status": "write_succeeded_readback_validation_failed",
            "mutation_applied": True,
        }}
    }))
    module = {
        "backlog_issues": 0, "cancelled_issues": 0, "completed_issues": 0,
        "created_at": "2026-08-25T00:00:00Z", "description": None,
        "id": "module-1", "name": "Demo", "start_date": None,
        "started_issues": 0, "status": "cancelled", "target_date": None,
        "total_issues": 0, "unstarted_issues": 0,
        "updated_at": "2026-08-25T00:00:01Z",
    }
    client = PlaneClient(
        {"base_url": "https://plane.invalid"}, QueueTransport([FakeResponse(200, module)])
    )
    authorization = {
        "action": "module__update_module_detail",
        "method": "PATCH",
        "approved_live_reconciliation": True,
        "authorization_scope": "one_mutation_attempt_one_target",
        "path_params": params,
        "payload_fingerprint": payload_fingerprint(payload),
        "key_hash": key_hash,
        "authorization_id": "module-update-reconcile-0001",
        "provider_instance_hash": _provider_instance_hash(client.config),
        "contract_version": CONTRACT_VERSION,
        "contract_hash": CONTRACT_HASH,
        "operator_disposition": "current_state_matches_intended_update",
    }

    result = client.reconcile_module_update_attempt(
        **params,
        key_hash=key_hash,
        expected_payload=payload,
        attempts=1,
        approved_live_reconciliation=True,
        authorization_receipt=authorization,
        ledger_path=ledger,
    )

    assert result["provider_mutation_applied"] is False
    assert result["readback_verified"] is True
    assert result["disposition"] == "effect_present_at_reconciliation_time"
    persisted = json.loads(ledger.read_text())
    assert persisted["receipts"][key_hash]["status"] == "write_succeeded_readback_validation_failed"
    assert persisted["reconciliation_events"][-1]["evidence"]["verified_fields"] == ["status"]


def test_module_update_duplicate_reconciles_sparse_ack_with_get_without_replay(tmp_path: Path) -> None:
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    payload = {"status": "cancelled"}
    key = "hermes-211-module-update-duplicate-proof"
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    fingerprint = __import__("plane_api.client", fromlist=["_fingerprint"])._fingerprint(
        "module__update_module_detail", "", "", None,
        {"path_params": params, "query": {}, "payload": payload},
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"receipts": {key_hash: {
        "action": "module__update_module_detail", "method": "PATCH",
        "path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/",
        "fingerprint": fingerprint, "status": "write_succeeded_readback_validation_failed",
        "mutation_applied": True,
        "response_metadata": {"status_code": 200, "body_shape": "object", "body_present": True},
    }}}))
    module = {
        "backlog_issues": 0, "cancelled_issues": 0, "completed_issues": 0,
        "created_at": "2026-08-25T00:00:00Z", "description": None,
        "id": "module-1", "name": "Demo", "start_date": None,
        "started_issues": 0, "status": "cancelled", "target_date": None,
        "total_issues": 0, "unstarted_issues": 0, "updated_at": "2026-08-25T00:00:01Z",
    }
    client = PlaneClient({"base_url": "https://plane.invalid"}, QueueTransport([FakeResponse(200, module)]))
    authorization = {
        "action": "module__update_module_detail", "method": "PATCH",
        "approved_live_mutation": True, "authorization_scope": "one_operation_one_target",
        "path_params": params, "payload_fingerprint": payload_fingerprint(payload),
        "key_hash": key_hash, "authorization_id": "module-update-duplicate-proof-0001",
        "provider_instance_hash": _provider_instance_hash(client.config),
        "workspace_slug": "workspace", "project_id": "project",
    }

    receipt = client.execute_mutation_action(
        "module__update_module_detail", path_params=params, payload=payload,
        approved_live_mutation=True, authorization_receipt=authorization,
        attempts=1, idempotency_key=key, ledger_path=ledger,
    )

    assert receipt["duplicate"] is True
    assert receipt["mutation_applied"] is False
    assert receipt["readback_verified"] is True
    assert receipt["postcondition_verified"] is True
    assert len(client._transport.calls) == 1
    assert client._transport.calls[0][0] == "GET"
    assert json.loads(ledger.read_text())["receipts"][key_hash]["status"] == "verified"


def test_archive_409_is_ambiguous_and_burns_the_key(tmp_path: Path) -> None:
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    key = "hermes-211-archive-conflict"
    authorization = archive_authorization(params) | {
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "archive-conflict-0001",
    }
    client = PlaneClient({"base_url": "https://plane.invalid"}, QueueTransport([http_error(409, b'{"message":"conflict"}')]))
    with pytest.raises(RuntimeError, match="provider request"):
        client.execute_mutation_action("module__archive_module", path_params=params, payload={},
            approved_live_mutation=True, authorization_receipt=authorization, attempts=1,
            idempotency_key=key, ledger_path=tmp_path / "ledger.json")
    with pytest.raises(RuntimeError, match="unknown outcome"):
        client.execute_mutation_action("module__archive_module", path_params=params, payload={},
            approved_live_mutation=True, authorization_receipt=authorization | {"authorization_id": "archive-conflict-0002"}, attempts=1,
            idempotency_key=key, ledger_path=tmp_path / "ledger.json")


def test_reconciliation_paginates_active_modules_after_first_page() -> None:
    empty = {"count": 0, "extra_stats": None, "grouped_by": None, "next_cursor": "", "next_page_results": False, "prev_cursor": "", "prev_page_results": False, "results": [], "sub_grouped_by": None, "total_count": 0, "total_pages": 0, "total_results": 0}
    active_first = empty | {"next_cursor": "active-2", "next_page_results": True, "results": []}
    active_second = empty | {"results": [{"id": "module-1", "name": "active", "created_at": "2026-08-25T00:00:00Z", "description": None, "start_date": None, "status": "planned", "target_date": None}]}
    client = PlaneClient({"base_url": "https://plane.invalid"}, RequestQueueTransport([
        {"status_code": 200, "body": empty}, {"status_code": 200, "body": active_first}, {"status_code": 200, "body": active_second},
    ]))
    proof = client._prove_module_archive_not_applied({"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"})
    assert proof["active_list_pages"] == 2


def test_reconciliation_active_cursor_loop_is_inconclusive_not_absence() -> None:
    empty = {"count": 0, "extra_stats": None, "grouped_by": None, "next_cursor": "", "next_page_results": False, "prev_cursor": "", "prev_page_results": False, "results": [], "sub_grouped_by": None, "total_count": 0, "total_pages": 0, "total_results": 0}
    looping = empty | {"next_cursor": "again", "next_page_results": True}
    client = PlaneClient({"base_url": "https://plane.invalid"}, RequestQueueTransport([
        {"status_code": 200, "body": empty}, {"status_code": 200, "body": looping}, {"status_code": 200, "body": looping},
    ]))
    with pytest.raises(RuntimeError, match="not exhaustive"):
        client._prove_module_archive_not_applied({"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"})


def test_generic_mutation_ignores_legacy_provider_receipt(tmp_path: Path) -> None:
    key = "hermes-211-cross-provider-key"
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE)])
    client = PlaneClient({"base_url": "https://plane-a.invalid"}, transport)
    wrong = PlaneClient({"base_url": "https://plane-b.invalid"})
    receipt = {
        "action": ACTION, "method": "POST", "approved_live_mutation": True,
        "authorization_scope": "one_operation_one_target", "path_params": PARAMS,
        "payload_fingerprint": payload_fingerprint(PAYLOAD), "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "cross-provider-approval-0001", "provider_instance_hash": _provider_instance_hash(wrong.config), **PARAMS,
    }
    result = client.execute_mutation_action(ACTION, path_params=PARAMS, payload=PAYLOAD, approved_live_mutation=True,
        authorization_receipt=receipt, attempts=1, idempotency_key=key, ledger_path=tmp_path / "ledger.json")
    assert result["readback_verified"] is True
    assert len(transport.calls) == 2


def test_generic_duplicate_with_fresh_approval_performs_fresh_readback(tmp_path: Path) -> None:
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE), FakeResponse(200, ISSUE)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    ledger = tmp_path / "ledger.json"
    first = invoke(client, ledger)
    duplicate = invoke(client, ledger)
    assert first["readback_verified"] is True
    assert duplicate["duplicate"] is True
    assert len(transport.calls) == 3


def test_generic_mutation_still_requires_a_ledger_but_allows_native_state_patch() -> None:
    client = PlaneClient({"base_url": "https://plane.invalid"})
    proof = client.preflight_mutation("issue__update_issue_detail", path_params={"workspace_slug": "workspace", "project_id": "project", "resource_id": "issue-1"}, payload={"state": "done"}, approved_live_mutation=True, authorization_receipt={}, attempts=1, idempotency_key="hermes-211-state-denial")
    assert proof["method"] == "PATCH"
    with pytest.raises(TypeError):
        client.execute_mutation_action(ACTION, path_params=PARAMS, payload=PAYLOAD, approved_live_mutation=False, authorization_receipt={}, attempts=1, idempotency_key="hermes-211-explicit-ledger")


def test_legacy_state_preflight_arguments_are_compatibility_only() -> None:
    client = PlaneClient({"base_url": "https://plane.invalid"})
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "issue-1"}
    payload = {"state": "done"}
    key = "hermes-211-runtime-state-key"
    receipt = {
        "action": "issue__update_issue_detail",
        "method": "PATCH",
        "approved_live_mutation": True,
        "authorization_scope": "one_operation_one_target",
        "path_params": params,
        "payload_fingerprint": payload_fingerprint(payload),
        "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "runtime-state-approval-0001",
        "provider_instance_hash": _provider_instance_hash(client.config),
        "workspace_slug": "workspace",
        "project_id": "project",
    }

    proof = client.preflight_mutation(
        "issue__update_issue_detail",
        path_params=params,
        payload=payload,
        approved_live_mutation=True,
        authorization_receipt=receipt,
        attempts=1,
        idempotency_key=key,
        allow_runtime_state_transition=True,
    )

    assert proof["key_hash"] == hashlib.sha256(key.encode()).hexdigest()


def test_comment_authorization_rejects_wrong_provider_and_reuse_before_io(tmp_path: Path) -> None:
    payload = {"comment_html": "<p>governed</p>"}
    key = "hermes-211-comment-key"
    client = PlaneClient({"base_url": "https://plane.invalid"})
    params = {"workspace_slug": "workspace", "project_id": "project", "work_item_id": "issue-1"}
    receipt = {"action": "add_work_item_comment", "method": "POST", "approved_live_mutation": True,
        "authorization_scope": "one_work_item_one_operation", "path_params": params,
        "payload_fingerprint": payload_fingerprint(payload), "key_hash": hashlib.sha256(key.encode()).hexdigest(),
        "authorization_id": "comment-auth-0001", "provider_instance_hash": "wrong"}
    kwargs = dict(**params, payload=payload, approved_live_mutation=True,
        authorization_scope="one_work_item_one_operation", authorized_workspace_slug="workspace",
        authorized_project_id="project", authorized_work_item_id="issue-1", idempotency_key=key,
        attempts=1, authorization_receipt=receipt, ledger_path=tmp_path / "ledger.json")
    with pytest.raises(PermissionError, match="authorization receipt"):
        client.governed_add_work_item_comment(**kwargs)
    receipt["provider_instance_hash"] = _provider_instance_hash(client.config)
    (tmp_path / "ledger.json").write_text(json.dumps({"receipts": {}, "comment_authorizations": {"comment-auth-0001": {}}}))
    with pytest.raises(PermissionError, match="already consumed"):
        client.governed_add_work_item_comment(**kwargs)


def test_comment_provider_calls_are_outside_ledger_lock(tmp_path: Path, monkeypatch) -> None:
    import plane_api.client as client_module
    payload = {"comment_html": "<p>governed</p>"}
    key = "hermes-211-comment-lock-key"
    client = PlaneClient({"base_url": "https://plane.invalid"})
    params = {"workspace_slug": "workspace", "project_id": "project", "work_item_id": "issue-1"}
    locked = 0
    original = client_module._locked_ledger
    @contextmanager
    def observed_lock(path):
        nonlocal locked
        with original(path) as ledger:
            locked += 1
            try:
                yield ledger
            finally:
                locked -= 1
    calls: list[str] = []
    def request(method, _path, payload=None):
        assert locked == 0
        calls.append(method)
        return {"id": "comment-1", "comment_html": payload["comment_html"]} if method == "POST" else {"id": "comment-1", "comment_html": "<p>governed</p>"}
    monkeypatch.setattr(client_module, "_locked_ledger", observed_lock)
    monkeypatch.setattr(client, "_governed_request", request)
    client.governed_add_work_item_comment(**params, payload=payload, approved_live_mutation=True,
        authorization_scope="one_work_item_one_operation", authorized_workspace_slug="workspace",
        authorized_project_id="project", authorized_work_item_id="issue-1", idempotency_key=key, attempts=1,
        authorization_receipt={"action": "add_work_item_comment", "method": "POST", "approved_live_mutation": True,
            "authorization_scope": "one_work_item_one_operation", "path_params": params,
            "payload_fingerprint": payload_fingerprint(payload), "key_hash": hashlib.sha256(key.encode()).hexdigest(),
            "authorization_id": "comment-lock-approval-0001", "provider_instance_hash": _provider_instance_hash(client.config)},
        ledger_path=tmp_path / "ledger.json")
    assert calls == ["POST", "GET"]


def _legacy_reconciliation_setup(tmp_path: Path, authorization_id: str):
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}
    key_hash = hashlib.sha256(b"hermes-211-reconciliation-event-key").hexdigest()
    fingerprint = __import__("plane_api.client", fromlist=["_fingerprint"])._fingerprint(
        "module__archive_module", "", "", None, {"path_params": params, "query": {}, "payload": {}}
    )
    ledger = tmp_path / "ledger.json"
    ledger.write_text(json.dumps({"receipts": {key_hash: {"action": "module__archive_module", "method": "POST", "path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/", "fingerprint": fingerprint, "status": "attempt_started"}}}))
    client = PlaneClient({"base_url": "https://plane.invalid"})
    authorization = {"action": "module__archive_module", "method": "POST", "approved_live_reconciliation": True,
        "authorization_scope": "one_legacy_attempt_one_target", "path_params": params, "key_hash": key_hash,
        "authorization_id": authorization_id, "provider_instance_hash": _provider_instance_hash(client.config),
        "legacy_path": "/api/v1/workspaces/workspace/projects/project/modules/module-1/archive/", "legacy_method": "POST",
        "legacy_fingerprint": fingerprint, "legacy_payload_fingerprint": payload_fingerprint({}), "historical_wire_body_present": True,
        "contract_version": CONTRACT_VERSION, "contract_hash": CONTRACT_HASH, "operator_disposition": "replacement_authorized_after_effect_absence"}
    return client, ledger, params, key_hash, authorization


def test_reconciliation_provider_failure_appends_inconclusive_terminal_event(tmp_path: Path) -> None:
    client, ledger, params, key_hash, authorization = _legacy_reconciliation_setup(tmp_path, "reconcile-inconclusive-0001")
    client._transport = RequestQueueTransport([{"status_code": 500, "body": {"token": "must-not-leak"}}])
    receipt = client.reconcile_legacy_module_archive_attempt(**params, key_hash=key_hash, attempts=1,
        approved_live_reconciliation=True, authorization_receipt=authorization, ledger_path=ledger)
    persisted = json.loads(ledger.read_text())
    event = persisted["reconciliation_events"][0]
    assert receipt["disposition"] == event["disposition"] == "inconclusive"
    assert receipt["current_effect"] == "unknown" and receipt["readback_verified"] is False
    assert event["error"]["code"] == "provider_request_inconclusive"
    assert "must-not-leak" not in json.dumps({"receipt": receipt, "event": event})
    assert persisted["receipts"][key_hash]["status"] == "attempt_started"


def test_fresh_reconciliation_event_references_prior_incomplete_authorization(tmp_path: Path) -> None:
    client, ledger, params, key_hash, authorization = _legacy_reconciliation_setup(tmp_path, "reconcile-fresh-success-0002")
    persisted = json.loads(ledger.read_text())
    persisted["reconciliation_authorizations"] = {"reconcile-prior-incomplete-0001": {"key_hash": key_hash, "action": "module__archive_module"}}
    ledger.write_text(json.dumps(persisted))
    empty = {"count": 0, "extra_stats": None, "grouped_by": None, "next_cursor": "", "next_page_results": False, "prev_cursor": "", "prev_page_results": False, "results": [], "sub_grouped_by": None, "total_count": 0, "total_pages": 0, "total_results": 0}
    active = empty | {"results": [{"id": "module-1", "description": None, "start_date": None, "target_date": None}]}
    client._transport = RequestQueueTransport([{"status_code": 200, "body": empty}, {"status_code": 200, "body": active}])
    receipt = client.reconcile_legacy_module_archive_attempt(**params, key_hash=key_hash, attempts=1,
        approved_live_reconciliation=True, authorization_receipt=authorization, ledger_path=ledger)
    event = json.loads(ledger.read_text())["reconciliation_events"][0]
    assert receipt["disposition"] == "effect_not_present_at_reconciliation_time"
    assert event["prior_incomplete_authorization_ids"] == ["reconcile-prior-incomplete-0001"]


def test_native_mutation_uses_real_client_transport_and_preserves_one_shot_guards(tmp_path: Path) -> None:
    """The simple MCP path is not a fake approval wrapper around legacy execution."""
    ledger = tmp_path / "ledger.json"
    transport = QueueTransport([FakeResponse(201, ISSUE), FakeResponse(200, ISSUE), FakeResponse(200, ISSUE)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    kwargs = {
        "path_params": PARAMS,
        "payload": PAYLOAD,
        "idempotency_key": "native-simple-client-key",
        "ledger_path": ledger,
    }

    receipt = client.execute_native_mutation_action(ACTION, **kwargs)
    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is True
    assert [call[0] for call in transport.calls] == ["POST", "GET"]

    duplicate = client.execute_native_mutation_action(ACTION, **kwargs)
    assert duplicate["duplicate"] is True
    assert len(transport.calls) == 3  # readback only; no second POST

    with pytest.raises(ValueError, match="different mutation"):
        client.execute_native_mutation_action(
            ACTION, path_params=PARAMS, payload={"name": "different"},
            idempotency_key="native-simple-client-key", ledger_path=ledger,
        )


def test_native_state_patch_and_invalid_request_are_preflighted_without_provider_io(tmp_path: Path) -> None:
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "issue-1"}
    transport = QueueTransport([])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    with pytest.raises(PlaneResponseValidationError, match="mutation request"):
        client.execute_native_mutation_action(
            "issue__update_issue_detail", path_params=params, payload={"priority": 7},
            idempotency_key="native-invalid-payload", ledger_path=tmp_path / "ledger.json",
        )
    assert transport.calls == []

    proof = client.preflight_native_mutation(
        "issue__update_issue_detail", path_params=params, payload={"state": "done"},
        attempts=1, idempotency_key="native-state-patch-key",
    )
    assert proof["method"] == "PATCH"
    assert transport.calls == []


def test_comment_collection_schema_accepts_provider_comment_without_name() -> None:
    schema = get_operation("issue_comment__list_issue_comments").response_schema["json_schema"]

    compatible = _provider_compatible_response_schema(
        schema, operation_action="issue_comment__list_issue_comments"
    )
    required = compatible["properties"]["results"]["items"]["required"]

    assert "id" in required
    assert "created_at" in required
    assert "name" not in required


def test_delete_absence_readback_ignores_unrelated_surviving_item_detail_shape(tmp_path: Path) -> None:
    collection = {
        "count": 1,
        "extra_stats": None,
        "grouped_by": None,
        "next_cursor": "",
        "next_page_results": False,
        "prev_cursor": "",
        "prev_page_results": False,
        "results": [{"id": "surviving-item", "state": "state-uuid"}],
        "sub_grouped_by": None,
        "total_count": 1,
        "total_pages": 1,
        "total_results": 1,
    }
    transport = QueueTransport([FakeResponse(204, {}), FakeResponse(200, collection)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)

    receipt = client.execute_native_mutation_action(
        "issue__delete_issue",
        path_params={"workspace_slug": "workspace", "project_id": "project", "resource_id": "deleted-item"},
        payload={},
        idempotency_key="delete-absence-unrelated-detail-shape",
        ledger_path=tmp_path / "ledger.json",
    )

    assert receipt["mutation_applied"] is True
    assert receipt["readback_verified"] is True
