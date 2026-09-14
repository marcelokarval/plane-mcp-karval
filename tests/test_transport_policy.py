import asyncio

import pytest
from fastmcp import FastMCP
from mcp.types import ToolAnnotations

import plane_mcp_karval.transport_policy as transport_policy


ALLOWLIST = {
    "plane_catalog",
    "plane_action_descriptor",
    "plane_normalize_title",
    "plane_validate_title_contract",
    "plane_validate_work_item_contract",
    "plane_render_lifecycle_comment",
    "plane_prepare_issue_creation",
}


def test_offline_server_retains_exact_provider_free_allowlist():
    server = transport_policy.create_offline_server()

    tools = asyncio.run(server.get_tools())

    assert set(tools) == ALLOWLIST
    assert all(tool.annotations.readOnlyHint is True for tool in tools.values())


def test_offline_server_denies_mutation_read_and_reconciliation_tools():
    server = transport_policy.create_offline_server()

    names = set(asyncio.run(server.get_tools()))

    assert "plane_read_action" not in names
    assert "plane_mutation_action" not in names
    assert not any("reconcile" in name for name in names)


def test_future_read_only_tool_is_removed_when_not_allowlisted(monkeypatch):
    original_create_server = transport_policy.create_server

    def create_server_with_future_tool():
        server = original_create_server()

        @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
        def future_provider_free_tool() -> dict[str, bool]:
            return {"ok": True}

        return server

    monkeypatch.setattr(transport_policy, "create_server", create_server_with_future_tool)

    server = transport_policy.create_offline_server()

    assert set(asyncio.run(server.get_tools())) == ALLOWLIST


def test_contradictory_retained_annotation_fails_closed(monkeypatch):
    original_create_server = transport_policy.create_server

    def create_server_with_contradiction():
        server = original_create_server()
        tools = asyncio.run(server.get_tools())
        tools["plane_catalog"].annotations.readOnlyHint = False
        return server

    monkeypatch.setattr(transport_policy, "create_server", create_server_with_contradiction)

    with pytest.raises(RuntimeError, match="readOnlyHint"):
        transport_policy.create_offline_server()


def test_catalog_call_does_not_construct_a_plane_client(monkeypatch):
    import plane_mcp_karval.server as server_module

    monkeypatch.setattr(server_module, "_client", lambda: pytest.fail("client used"))

    server = transport_policy.create_offline_server()
    tools = asyncio.run(server.get_tools())

    result = asyncio.run(tools["plane_catalog"].run({}))

    assert result.content
