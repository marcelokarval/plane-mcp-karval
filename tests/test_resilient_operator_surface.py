"""Explicit retry authority is reachable only as a separate MCP decision."""
import asyncio

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from plane_api import resilience
from plane_mcp_karval import server
from test_resilient_mutations import ACTION, Transport, persisted, setup


def test_public_operator_authorization_does_not_send_or_get_consumed_by_reads(monkeypatch, tmp_path):
    transport = Transport(lost=True)
    real_client, kwargs = setup(tmp_path, transport)
    monkeypatch.setattr(server, "_client", lambda: real_client)
    monkeypatch.setenv("PLANE_MUTATION_LEDGER", str(kwargs["ledger_path"]))
    monkeypatch.setattr(resilience.time, "sleep", lambda _: None)
    args = {"operation": ACTION, "path_params": kwargs["path_params"], "payload": kwargs["payload"],
            "idempotency_key": kwargs["idempotency_key"]}

    async def exercise():
        async with Client(server.create_server()) as client:
            first = (await client.call_tool("plane_mutation_action", args)).data
            assert first["status"] == "outcome_unknown"
            # Simulate explicit provider absence; not an inference from the first error.
            transport.pages = [{"results": [], "next_page_results": False, "total_results": 0,
                                "total_count": 0, "count": 0, "total_pages": 1}]
            scan = (await client.call_tool("plane_reconcile_mutation", args)).data
            assert scan["recovery_evidence"]["complete"] is True
            auth_args = {**args, "operator_acknowledged_duplicate_risk": False,
                         "reason": "Offline explicit authority regression"}
            before = list(transport.calls)
            with pytest.raises(ToolError, match="acknowledg"):
                await client.call_tool("plane_authorize_mutation_reattempt", auth_args)
            assert transport.calls == before
            auth_args["operator_acknowledged_duplicate_risk"] = True
            authorized = (await client.call_tool("plane_authorize_mutation_reattempt", auth_args)).data
            assert authorized["provider_write_performed"] is False
            assert transport.calls == before
            pending = (await client.call_tool("plane_reconcile_mutation", args)).data
            assert pending["status"] == "authorized_retry"
            assert transport.calls == before
            assert persisted(kwargs)["reattempt_authorizations"][0]["consumed_at"] is None
            transport.lost = False
            delivered = (await client.call_tool("plane_mutation_action", args)).data
            assert delivered["status"] == "verified"
            receipt = persisted(kwargs)
            assert receipt["reattempt_authorizations"][0]["consumed_at"]
            assert receipt["uncertain_attempts"][0]["authorization_id"] == authorized["authorization_id"]
            assert receipt["active_authorization_id"] == authorized["authorization_id"]
            after = list(transport.calls)
            repeated = (await client.call_tool("plane_mutation_action", args)).data
            assert repeated["duplicate"] is True
            assert transport.calls == after

    asyncio.run(exercise())
    assert transport.calls.count("POST") == 2
