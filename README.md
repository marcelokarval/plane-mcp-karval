# plane-mcp-karval

## Operação rápida

### Skill operacional — 0.3.5

Roadmap da linha atual e das próximas versões: [ROADMAP.md](ROADMAP.md).

A versão 0.3.4 adiciona Inbox Issues, relações entre work items, readback de
mapas de relação e gates de paridade/release. Ela preserva a transição de
lifecycle v3 para operadores e
`skills/plane-mcp-operations/`, também incluída no wheel.
Ela documenta o MCP atual: descoberta, leitura fresca, mutações, ADMIT/estado,
comentários, idempotência, readback e recuperação, sem impor gates do conector legado.
Não altera o protocolo de operações da 0.3.1.

Instalação global, a partir deste repositório:

```sh
python scripts/install_operating_skill.py --check
python scripts/install_operating_skill.py --apply
```

Destino: `~/.agents/skills/plane-mcp-operations/`. A fonte versionada está no
repositório; o instalador verifica hashes e guarda backup se substituir uma cópia.
Configure a descoberta do cliente para esse diretório e carregue
`plane-mcp-operations`. No Hermes, inclua-o em `skills.external_dirs` preservando
as entradas existentes; não duplique a skill em `~/.hermes/skills`.
O teste `tests/test_operating_skill.py` confere estrutura, instalação e contrato
das ferramentas offline; não é um benchmark de comportamento de modelos.
O relatório das 12 skills Plane revisadas está em
`skills/plane-mcp-operations/references/skill-family-audit.md`.

MCP local stdio para Plane: catálogo completo, leitura e escrita com alvo,
payload, idempotência e readback. Sem confirmação MCP redundante, sem overlay
e sem dependência do runtime Hermes para executar uma operação.

## Contribuição e direitos

Issue obrigatória antes da implementação; toda entrega termina em PR. Leia [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [LICENSE](LICENSE) e [THIRD_PARTY.md](THIRD_PARTY.md). Publicação permanece bloqueada até saneamento do histórico e resolução dos direitos de terceiros.

Use `uv sync --frozen` e execute a partir do checkout. Credenciais ficam somente no `.env` local ignorado, modo `0600`. Instalações e backups particulares não pertencem à documentação distribuída.

Karval-owned Plane MCP server with full registry coverage.

This package is not a wrapper around `plane-mcp-server`.  It vendors the
reviewed Plane operation registry and uses a small stdio MCP surface over an
owned HTTP client.

## Coverage

- 233 documented Plane actions.
- 231 unique HTTP operations.
- 94 GET reads.
- 139 registered POST/PATCH/DELETE mutations. Catalog coverage is not a claim
  that every operation has been exercised against a production Plane instance.

The MCP intentionally exposes a compact tool surface:

- `plane_catalog` - discover the bounded operation registry.
- `plane_action_descriptor` - describe any registered action without calling Plane.
- `plane_read_action` - execute any registered GET action.
- `plane_capture_state_catalog` - read a project's current states without
  promoting or changing them.
- `plane_validate_work_item_contract` - validate lifecycle/readiness artifacts
  without provider calls.
- `plane_render_lifecycle_comment` - render complete START/PROGRESS/BLOCKED/
  REVIEW/FINISH comments before governed comment writes.
- `plane_add_lifecycle_comment` - render and persist a lifecycle comment with
  explicit target, idempotency, and readback.
- `plane_add_comment` - append an ordinary HTML comment without requiring a
  lifecycle packet.
- `plane_lifecycle_transition` - optional state/comment convenience workflow
  with before-read, per-step results, and no automatic compensation.
- `plane_operator_lifecycle_transition` - contract-v3 state transition or
  annotation bound to explicit caller approval, exact target, fresh state/revision,
  canonical phase roles, one attempt, server receipt, and partial-outcome reporting.
- `plane_validate_title_contract` and `plane_normalize_title` - enforce the
  canonical title/icon contract before create or title mutation.
- `plane_mutation_action` - execute a registered mutation for the trusted local
  MCP client with an explicit operation/target, idempotency, and readback.
- `plane_reconcile_mutation` - refresh a recorded write's readback without
  replaying its provider write.
- `plane_reconcile_legacy_module_archive_attempt` - append fresh, read-only
  evidence for one historical module-archive attempt whose original outcome is
  unknown. It is not a replay and has no caller-selectable ledger path.
- `get_current_user`, `list_projects`, `list_work_items`, `get_work_item`,
  `search_work_items` - high-frequency read shortcuts.

## Environment

Required:

- `PLANE_API_KEY` or `PLANE_API_TOKEN`
- `PLANE_BASE_URL` or `PLANE_API_URL`

For the canonical checkout, keep these values in the untracked `.env` beside
this README. The `stdio` CLI loads that file automatically without overriding
variables already supplied by its caller. An installed wheel also loads `.env`
from its working directory, so its launcher must start in the canonical project
directory. Never copy `.env` into a wheel, release artifact, or Git commit.

Optional:

- `PLANE_WORKSPACE_SLUG`
- `PLANE_INSTANCE_ID`
- `PLANE_TIMEOUT`
- `PLANE_MUTATION_LEDGER` - optional explicit durable ledger path.
- `XDG_STATE_HOME` - neutral default state root for new installations.

Secrets are only used for provider calls and are never returned by catalog,
descriptor, or receipt payloads.

## Local MCP mutation contract (0.3.1)

### Prepared operation integrity (source candidate)

For every submitted `description_html` or `comment_html` field, including empty,
null, nested, or invalidly typed values, use
`plane_mutation_action` with `prepared_file` and `prepared_sha256`. Create a
UTF-8 JSON file in `.prepared-mutations/` under the MCP process working
directory, with exactly `operation`, `path_params`, `query`, `payload`, and
`idempotency_key`. Supply the basename (letters, digits, `_`, `-`, `.json`)
and the lowercase SHA-256 of the exact file bytes. Maximum size is 8 MiB.
Pass matching operation, path, query and key to the tool; omit payload to avoid
transcribing it. If supplied, inline payload must match. A digest proves byte
integrity, not authorization or correctness. Protect the prepared directory
and delete sensitive files according to your own retention policy.

Comment and lifecycle tools also accept `prepared_file` and `prepared_sha256`;
their JSON envelope uses the same five fields, with `operation` equal to the tool
name and `query` null. `payload` contains exactly `comment` (lifecycle tools) or
`comment_html` (ordinary comment). `path_params` binds:

- `plane_add_comment` and `plane_add_lifecycle_comment`: `workspace_slug`,
  `project_id`, `work_item_id`, `external_source`, `external_id`.
- `plane_lifecycle_transition`: `workspace_slug`, `project_id`, `work_item_id`,
  `target_state_id`, `expected_state_id`, `expected_updated_at` (use null for
  omitted optional fields). A state-only call needs no file.
- `plane_operator_lifecycle_transition`: `workspace_slug`, `project_id`,
  `work_item_id`, `expected_current_state_id`, `expected_updated_at`,
  `target_state_id`. Supply approval and contract fields separately as usual.

Inline comment can be omitted or must match the prepared value. A lifecycle
transition comprises separate operations and must not be represented as atomic;
generic mutations do not reproduce v3 authorization, preconditions or receipts.
Historical read-only
reconciliation remains available. This source contract does not establish that
an installed server or another client has adopted it. Direct external Plane APIs
and other MCP servers are outside this enforcement boundary.

The trusted local MCP client supplies the operation, path parameters, payload
and an idempotency key. The server does not ask for `ctx.elicit`, a second
human confirmation, `approved_live_mutation`, a fabricated approval receipt,
or a planning artifact before an ordinary API mutation. The existing planning,
title and lifecycle helper tools remain useful independently.

This is a local stdio trust model, not a human-consent verification service.
The host/client must restrict which principals can launch it with Plane
credentials and enforce the operator's requested scope. Stdio alone does not
authenticate arbitrary remote callers. No same-process plugin isolation or
Hermes gateway dependency is claimed. Do not expose live tools through an
unauthenticated network transport.

The client validates registry-bound data and requires `attempts=1`: one logical
operation invocation with at most three bounded safe transport attempts. New
native receipts have stable provider/request-bound operation identities, durable
leases/fences and sanitized history. Proven pre-send transient DNS/refusal may
resume under the same key; uncertain writes are never automatically replayed.
Readback distinguishes applied writes from verified effects. Completed repeats
return stored completion evidence, not fresh current Plane state.

[Native resilient mutation/recovery design](docs/resilient-mutations.md) documents
status handling, correlation, throttling, legacy preservation and crash boundaries.
Issue/comment creates receive stable external correlation when absent. Read-only
reconciliation performs complete bounded exact lookup; missing/incomplete evidence
cannot authorize a resend. `plane_authorize_mutation_reattempt` is a separate
local-only explicit operator duplicate-risk decision after fresh complete absent
correlation; it preserves the original ambiguous attempt and grants one linked
same-key retry. It is never available over the offline HTTP transport.

An issue update may include `state`. Plane PATCH has ordinary non-atomic
semantics: a preceding GET is not compare-and-set, and the server does not
claim atomic state/comment transactions or perform automatic compensation.

The default ledger is `$XDG_STATE_HOME/plane-mcp-karval/mutations.json`
(default `~/.local/state`). On first use it merges compatible legacy Hermes and
Codex ledgers into that canonical location. Conflicting historical entries are
recorded and only the conflicting idempotency key is blocked; unrelated calls
continue normally. Set `PLANE_MUTATION_LEDGER` only when an operator deliberately
needs a different persistent location. Never delete, reset, or replace a ledger
with an empty file to make a retry succeed. A rollback of code must retain writes
and receipts produced after deployment; it is not a rollback of Plane data.

Legacy client APIs may retain their old receipt contracts for compatibility;
they do not make the simple public MCP interface manufacture authorizations.
`plane_reconcile_mutation` never replays a write: it refreshes validated-ACK
readback or attempts exact read-only create correlation recovery. An inconclusive
observation remains inconclusive.

## Request preparation and validation

For `issue__add_issue` only, unset (`null`) `start_date`, `target_date` and
`estimate_point` are omitted from the provider body. Other fields and PATCH
operations retain strict schema validation; null must not be stripped from an
update because it can mean an explicit clear. Original caller payloads remain
unchanged and remain the input to the durable request fingerprint, so changing
nulls to omitted fields under an already-used key does not bypass collision checks.

Type errors identify request/response context, nested field path, expected type
and received type, without printing supplied values. A preflight validation
failure occurs before the provider write and before ledger reservation; it must
not be described as a lost POST response.

## Run

### Candidate HTTP transport (offline only)

The candidate CLI supports `plane-mcp-karval streamable-http` on
`127.0.0.1:8000`, with optional `--host 127.0.0.1|::1` and `--port 1..65535`.
HTTP exposes only an explicit allowlist of offline catalog, descriptor,
normalization, readiness and validation tools. Private provider reads,
mutations and ledger reconciliation are not exposed. Unknown future tools
are excluded even when their annotations claim read-only behavior.

This is a development transport, not a deployed authenticated Plane service.
Do not put a public proxy in front of it or enable live tools without a
separately verified authenticated transport. It is not a prerequisite for
using the operational stdio MCP.

The `stdio` transport invocation is unchanged; the 0.3.1 public mutation schema
is intentionally simplified, so clients must refresh their tool catalog.
HTTP-specific arguments are rejected for stdio. The checked-in `uv.lock`
freezes FastMCP at 4.1.7 and MCP at 2.3.0. These upstream releases implement
`server/discover` (2026-07-28) and retain the legacy `initialize` handshake.
Do not patch protocol-version constants in an installed SDK. The offline HTTP
allowlist is unchanged. OpenCode pooling additionally requires a verified client
configuration (`pool: true`) and a launcher with a fixed canonical working
directory; a source update alone does not prove pooling in an existing session.
No container is required. Installation alone does not
migrate credentials, launchers or runtime state.

```sh
uv run --directory . \
  plane-mcp-karval stdio
```

## Tests

```sh
uv run --directory . \
  python -m pytest
```

## MCP smoke

```sh
uv run --directory . python - <<'PY'
import asyncio

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def check():
    transport = StdioTransport(
        "uv",
        [
            "run",
            "--directory",
            ".",
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
