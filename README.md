# plane-mcp-karval

Karval-owned Plane MCP server with full registry coverage.

This package is not a wrapper around `plane-mcp-server`.  It vendors the
reviewed Plane operation registry and uses a small stdio MCP surface over an
owned HTTP client.

## Coverage

- 225 documented Plane actions.
- 223 unique HTTP operations.
- 91 GET reads.
- 134 governed POST/PATCH/DELETE mutations.

The MCP intentionally exposes a compact tool surface:

- `plane_catalog` - discover the bounded operation registry.
- `plane_action_descriptor` - describe any registered action without calling Plane.
- `plane_read_action` - execute any registered GET action.
- `plane_validate_work_item_contract` - validate lifecycle/readiness artifacts
  without provider calls.
- `plane_render_lifecycle_comment` - render complete START/PROGRESS/BLOCKED/
  REVIEW/FINISH comments before governed comment writes.
- `plane_add_lifecycle_comment` - render and persist one governed lifecycle
  comment with exact work-item authorization, idempotency, and readback.
- `plane_validate_title_contract` and `plane_normalize_title` - enforce the
  canonical title/icon contract before create or title mutation.
- `plane_mutation_action` - execute any registered mutation only with explicit
  operation/target approval, idempotency, and readback.
- `plane_reconcile_legacy_module_archive_attempt` - append fresh, read-only
  evidence for one historical module-archive attempt whose original outcome is
  unknown. It is not a replay and has no caller-selectable ledger path.
- `get_current_user`, `list_projects`, `list_work_items`, `get_work_item`,
  `search_work_items` - high-frequency read shortcuts.

## Environment

Required:

- `PLANE_API_KEY` or `PLANE_API_TOKEN`
- `PLANE_BASE_URL` or `PLANE_API_URL`

Optional:

- `PLANE_WORKSPACE_SLUG`
- `PLANE_INSTANCE_ID`
- `PLANE_TIMEOUT`

Secrets are only used for provider calls and are never returned by catalog,
descriptor, or receipt payloads.

## Governed mutation and reconciliation semantics

Every generic live-mutation approval must include a unique `authorization_id`
and `key_hash` (SHA-256 of the idempotency key), in addition to the exact
method, path parameters, payload fingerprint, and operation. An authorization
is single-use; a subsequent idempotent lookup requires a fresh authorization
bound to the same key. This deliberately changes older callers that supplied
only an operation/target receipt.

The MCP server, not `PlaneClient`, owns runtime ledger selection. This Codex
server injects `~/.codex/state/plane-mcp-karval-mutations.json`; the Hermes
installed server injects its own `~/.hermes` ledger. There is no dual-read or
migration fallback. Each runtime must retain its historical ledger, and a
promotion must preserve that server's fixed default. Direct client mutation or
reconciliation calls therefore require an explicit `ledger_path` for tests or
another declared runtime authority.

Lifecycle-comment writes use the same fail-closed receipt shape: unique
`authorization_id`, key hash, provider-instance hash, exact target, and
rendered payload fingerprint. This is a deliberate public schema change;
callers without a receipt are rejected. Generic issue-detail payloads that
include `state` are also rejected here: a state transition requires the
runtime-specific governed state tool.

The reconciliation tool is restricted to legacy module archives. Its approval
also binds the provider-instance hash, the original method/path/fingerprint,
the burned key hash, and the current contract hash. It scans both archived and
active module lists exhaustively with bounded pagination; a cursor loop or
limit is inconclusive, never proof of absence. The immutable original receipt
continues to say `unknown`; a top-level append-only event records only the
current observation and safe evidence. A replacement archive operation needs a
fresh mutation authorization and a new idempotency key.

If a consumed reconciliation approval cannot establish provider state (network,
schema, membership, cursor, or bound-limit failure), the server appends an
`inconclusive` terminal event with safe error classification. It never
authorizes a replacement in that result. A later fresh approval may prove the
current state and references earlier consumed approvals that lacked a terminal
event. Module membership proof keeps the collection envelope and cursors strict
while tolerating nullable optional module description/date fields observed from
the provider.

## Run

```sh
uv run --directory /path/to/project \
  plane-mcp-karval stdio
```

## Tests

```sh
uv run --directory /path/to/project \
  python -m pytest
```

## MCP smoke

```sh
uv run --directory /path/to/project python - <<'PY'
import asyncio

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def check():
    transport = StdioTransport(
        "uv",
        [
            "run",
            "--directory",
            "/path/to/project",
            "plane-mcp-karval",
            "stdio",
        ],
    )
    async with Client(transport) as client:
        tools = await client.list_tools()
        print("MCP OK:", len(tools), "tools")
        print(", ".join(tool.name for tool in tools))


asyncio.run(check())
PY
```
