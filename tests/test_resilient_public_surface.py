"""Exercise recovery through real MCP tools, not a stubbed mutation client."""
import asyncio
import json
import socket
from urllib.error import URLError
from urllib.parse import urlsplit

import pytest
from fastmcp import Client
from plane_api.client import PlaneClient
from plane_api.registry import get_operation
from plane_api import resilience
from plane_mcp_karval import server
from test_plane_client_failure_receipts import FakeResponse, ISSUE


@pytest.mark.parametrize("failure", ["dns", "lost", "response_dns"])
@pytest.mark.parametrize("kind", ["issue", "comment"])
def test_native_recovery_through_public_mcp_tools(monkeypatch, tmp_path, failure, kind):
    calls = []
    remote = []
    failures = [1]
    action = "issue__add_issue" if kind == "issue" else "issue_comment__add_issue_comment"
    params = {"workspace_slug": "workspace", "project_id": "project"}
    payload = {"name": ISSUE["name"]}
    if kind == "comment":
        params["work_item_id"] = "issue-1"
        payload = {"comment_html": "<p>Recovered public comment</p>", "external_source": "plane-mcp-karval"}

    def transport(request, timeout):
        method = request.get_method()
        calls.append(method)
        if method == "POST":
            if failure == "dns" and failures[0]:
                failures[0] -= 1
                raise URLError(socket.gaierror(socket.EAI_AGAIN, "transient fixture"))
            base = ISSUE if kind == "issue" else {"created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z", "actor": "00000000-0000-4000-8000-000000000002",
                    "id": "comment-1", "project": "project", "issue": "issue-1",
                    "workspace": "00000000-0000-4000-8000-000000000001", "access": "INTERNAL"}
            row = {**base, **json.loads(request.data)}
            remote.append(row)
            if failure == "lost":
                raise URLError("response lost after fixture applied write")
            if failure == "response_dns":
                class LostResponse(FakeResponse):
                    def read(self):
                        raise socket.gaierror(socket.EAI_AGAIN, "failure after response acquired")
                return LostResponse(201, row)
            return FakeResponse(201, row)
        if urlsplit(request.full_url).path.endswith(("work-items/", "comments/")):
            listing = {"results": remote, "next_page_results": False}
            if kind == "comment":
                listing = {**get_operation("issue_comment__list_issue_comments").response_schema["example"],
                           "results": remote,
                           "count": len(remote), "total_count": len(remote), "total_results": len(remote),
                           "total_pages": 1, "next_page_results": False, "next_cursor": "100:1:0"}
            return FakeResponse(200, listing)
        return FakeResponse(200, remote[0])

    setattr(transport, "__hermes_test_fake_transport__", True)
    real_client = PlaneClient({"base_url": "https://plane.invalid", "token": "test-only"}, transport)
    monkeypatch.setattr(server, "_client", lambda: real_client)
    monkeypatch.setenv("PLANE_WORKSPACE_SLUG", "workspace")
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(tmp_path / "ledger.json"))
    monkeypatch.setattr(resilience.time, "sleep", lambda _: None)
    args = {"operation": action, "path_params": params, "payload": payload,
            "idempotency_key": "public-recovery-stable-key"}
    shortcut = {"workspace_slug": "workspace", "project_id": "project", "work_item_id": "issue-1", "comment_html": payload.get("comment_html"),
                "idempotency_key": args["idempotency_key"]}

    async def exercise():
        async with Client(server.create_server()) as client:
            first = (await client.call_tool("plane_add_comment", shortcut) if kind == "comment" else
                     await client.call_tool("plane_mutation_action", args)).data
            if failure in {"lost", "response_dns"}:
                assert first["status"] == "outcome_unknown"
                recovered = (await client.call_tool("plane_reconcile_mutation", args)).data
                assert recovered["status"] == "verified"
            else:
                assert first["status"] == "verified", json.dumps(first) + "\n" + (tmp_path / "ledger.json").read_text()
            before = list(calls)
            repeated = (await client.call_tool("plane_add_comment", shortcut) if kind == "comment" else
                        await client.call_tool("plane_mutation_action", args)).data
            assert repeated["status"] == "verified"
            assert repeated["duplicate"] is True
            assert calls == before

    asyncio.run(exercise())
    assert len(remote) == 1
    assert calls.count("POST") == (2 if failure == "dns" else 1)
