"""Read-only reconciliation of an existing successful MCP mutation canary.

Never repeats a mutation. Only a typed provider HTTP 404 proves detail absence;
transport failures, authentication errors and generic MCP errors do not.
"""
import argparse
import hashlib
import json
from pathlib import Path

from plane_api.client import PlaneProviderRequestError
from plane_mcp_karval.cli import _load_project_env
from plane_mcp_karval.server import _client


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    assert args.source.resolve() != args.output.resolve(), "Preserve original evidence"
    original = args.source.read_bytes()
    evidence = json.loads(original)
    for step in ("create", "state_patch", "comment", "delete"):
        assert evidence[step]["mutation_applied"] is True
        assert evidence[step]["readback_verified"] is True
    _load_project_env()
    try:
        _client().get_work_item(
            evidence["workspace_slug"], evidence["project_id"],
            evidence["create"]["provider_id"],
        )
    except PlaneProviderRequestError as error:
        if error.http_status != 404:
            raise SystemExit("FAIL: absence not proven by HTTP 404") from None
    else:
        raise SystemExit("FAIL: target still exists")
    result = {
        "status": "PASS", "original_status": evidence.get("status"),
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "source": str(args.source), "mutation_steps_verified": 4,
        "absence_http_status": 404, "absence_readback_verified": True,
        "verification_path": "supplemental GET through canonical PlaneClient",
        "live_mutations_executed": 0,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
