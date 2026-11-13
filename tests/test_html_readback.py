"""Preserve exact HTML inside one observed plain provider envelope."""
import json

import pytest

from plane_api.client import (
    PlaneClient, _validate_detail_effects, _verify_comment_readback, _verify_readback,
)
from test_plane_client_failure_receipts import FakeResponse, ISSUE, QueueTransport


HTML = '<p id="original">Read <a href="https://example.invalid/a" rel="nofollow">this</a>.</p>'
WRAPPED = '<div><p id="original">Read <a href="https://example.invalid/a" rel="nofollow">this</a>.</p></div>'
PARAMS = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "issue-1"}


def verify(kind, payload, row):
    if kind == "issue":
        _verify_readback("issue-1", payload, row)
    elif kind == "comment":
        _verify_comment_readback("issue-1", payload, row)
    else:
        _validate_detail_effects(row, payload, {"effects": {"payload_fields": list(payload)}}, allow_html_envelope=True)


@pytest.mark.parametrize("kind,field", [("issue", "description_html"), ("comment", "comment_html"), ("detail", "description_html")])
def test_one_plain_envelope_preserves_exact_html(kind, field):
    verify(kind, {field: HTML}, {"id": "issue-1", field: WRAPPED})


@pytest.mark.parametrize("observed", [
    WRAPPED.replace('/a"', '/b"'),
    WRAPPED.replace('id="original"', 'id="changed"'),
    WRAPPED.replace('rel="nofollow"', 'rel="noopener"'),
    WRAPPED.replace('this</a>', 'that</a>'),
    WRAPPED.replace('Read ', 'Read  '),
    WRAPPED.replace('<div>', '<div class="editor">', 1),
    '<div>' + WRAPPED + '</div>',
    WRAPPED + '<p>extra</p>',
    '<!--extra-->' + WRAPPED,
    WRAPPED.replace('<a href="https://example.invalid/a" rel="nofollow">this</a>', 'this'),
    WRAPPED.replace('</div>', ''),
    None,
])
@pytest.mark.parametrize("kind,field", [("issue", "description_html"), ("comment", "comment_html"), ("detail", "description_html")])
def test_changed_content_or_envelope_never_proves_html(kind, field, observed):
    with pytest.raises(RuntimeError):
        verify(kind, {field: HTML}, {"id": "issue-1", field: observed})


@pytest.mark.parametrize("kind,field", [("issue", "description_html"), ("comment", "comment_html"), ("detail", "description_html"), ("issue", "state"), ("comment", "external_id")])
def test_missing_requested_field_is_not_verified(kind, field):
    with pytest.raises(RuntimeError):
        verify(kind, {field: None}, {"id": "issue-1"})


@pytest.mark.parametrize("kind,field", [("issue", "description_html"), ("comment", "comment_html")])
def test_wrong_identity_not_hidden_by_valid_envelope(kind, field):
    with pytest.raises(RuntimeError):
        verify(kind, {field: HTML}, {"id": "other", field: WRAPPED})


@pytest.mark.parametrize("field,expected,observed", [("state", "open", "done"), ("parent", "one", "two"), ("priority", "low", "high"), ("is_draft", True, 1), ("estimate_point", 1, True)])
def test_scalar_changes_stay_strict(field, expected, observed):
    with pytest.raises(RuntimeError):
        _verify_readback("issue-1", {field: expected}, {"id": "issue-1", field: observed})


def test_patch_envelope_and_read_only_reconcile_never_replay(tmp_path):
    payload = {"description_html": HTML}
    row = {**ISSUE, "description_html": WRAPPED}
    transport = QueueTransport([FakeResponse(200, row), FakeResponse(200, row)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    kwargs = dict(path_params=PARAMS, payload=payload, idempotency_key="html-envelope-patch-0001", ledger_path=tmp_path / "ledger.json")
    result = client.execute_native_mutation_action("issue__update_issue_detail", **kwargs)
    assert result["readback_verified"] is True
    before = json.loads(kwargs["ledger_path"].read_text())["receipts"]
    again = client.reconcile_native_mutation("issue__update_issue_detail", **kwargs)
    assert again["readback_verified"] is True
    assert again["replayed_write"] is False
    assert [call[0] for call in transport.calls] == ["PATCH", "GET"]
    assert json.loads(transport.calls[0][3]) == payload == {"description_html": HTML}
    assert before == json.loads(kwargs["ledger_path"].read_text())["receipts"]


def test_existing_ack_failure_reconciles_with_original_binding_only_get(tmp_path):
    bad = {**ISSUE, "description_html": "<p>different</p>"}
    good = {**ISSUE, "description_html": WRAPPED}
    transport = QueueTransport([FakeResponse(200, bad), FakeResponse(200, bad), FakeResponse(200, good)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    kwargs = dict(path_params=PARAMS, payload={"description_html": HTML}, idempotency_key="html-reconcile-patch-0001", ledger_path=tmp_path / "ledger.json")
    first = client.execute_native_mutation_action("issue__update_issue_detail", **kwargs)
    assert first["mutation_applied"] is True
    assert first["readback_verified"] is False
    before = next(iter(json.loads(kwargs["ledger_path"].read_text())["receipts"].values()))
    result = client.reconcile_native_mutation("issue__update_issue_detail", **kwargs)
    assert result["readback_verified"] is True
    assert result["replayed_write"] is False
    assert result["operation_id"] == first["operation_id"]
    after = next(iter(json.loads(kwargs["ledger_path"].read_text())["receipts"].values()))
    assert after["fingerprint"] == before["fingerprint"]
    assert after["key_hash"] == before["key_hash"]
    assert [call[0] for call in transport.calls] == ["PATCH", "GET", "GET"]
    with pytest.raises(ValueError, match="different mutation"):
        client.reconcile_native_mutation("issue__update_issue_detail", **{**kwargs, "payload": {"description_html": WRAPPED}})


def test_other_detail_contracts_do_not_inherit_html_tolerance():
    with pytest.raises(RuntimeError):
        _validate_detail_effects({"description_html": WRAPPED}, {"description_html": HTML}, {"effects": {"payload_fields": ["description_html"]}})


@pytest.mark.parametrize("kind,field", [("issue", "description_html"), ("comment", "comment_html"), ("detail", "description_html")])
def test_literal_html_remains_valid(kind, field):
    verify(kind, {field: HTML}, {"id": "issue-1", field: HTML})


def test_text_evidence_cannot_verify_changed_links_or_attributes():
    from plane_api.resilience import effect_evidence

    effects = effect_evidence({"description_html": WRAPPED.replace('/a"', '/b"')}, {"description_html": HTML})
    assert effects == {"description_html": "text_equivalent"}
    assert not all(state == "verified" for state in effects.values())


@pytest.mark.parametrize("observed", [WRAPPED.replace('/a"', '/b"'), None])
def test_native_patch_rejects_changed_or_missing_html(tmp_path, observed):
    row = {**ISSUE}
    if observed is not None:
        row["description_html"] = observed
    transport = QueueTransport([FakeResponse(200, row), FakeResponse(200, row), FakeResponse(200, row)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    kwargs = dict(path_params=PARAMS, payload={"description_html": HTML}, idempotency_key="html-negative-patch-0001", ledger_path=tmp_path / "ledger.json")
    first = client.execute_native_mutation_action("issue__update_issue_detail", **kwargs)
    assert first["readback_verified"] is False
    assert client.reconcile_native_mutation("issue__update_issue_detail", **kwargs)["readback_verified"] is False
    assert [call[0] for call in transport.calls] == ["PATCH", "GET", "GET"]


def test_public_mcp_patch_uses_envelope_proof_without_replay(monkeypatch, tmp_path):
    import asyncio
    from fastmcp import Client
    from plane_mcp_karval import server

    row = {**ISSUE, "description_html": WRAPPED}
    transport = QueueTransport([FakeResponse(200, row), FakeResponse(200, row)])
    real_client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    monkeypatch.setattr(server, "_client", lambda: real_client)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))
    args = {"operation": "issue__update_issue_detail", "path_params": PARAMS,
            "payload": {"description_html": HTML}, "idempotency_key": "html-public-patch-0001"}

    async def exercise():
        async with Client(server.create_server()) as client:
            first = (await client.call_tool("plane_mutation_action", args)).data
            assert first["readback_verified"] is True
            result = (await client.call_tool("plane_reconcile_mutation", args)).data
            assert result["readback_verified"] is True
            assert result["operation_id"] == first["operation_id"]
            assert result["replayed_write"] is False

    asyncio.run(exercise())
    assert [call[0] for call in transport.calls] == ["PATCH", "GET"]


@pytest.mark.parametrize("observed,verified", [
    (WRAPPED, True),
    (HTML, True),
    (WRAPPED.replace('/a"', '/b"'), False),
    (WRAPPED.replace('this</a>', 'changed</a>'), False),
    (WRAPPED.replace('id="original"', 'id="changed"'), False),
    (None, False),
])
def test_native_create_proves_submitted_html_not_just_identity(tmp_path, observed, verified):
    ack = {**ISSUE, "description_html": HTML}
    row = {**ISSUE}
    if observed is not None:
        row["description_html"] = observed
    transport = QueueTransport([FakeResponse(201, ack), FakeResponse(200, row)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    payload = {"name": ISSUE["name"], "description_html": HTML}
    kwargs = dict(path_params={"workspace_slug": "workspace", "project_id": "project"}, payload=payload,
                  idempotency_key="html-native-create-0001", ledger_path=tmp_path / "ledger.json")
    result = client.execute_native_mutation_action("issue__add_issue", **kwargs)
    assert result["mutation_applied"] is True
    assert result["readback_verified"] is verified
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
    assert transport.calls[1][1].endswith("/work-items/issue-1/")
    assert json.loads(transport.calls[0][3])["description_html"] == payload["description_html"] == HTML


@pytest.mark.parametrize("field,expected,observed", [
    ("name", ISSUE["name"], "changed"), ("priority", "low", "high"),
    ("state", "open", "done"), ("parent", "parent-a", "parent-b"),
    ("external_id", "source-a", "source-b"), ("external_source", "source-a", "source-b"),
    ("labels", ["label-a"], []), ("assignees", ["user-a"], []),
    ("start_date", "2026-11-13", "2026-11-14"),
    ("target_date", "2026-11-13", "2026-11-14"),
])
def test_native_create_checks_other_submitted_effects(tmp_path, field, expected, observed):
    payload = {"name": ISSUE["name"], field: expected}
    ack = {**ISSUE, **payload}
    row = {**ack, field: observed}
    transport = QueueTransport([FakeResponse(201, ack), FakeResponse(200, row)])
    result = PlaneClient({"base_url": "https://plane.invalid"}, transport).execute_native_mutation_action(
        "issue__add_issue", path_params={"workspace_slug": "workspace", "project_id": "project"},
        payload=payload, idempotency_key="create-scalar-effect-0001", ledger_path=tmp_path / "ledger.json")
    assert result["readback_verified"] is False
    assert [call[0] for call in transport.calls] == ["POST", "GET"]


def test_create_failed_html_proof_recovers_by_get_not_post(tmp_path):
    ack = {**ISSUE, "description_html": HTML}
    bad = {**ack, "description_html": WRAPPED.replace('/a"', '/b"')}
    good = {**ack, "description_html": WRAPPED}
    transport = QueueTransport([FakeResponse(201, ack), FakeResponse(200, bad), FakeResponse(200, good)])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    kwargs = dict(path_params={"workspace_slug": "workspace", "project_id": "project"},
                  payload={"name": ISSUE["name"], "description_html": HTML},
                  idempotency_key="create-get-recovery-0001", ledger_path=tmp_path / "ledger.json")
    first = client.execute_native_mutation_action("issue__add_issue", **kwargs)
    assert first["readback_verified"] is False
    before = next(iter(json.loads(kwargs["ledger_path"].read_text())["receipts"].values()))
    result = client.reconcile_native_mutation("issue__add_issue", **kwargs)
    assert result["readback_verified"] is True
    assert result["operation_id"] == first["operation_id"]
    assert result["replayed_write"] is False
    after = next(iter(json.loads(kwargs["ledger_path"].read_text())["receipts"].values()))
    assert after["fingerprint"] == before["fingerprint"]
    assert after["key_hash"] == before["key_hash"]
    assert [call[0] for call in transport.calls] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("field,expected,observed", [
    ("is_draft", True, 1), ("estimate_point", 1, True),
])
def test_create_detail_effects_reject_boolean_number_equality(field, expected, observed):
    with pytest.raises(RuntimeError):
        _validate_detail_effects(
            {field: observed}, {field: expected},
            {"effects": {"payload_fields": [field]}}, allow_html_envelope=True,
        )


def test_create_unset_schedule_matches_actual_wire_omission(tmp_path):
    payload = {"name": ISSUE["name"], "description_html": HTML,
               "start_date": None, "target_date": None, "estimate_point": None}
    row = {**ISSUE, "description_html": WRAPPED}
    transport = QueueTransport([FakeResponse(201, row), FakeResponse(200, row)])
    result = PlaneClient({"base_url": "https://plane.invalid"}, transport).execute_native_mutation_action(
        "issue__add_issue", path_params={"workspace_slug": "workspace", "project_id": "project"},
        payload=payload, idempotency_key="create-wire-omission-0001", ledger_path=tmp_path / "ledger.json")
    assert result["readback_verified"] is True
    sent = json.loads(transport.calls[0][3])
    assert not {"start_date", "target_date", "estimate_point"}.intersection(sent)
    assert payload["start_date"] is None
    assert [call[0] for call in transport.calls] == ["POST", "GET"]
