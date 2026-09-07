"""Command-line entry point for the Plane MCP compatibility server."""

from __future__ import annotations

import argparse

from plane_mcp_karval.server import create_server


def main() -> None:
    """Run the compatibility server over MCP stdio."""
    parser = argparse.ArgumentParser(prog="plane-mcp-karval")
    parser.add_argument("transport", choices=("stdio",))
    args = parser.parse_args()
    create_server().run(transport=args.transport)
