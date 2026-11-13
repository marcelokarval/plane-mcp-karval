"""Wire-level modern discovery and legacy compatibility; never contact Plane."""

import asyncio
import json
import sys

import pytest


MODERN = "2026-07-28"
LEGACY = "2025-11-25"
PUBLIC_TOOLS = {
    "plane_catalog", "plane_action_descriptor", "plane_read_action",
    "plane_mutation_action", "plane_add_comment", "plane_reconcile_mutation",
}
OFFLINE_TOOLS = {
    "plane_catalog", "plane_action_descriptor", "plane_normalize_title",
    "plane_validate_title_contract", "plane_validate_work_item_contract",
    "plane_render_lifecycle_comment", "plane_prepare_issue_creation",
}


@pytest.mark.parametrize("offline", [False, True], ids=["full-stdio", "http-offline-policy"])
@pytest.mark.parametrize("modern", [True, False], ids=["discover-2026", "initialize-legacy"])
def test_discovery_catalog_and_transport_boundary(offline, modern, tmp_path):
    """Exercise the exact HTTP server factory too, without exposing a live port."""
    async def exercise():
        factory = (
            "from plane_mcp_karval.transport_policy import create_offline_server"
            if offline else "from plane_mcp_karval.server import create_server"
        )
        name = "create_offline_server" if offline else "create_server"
        # These factories must remain lazy: all exercised tools are provider-free.
        program = (
            "import plane_mcp_karval.server as s; "
            "s._client = lambda: (_ for _ in ()).throw(AssertionError('provider used')); "
            f"{factory}; {name}().run(transport='stdio')"
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", program,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, cwd=tmp_path,
        )

        async def request(identifier, method, params=None):
            params = dict(params or {})
            if modern:
                params["_meta"] = {
                    "io.modelcontextprotocol/protocolVersion": MODERN,
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
            process.stdin.write((json.dumps(message) + "\n").encode())
            await process.stdin.drain()
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=20)
                if not line:
                    diagnostic = (await process.stderr.read()).decode()
                    pytest.fail(f"server exited before responding: {diagnostic}")
                response = json.loads(line)
                if response.get("id") == identifier:
                    return response

        try:
            if modern:
                discovered = await request(1, "server/discover")
                assert "error" not in discovered, discovered
                result = discovered["result"]
                assert MODERN in result["supportedVersions"]
                assert "tools" in result["capabilities"]
                assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "plane-mcp-karval"
            else:
                initialized = await request(1, "initialize", {
                    "protocolVersion": LEGACY, "capabilities": {},
                    "clientInfo": {"name": "offline-regression", "version": "1"},
                })
                assert initialized["result"]["protocolVersion"] == LEGACY
                process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
                await process.stdin.drain()

            listed = await request(2, "tools/list")
            names = {tool["name"] for tool in listed["result"]["tools"]}
            if offline:
                assert names == OFFLINE_TOOLS
                assert all(t["annotations"]["readOnlyHint"] for t in listed["result"]["tools"])
                denied = await request(3, "tools/call", {"name": "plane_read_action", "arguments": {}})
                assert "error" in denied or denied["result"].get("isError") is True
            else:
                assert PUBLIC_TOOLS <= names
            catalog = await request(4, "tools/call", {"name": "plane_catalog", "arguments": {"limit": 1}})
            assert "error" not in catalog, catalog
            assert not catalog["result"].get("isError", False), catalog
            assert catalog["result"]["content"]
        finally:
            if process.returncode is None:
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=5)

    asyncio.run(exercise())
