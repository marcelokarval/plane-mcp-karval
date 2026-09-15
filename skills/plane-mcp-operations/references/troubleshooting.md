# Troubleshooting and installation

## Stale session

If a session says state PATCH is forbidden or requires `ctx.elicit`/an issued
approval receipt, inspect the currently connected MCP tools. The current shared
contract uses `plane_lifecycle_transition`, `plane_operator_lifecycle_transition`,
`plane_mutation_action`, `plane_add_comment` and `plane_reconcile_mutation`. Use
the operator tool only when the governing client requires its v3 target/approval
binding; its presence does not invalidate the simpler trusted-stdio routes. Reconnect the MCP in that
client; a server upgrade does not rewrite an existing session's cached catalog.
Do not substitute a Hermes-native connector's restrictions for the shared MCP's
capability. Do not bypass a real policy decision by selecting another transport.

## Errors

HTTP 401/403/500, timeouts and generic MCP errors never prove target absence.

- Authentication / HTTP 401 or 403: verify credential configuration and scope;
  never print tokens. This is not absence and does not authorize a retry of an
  ambiguous write.
- HTTP 404: confirm workspace/project/resource and route before calling it absence.
- Missing tool: compare client configuration and connected schema; do not invent
  aliases or fall back to direct HTTP writes.
- `precondition_failed`: refresh state and resolve the conflict.
- Applied state but missing comment/readback: report partial success; reconcile
  the exact step. Do not roll back automatically or replay the entire workflow.
- Schema drift: retain sanitized evidence, add a scoped regression and fix the
  affected response contract. Do not globally disable schema validation.

## Installation

The versioned source is `skills/plane-mcp-operations/` in the MCP repository.
The global installation is `~/.agents/skills/plane-mcp-operations/`.
Use the repository's `scripts/install_operating_skill.py --check` to preview;
`--apply` installs a copy, creates a private backup when replacing a prior copy,
and verifies SHA-256 parity. Do not edit the global copy independently.

Configure the host's skill discovery to include that exact directory. In Hermes,
use its supported `skills.external_dirs` configuration without dropping existing
entries. Other clients may discover `~/.agents/skills` natively; verify rather
than assuming. Do not create duplicate same-name skill copies under ~/.hermes.

Loading this skill does not install or upgrade the MCP itself. `stdio` is the
live local transport; the package's HTTP transport remains offline-only.
A wheel launcher must start in the canonical project directory to load its `.env`.
Caller-provided environment variables have precedence. Never source credentials
from unrelated legacy installations or include `.env` in published artifacts.

## Proof boundaries

Skill contract tests are deterministic, offline evidence, not a model benchmark.
A live read through one client's configured command proves that command, not a
model turn in every client. A release's catalog denominator is not a statement
that every endpoint was exercised against the provider.
