"""Verify an installed Plane MCP over real stdio, optionally one provider GET.

Run with the release interpreter, never a model turn. Only public contract
metadata and boolean provider proof are printed; no provider payload is emitted.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from fastmcp import Client


async def verify(args: argparse.Namespace) -> dict:
    if args.launcher:
        command, command_args = "bash", [str(Path(args.launcher).resolve())]
    else:
        command = str(Path(args.python).absolute())
        command_args = ["-I", "-c", "from plane_mcp_karval.cli import main; main()", "stdio"]
    config = {"mcpServers": {"release": {"command": command, "args": command_args}}}
    async with Client(config) as client:
        tools = await client.list_tools()
        by_name = {tool.name: tool for tool in tools}
        required = {
            "plane_catalog", "plane_action_descriptor", "plane_read_action",
            "plane_mutation_action", "plane_add_comment",
            "plane_capture_state_catalog", "plane_lifecycle_transition",
            "plane_operator_lifecycle_transition",
            "plane_reconcile_mutation", "plane_add_lifecycle_comment",
            "get_current_user", "list_projects", "list_work_items",
            "get_work_item", "search_work_items",
        }
        missing = sorted(required - by_name.keys())
        if missing:
            raise RuntimeError(f"Missing public functions: {missing}")
        mutation_schema = by_name["plane_mutation_action"].inputSchema
        redundant = {"approved_live_mutation", "authorization_receipt", "issue_readiness"}
        if redundant & set(mutation_schema.get("required", [])):
            raise RuntimeError("Mutation still requires redundant approval/preparation artifacts")
        result = await client.call_tool("plane_catalog", {"limit": 1})
        if result.is_error or not isinstance(result.data, dict):
            raise RuntimeError("Registry call failed")
        catalog = result.data
        descriptor = await client.call_tool("plane_action_descriptor", {
            "operation": "project__list_projects",
            "path_params": {"workspace_slug": "release-contract-probe"},
        })
        if descriptor.is_error:
            raise RuntimeError("Descriptor call failed")
        provider_ok = None
        if args.live_read:
            response = await client.call_tool("get_current_user", {})
            body = response.data
            provider_ok = not response.is_error and isinstance(body, dict) and bool(body.get("id"))
            if not provider_ok:
                raise RuntimeError("Authenticated read did not return a user identity")
        return {
            "status": "PASS", "transport": "stdio", "tool_count": len(tools),
            "tools": sorted(by_name),
            "mutation_required": mutation_schema.get("required", []),
            "registry": catalog.get("server"),
            "capability_contract_versions": catalog.get("capability_contract_versions"),
            "authenticated_read_verified": provider_ok,
            "live_mutations_executed": 0,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--launcher")
    parser.add_argument("--live-read", action="store_true")
    args = parser.parse_args()
    try:
        result = asyncio.run(verify(args))
    except Exception as error:
        # Do not echo error messages originating in a provider or credential loader.
        print(json.dumps({"status": "FAIL", "error_class": type(error).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
