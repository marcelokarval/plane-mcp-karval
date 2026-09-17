"""Focused v0.3.4 contract and operational-gate tests."""
from __future__ import annotations

from typing import Any, cast

from scripts.plane_contract_diagnostics import diagnostics
from scripts.verify_contract_parity import verify
from scripts.verify_release_consistency import check
from plane_api.client import _readback_contains_targets, _validate_response_shape
from plane_api.registry import get_mutation_contract, get_operation
from test_plane_client_failure_receipts import FakeResponse, QueueTransport


V034_ACTIONS = {
    "inbox_issue__add_inbox_issue",
    "inbox_issue__delete_inbox_issue",
    "inbox_issue__get_inbox_issue_detail",
    "inbox_issue__list_inbox_issues",
    "inbox_issue__update_inbox_issue_detail",
    "work_item_relation__create_work_item_relation",
    "work_item_relation__list_work_item_relations",
    "work_item_relation__remove_work_item_relation",
}


def test_v034_actions_are_registered_with_official_docs() -> None:
    actual = {
        action
        for action in V034_ACTIONS
        if get_operation(action).docs_path.startswith("/api-reference/")
    }
    assert actual == V034_ACTIONS
    assert verify()["status"] == "PASS"


def test_relation_map_readback_validates_presence_and_absence() -> None:
    operation = get_operation("work_item_relation__list_work_item_relations")
    body = {
        "blocking": ["related-id"],
        "blocked_by": [],
        "duplicate": [],
        "relates_to": [],
        "start_after": [],
        "start_before": [],
        "finish_after": [],
        "finish_before": [],
    }
    _validate_response_shape(body, {"response_shape": "relation_map"}, operation.response_schema, context="test")
    create_spec = get_mutation_contract("work_item_relation__create_work_item_relation")["postcondition"]
    remove_spec = get_mutation_contract("work_item_relation__remove_work_item_relation")["postcondition"]
    assert _readback_contains_targets(body, {"related-id"}, create_spec, payload={"relation_type": "blocking"})
    assert _readback_contains_targets(body, {"related-id"}, remove_spec, payload=None)
    absent_body = {key: [] for key in body}
    assert not _readback_contains_targets(absent_body, {"related-id"}, remove_spec, payload=None)


def test_relation_mutations_execute_post_then_relation_map_readback(tmp_path) -> None:
    relation = {
        "id": "related-id", "name": "Related", "sequence_id": 1,
        "project_id": "project", "relation_type": "blocking", "state_id": "state",
        "priority": "none", "type_id": "type", "is_epic": False,
        "created_at": "2026-09-17T00:00:00Z", "updated_at": "2026-09-17T00:00:00Z",
        "created_by": "user", "updated_by": "user",
    }
    relation_map = {
        "blocking": ["related-id"], "blocked_by": [], "duplicate": [], "relates_to": [],
        "start_after": [], "start_before": [], "finish_after": [], "finish_before": [],
    }
    create_transport = QueueTransport([FakeResponse(201, cast(Any, [[relation]])), FakeResponse(200, relation_map)])
    from plane_api.client import PlaneClient

    created = PlaneClient({"base_url": "https://plane.invalid"}, create_transport).execute_native_mutation_action(
        "work_item_relation__create_work_item_relation",
        path_params={"workspace_slug": "workspace", "project_id": "project", "work_item_id": "source"},
        payload={"relation_type": "blocking", "issues": ["related-id"]},
        idempotency_key="relation-create-v034-0001",
        ledger_path=tmp_path / "create-ledger.json",
    )
    assert [call[0] for call in create_transport.calls] == ["POST", "GET"]
    assert created["readback_verified"] is True

    remove_transport = QueueTransport([FakeResponse(204, cast(Any, None)), FakeResponse(200, {key: [] for key in relation_map})])
    removed = PlaneClient({"base_url": "https://plane.invalid"}, remove_transport).execute_native_mutation_action(
        "work_item_relation__remove_work_item_relation",
        path_params={"workspace_slug": "workspace", "project_id": "project", "work_item_id": "source"},
        payload={"related_issue": "related-id"},
        idempotency_key="relation-remove-v034-0001",
        ledger_path=tmp_path / "remove-ledger.json",
    )
    assert [call[0] for call in remove_transport.calls] == ["POST", "GET"]
    assert removed["readback_verified"] is True


def test_v034_diagnostics_are_offline_and_version_aligned() -> None:
    result = diagnostics()
    assert result["status"] == "PASS"
    assert result["provider_calls"] == 0
    assert result["live_mutations"] == 0
    assert result["secret_values_emitted"] is False
    assert result["package_version"] == "0.3.4"
    assert result["skill_version"] == "0.3.4"


def test_v034_release_gate_passes() -> None:
    result = check("0.3.4")
    assert result["status"] == "PASS"
    assert result["errors"] == []
