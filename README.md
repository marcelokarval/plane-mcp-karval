# plane-mcp-karval

## Operação rápida

### Skill operacional — 0.3.2

A versão 0.3.2 adiciona `skills/plane-mcp-operations/`, também incluída no wheel.
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

Projeto canônico: `/home/marcelo-karval/Backup/Projetos/mcps/plane-mcp-karval`.
Instale o ambiente de desenvolvimento com `uv sync --frozen`.
Guarde as credenciais somente no `.env` local, ignorado pelo Git, modo `0600`.
Execute `uv run --frozen plane-mcp-karval stdio` a partir deste diretório.

### Instalação operacional 0.3.1

- Release: `~/.local/share/plane-mcp-karval/releases/0.3.1-final-20260914/`.
- Python instalado: `~/.local/share/plane-mcp-karval/venvs/0.3.1-final-20260914/bin/python`.
- Entrada compartilhada: `launch.sh` dentro da release; inicia no diretório
  canônico para carregar `.env`, sem importar código do checkout via PYTHONPATH.
- Hermes: `mcp_servers.plane-mcp-karval` em `~/.hermes/config.yaml`.
- Codex: `mcp_servers.plane` em `~/.codex/config.toml`.
- OpenDesign: servidor `plane` em `~/.open-design/runtime-data/mcp-config.json`.
- Codex/OpenDesign usam `~/.codex/bin/plane-mcp-karval-launcher.sh`, agora um
  link para a mesma entrada da release. A instalação antiga não foi apagada.
- Sessões já abertas podem manter ferramentas antigas em memória; reconecte o
  MCP ou abra nova sessão. Não é necessário reiniciar serviços indiscriminadamente.

### Verificação e recuperação

`uv run --frozen python scripts/verify_configured_clients.py` testa os comandos
exatos dos três clientes com descoberta e leitura autenticada. A configuração
OpenDesign é lida da API do daemon. Não executa turnos de modelo nem mutações.

O diretório da release guarda `clients-smoke.json`, `canary-reconciled.json`
e o relatório final. Cobertura de catálogo não significa teste live de todas
as operações. O canário cobre criação, estado, comentário e exclusão.

Backup privado: `~/.local/share/plane-mcp-karval/backups/20260914-final/`.
`manifest.json` mapeia cada arquivo original para sua cópia e SHA-256.
Para reverter, restaure os arquivos afetados conforme o manifesto, removendo
primeiro o link do launcher Codex (não escreva através do link). Use
`hermes config` para restaurar as chaves MCP do Hermes ou restaure seu arquivo
de configuração com o cliente parado. Não restaure configurações inteiras
se outras alterações ocorreram depois: nesse caso reverta apenas o bloco Plane.
Nunca reverta ou apague o ledger de mutações. Rollback restaura a instalação
anterior, inclusive seus defeitos conhecidos; não desfaz dados do Plane.

Antes de atualizar: testes, wheel novo, ambiente limpo com dependências do
`uv.lock`, hash, backup e smoke dos comandos configurados. Não promover wheel
anterior a uma correção, não usar overlays nem branches móveis como release.

Karval-owned Plane MCP server with full registry coverage.

This package is not a wrapper around `plane-mcp-server`.  It vendors the
reviewed Plane operation registry and uses a small stdio MCP surface over an
owned HTTP client.

## Coverage

- 225 documented Plane actions.
- 223 unique HTTP operations.
- 91 GET reads.
- 134 registered POST/PATCH/DELETE mutations. Catalog coverage is not a claim
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

The client validates registry-bound data and requires exactly one write attempt.
It persists a reservation before provider I/O and does not blindly replay an
ambiguous write. Reusing a key with different request data is rejected.
Readback distinguishes an applied operation from a verified effect; provider
acknowledgement-only contracts are not represented as stronger proof.

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
`plane_reconcile_mutation` never replays a write: it only refreshes a failed
readback. An inconclusive observation remains inconclusive.

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
freezes FastMCP at 2.14.7. No container is required. Installation alone does not
migrate credentials, launchers or runtime state.

```sh
uv run --directory /home/marcelo-karval/Backup/Projetos/mcps/plane-mcp-karval \
  plane-mcp-karval stdio
```

## Tests

```sh
uv run --directory /home/marcelo-karval/Backup/Projetos/mcps/plane-mcp-karval \
  python -m pytest
```

## MCP smoke

```sh
uv run --directory /home/marcelo-karval/Backup/Projetos/mcps/plane-mcp-karval python - <<'PY'
import asyncio

from fastmcp import Client
from fastmcp.client.transports import StdioTransport


async def check():
    transport = StdioTransport(
        "uv",
        [
            "run",
            "--directory",
            "/home/marcelo-karval/Backup/Projetos/mcps/plane-mcp-karval",
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
