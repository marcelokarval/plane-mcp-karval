from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, cast

from fastmcp import Client
import pytest

from plane_api.client import PlaneClient
import plane_mcp_karval.server as server_module
from plane_mcp_karval.server import create_server


def run(coro):
    return asyncio.run(coro)


class FakeSimpleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute_read_action(self, operation: str, **kwargs):
        self.calls.append(("read", {"operation": operation, **kwargs}))
        if operation == "state__list_states":
            return {"results": [{"id": "started", "name": "Started"}]}
        raise AssertionError(operation)

    def get_work_item(self, workspace_slug: str, project_id: str, work_item_id: str):
        self.calls.append(("get_work_item", (workspace_slug, project_id, work_item_id)))
        return {"id": work_item_id, "state": {"id": "todo"}, "updated_at": "2026-09-14T12:00:00Z"}

    def execute_native_mutation_action(self, operation: str, **kwargs):
        self.calls.append(("mutation", {"operation": operation, **kwargs}))
        return {
            "action": operation,
            "mutation_applied": True,
            "readback_verified": True,
            "duplicate": False,
        }

    def reconcile_native_mutation(self, operation: str, **kwargs):
        self.calls.append(("reconcile", {"operation": operation, **kwargs}))
        return {
            "action": operation,
            "provider_mutation_applied": False,
            "readback_verified": True,
            "status": "reconciled_verified",
        }


class QueueTransport:
    __hermes_test_fake_transport__ = True

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, url))
        return self.responses.pop(0)


def test_multiple_legacy_ledgers_migrate_to_one_canonical_ledger(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "hermes.json"
    second = tmp_path / "codex.json"
    first.write_text(json.dumps({"receipts": {"a": {"status": "verified"}}}))
    second.write_text(json.dumps({"receipts": {"b": {"status": "verified"}}}))
    monkeypatch.delenv("PLANE_MUTATION_LEDGER", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "LEGACY_LEDGERS", (str(first), str(second)))

    target = server_module._ledger_path()

    assert target == tmp_path / "state" / "plane-mcp-karval" / "mutations.json"
    migrated = json.loads(target.read_text())
    assert migrated["receipts"] == {"a": {"status": "verified"}, "b": {"status": "verified"}}
    assert migrated["migration"]["sources"] == sorted([str(first), str(second)])


def test_ledger_migration_records_conflicts_without_discarding_unrelated_receipts(monkeypatch, tmp_path: Path) -> None:
    first = tmp_path / "hermes.json"
    second = tmp_path / "codex.json"
    first.write_text(json.dumps({"receipts": {"conflict": {"status": "verified"}, "first": {"status": "verified"}}}))
    second.write_text(json.dumps({"receipts": {"conflict": {"status": "attempt_started"}, "second": {"status": "verified"}}}))
    monkeypatch.delenv("PLANE_MUTATION_LEDGER", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "LEGACY_LEDGERS", (str(first), str(second)))

    target = server_module._ledger_path()

    migrated = json.loads(target.read_text())
    assert migrated["receipts"]["conflict"] == {"status": "verified"}
    assert migrated["receipts"]["first"] == {"status": "verified"}
    assert migrated["receipts"]["second"] == {"status": "verified"}
    assert migrated["migration_conflicts"]["receipts"]["conflict"] == [str(second)]


def test_legacy_receipt_collision_blocks_only_its_key_and_not_an_unrelated_write(monkeypatch, tmp_path: Path) -> None:
    collision_key = "legacy-collision-idempotency-key"
    collision_hash = hashlib.sha256(collision_key.encode()).hexdigest()
    first = tmp_path / "hermes.json"
    second = tmp_path / "codex.json"
    first.write_text(json.dumps({"receipts": {collision_hash: {"status": "verified"}}}))
    second.write_text(json.dumps({"receipts": {collision_hash: {"status": "attempt_started"}}}))
    monkeypatch.delenv("PLANE_MUTATION_LEDGER", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "LEGACY_LEDGERS", (str(first), str(second)))
    ledger = server_module._ledger_path()
    transport = QueueTransport([{"status_code": 204, "body": None}])
    client = PlaneClient({"base_url": "https://plane.invalid"}, transport)
    monkeypatch.setattr(client, "_evaluate_generic_postcondition", lambda *_args, **_kwargs: {"state": "verified"})
    params = {"workspace_slug": "workspace", "project_id": "project", "resource_id": "module-1"}

    with pytest.raises(RuntimeError, match="collides across legacy mutation ledgers"):
        client.execute_native_mutation_action(
            "module__archive_module", path_params=params, payload={},
            idempotency_key=collision_key, ledger_path=ledger,
        )
    assert transport.calls == []

    result = client.execute_native_mutation_action(
        "module__archive_module", path_params=params, payload={},
        idempotency_key="unrelated-legacy-migration-key", ledger_path=ledger,
    )
    assert result["mutation_applied"] is True
    assert transport.calls[0][0] == "POST"


def test_capture_state_catalog_is_a_read_only_convenience(monkeypatch, tmp_path: Path) -> None:
    fake = FakeSimpleClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_capture_state_catalog",
                {"workspace_slug": "karval", "project_id": "project-1"},
            )
            assert result.data["states"] == [{"id": "started", "name": "Started"}]
            assert result.data["source_action"] == "state__list_states"

    run(check())
    assert fake.calls == [
        ("read", {"operation": "state__list_states", "path_params": {"workspace_slug": "karval", "project_id": "project-1"}, "query": {}})
    ]


def test_optional_lifecycle_transition_uses_simple_native_mutation(monkeypatch, tmp_path: Path) -> None:
    fake = FakeSimpleClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_lifecycle_transition",
                {
                    "workspace_slug": "karval",
                    "project_id": "project-1",
                    "work_item_id": "issue-1",
                    "target_state_id": "started",
                    "idempotency_key": "lifecycle-transition-00001",
                    "expected_state_id": "todo",
                    "expected_updated_at": "2026-09-14T12:00:00Z",
                },
            )
            assert result.data["status"] == "state_verified"
            assert result.data["state"]["readback_verified"] is True

    run(check())
    assert fake.calls[0][0] == "get_work_item"
    assert fake.calls[1] == (
        "mutation",
        {
            "operation": "issue__update_issue_detail",
            "path_params": {"workspace_slug": "karval", "project_id": "project-1", "resource_id": "issue-1"},
            "query": {},
            "payload": {"state": "started"},
            "idempotency_key": "lifecycle-transition-00001:state",
            "attempts": 1,
            "ledger_path": tmp_path / "ledger.json",
        },
    )


def test_simple_comment_does_not_require_a_lifecycle_contract(monkeypatch, tmp_path: Path) -> None:
    fake = FakeSimpleClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_add_comment",
                {
                    "workspace_slug": "karval",
                    "project_id": "project-1",
                    "work_item_id": "issue-1",
                    "comment_html": "<p>Normal comment.</p>",
                    "idempotency_key": "simple-comment-00001",
                },
            )
            assert result.data["readback_verified"] is True

    run(check())
    assert fake.calls == [
        (
            "mutation",
            {
                "operation": "issue_comment__add_issue_comment",
                "path_params": {"workspace_slug": "karval", "project_id": "project-1", "work_item_id": "issue-1"},
                "query": {},
                "payload": {"comment_html": "<p>Normal comment.</p>", "external_source": "plane-mcp-karval"},
                "idempotency_key": "simple-comment-00001",
                "attempts": 1,
                "ledger_path": tmp_path / "ledger.json",
            },
        )
    ]


def test_reconcile_mutation_uses_internal_ledger_not_caller_receipts(monkeypatch, tmp_path: Path) -> None:
    fake = FakeSimpleClient()
    monkeypatch.setattr(server_module, "_client", lambda: fake)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))

    async def check() -> None:
        async with Client(create_server()) as client:
            result = await client.call_tool(
                "plane_reconcile_mutation",
                {
                    "operation": "issue__update_issue_detail",
                    "path_params": {"workspace_slug": "karval", "project_id": "project-1", "resource_id": "issue-1"},
                    "payload": {"state": "started"},
                    "idempotency_key": "lifecycle-transition-00001:state",
                },
            )
            assert result.data["status"] == "reconciled_verified"

    run(check())
    forwarded = cast(dict[str, Any], fake.calls[0][1])
    assert forwarded["operation"] == "issue__update_issue_detail"
    assert forwarded["attempts"] == 1
    assert "authorization_receipt" not in forwarded


def test_server_instructions_do_not_require_descriptor_or_readiness() -> None:
    server = create_server()
    instructions = str(server.instructions)

    assert "before any mutation" not in instructions
    assert "caller-supplied idempotency key" in instructions
