"""Command-line entry point for the Plane MCP compatibility server."""

from __future__ import annotations

import argparse
from pathlib import Path

from dotenv import load_dotenv

from plane_mcp_karval.server import create_server
from plane_mcp_karval.transport_policy import create_offline_server


LOOPBACK_HOSTS = ("127.0.0.1", "::1")
DEFAULT_HTTP_HOST = "127.0.0.1"
DEFAULT_HTTP_PORT = 8000
PROJECT_ENV = Path(__file__).resolve().parents[2] / ".env"


def _load_project_env() -> Path | None:
    """Load the canonical checkout's local secrets without overriding its caller."""
    candidates = dict.fromkeys((PROJECT_ENV, Path.cwd() / ".env"))
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            return candidate
    return None


def _port(value: str) -> int:
    """Parse a valid TCP port for the HTTP transport."""
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def main() -> None:
    """Run the compatibility server over MCP stdio or Streamable HTTP."""
    parser = argparse.ArgumentParser(prog="plane-mcp-karval")
    parser.add_argument(
        "transport",
        choices=("stdio", "streamable-http"),
        help="stdio uses the full server; streamable-http is offline-only with no provider access.",
    )
    parser.add_argument("--host", choices=LOOPBACK_HOSTS)
    parser.add_argument("--port", type=_port)
    args = parser.parse_args()

    if args.transport == "stdio":
        if args.host is not None or args.port is not None:
            parser.error("--host and --port are only valid with streamable-http")
        _load_project_env()
        create_server().run(transport="stdio")
        return

    create_offline_server().run(
        transport="streamable-http",
        host=args.host if args.host is not None else DEFAULT_HTTP_HOST,
        port=args.port if args.port is not None else DEFAULT_HTTP_PORT,
    )
