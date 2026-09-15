"""Smoke exact configured MCP launch commands; no model turns or mutations."""
import asyncio
import json
from pathlib import Path
import tomllib
from urllib.request import Request, urlopen

import yaml
from fastmcp import Client


async def main():
    home = Path.home()
    hermes = yaml.safe_load((home / ".hermes/config.yaml").read_text())["mcp_servers"]["plane-mcp-karval"]
    codex = tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]["plane"]
    req = Request("http://127.0.0.1:43210/api/mcp/servers", headers={"Origin": "http://127.0.0.1:43210"})
    with urlopen(req, timeout=20) as response:
        od = next(s for s in json.load(response)["servers"] if s["id"] == "plane")
    rows = []
    for name, config in [("Hermes", hermes), ("Codex", codex), ("OpenDesign", od)]:
        assert config.get("enabled", True)
        command = {k: config[k] for k in ("command", "args", "env", "cwd") if k in config}
        async with Client({"mcpServers": {"plane": command}}) as client:
            tools = await client.list_tools()
            names = sorted(t.name for t in tools)
            assert {"plane_mutation_action", "plane_operator_lifecycle_transition",
                    "plane_add_comment", "plane_reconcile_mutation"} <= set(names)
            result = await client.call_tool("get_current_user", {})
            assert not result.is_error and result.data.get("id")
            catalog = await client.call_tool("plane_catalog", {"limit": 1})
            assert not catalog.is_error
        rows.append({"client": name, "status": "PASS", "tool_count": len(names), "tools": names,
                     "authenticated_read_verified": True, "command": command["command"],
                     "args": command.get("args", []), "proof": "exact configured stdio command",
                     "configuration_source": "live daemon API" if name == "OpenDesign" else "client configuration"})
    assert len(rows) == 3 and len({tuple(r["tools"]) for r in rows}) == 1
    destination = home / ".local/share/plane-mcp-karval/releases/0.3.1-final-20260914/clients-smoke.json"
    destination.write_text(json.dumps(rows, indent=2) + "\n")
    print(json.dumps([{k: v for k,v in r.items() if k not in ("tools", "args")} for r in rows]))


if __name__ == "__main__":
    asyncio.run(main())
