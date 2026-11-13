"""Transport-specific tool policy for the provider-free offline server."""

from __future__ import annotations

import asyncio
import inspect
from typing import Final

from fastmcp import FastMCP

from plane_mcp_karval.server import create_server


OFFLINE_TOOL_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "plane_catalog",
        "plane_action_descriptor",
        "plane_normalize_title",
        "plane_validate_title_contract",
        "plane_validate_work_item_contract",
        "plane_render_lifecycle_comment",
        "plane_prepare_issue_creation",
    }
)


def _public_tools(server: FastMCP) -> dict[str, object]:
    list_tools = getattr(server, "list_tools", None)
    remove_tool = getattr(server.local_provider, "remove_tool", None)
    if not callable(list_tools) or not inspect.iscoroutinefunction(list_tools):
        raise RuntimeError("FastMCP public async list_tools API is required")
    if not callable(remove_tool):
        raise RuntimeError("FastMCP public remove_tool API is required")
    return {tool.name: tool for tool in asyncio.run(list_tools())}


def create_offline_server() -> FastMCP:
    """Create a FastMCP server exposing only the provider-free tool surface."""

    server = create_server()
    tools = _public_tools(server)

    for name in tools:
        if name not in OFFLINE_TOOL_ALLOWLIST:
            server.local_provider.remove_tool(name)

    retained = _public_tools(server)
    missing = OFFLINE_TOOL_ALLOWLIST - retained.keys()
    if missing:
        raise RuntimeError(f"offline allowlisted tools are missing: {sorted(missing)}")

    for name in OFFLINE_TOOL_ALLOWLIST:
        annotations = getattr(retained[name], "annotations", None)
        if getattr(annotations, "read_only_hint", None) is not True:
            raise RuntimeError(f"offline tool {name!r} must have readOnlyHint=True")

    return server


__all__ = ["OFFLINE_TOOL_ALLOWLIST", "create_offline_server"]
