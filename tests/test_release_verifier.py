"""Keep the offline release gate compatible with SDK v2 without deprecated reads."""

import argparse
import asyncio
import warnings

from fastmcp import Client
from fastmcp.exceptions import FastMCPDeprecationWarning

from plane_mcp_karval.server import create_server
from scripts import verify_mcp_release


def test_release_verifier_uses_sdk_v2_schema_without_deprecation(monkeypatch):
    monkeypatch.setattr(verify_mcp_release, "Client", lambda config: Client(create_server()))
    args = argparse.Namespace(launcher=None, python="unused", live_read=False)
    with warnings.catch_warnings():
        warnings.simplefilter("error", FastMCPDeprecationWarning)
        result = asyncio.run(verify_mcp_release.verify(args))
    assert result["status"] == "PASS"
    assert result["authenticated_read_verified"] is None
    assert result["live_mutations_executed"] == 0
