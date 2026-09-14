# Plane skill-family audit — 0.3.2

Scope: local skill source review, not live Plane records or provider-state audit.
Twelve existing Plane skills were reviewed for ownership and stale MCP mechanics.
The new portable skill does not migrate the entire Hermes policy library.

| Existing skill | Retained responsibility | Disposition |
| --- | --- | --- |
| plane-api-tools | Native connector/API engineering | Shared MCP routed to plane-mcp-operations; legacy receipt/operator-only rules scoped to native connector |
| plane | Family routing and shared business policy | Shared MCP override added; ADMIT capability separated from policy |
| plane-completion-docs | Closure evidence | Retained; no copy into portable mechanics |
| plane-compliance-enforcer | Policy compliance | Retained; native descriptor restrictions do not define shared MCP availability |
| plane-governance | Workspace policy and authorization | Retained |
| plane-pm | Scope, acceptance, owner and planning | Retained |
| plane-progress-reporter | Evidence-based progress | Retained |
| plane-status-validator | State supported by evidence | Retained; tool availability and readiness are distinct |
| plane-task-intake-manager | Conversational intake and channel scope | Retained; Telegram policy is not a universal MCP transport restriction |
| plane-taxonomy-advisor | Type, grouping, scheduling and ownership semantics | Retained |
| plane-work-item-analyzer | Diagnose item readiness | Retained |
| plane-workflow-orchestrator | Dependencies and work sequencing | Retained |

## Concrete drift found

- `plane-api-tools` mixed shared MCP mutations with issued authorization receipts
  and operator-only state restrictions from the native connector.
- `plane` described `plane_operator_lifecycle_transition` as the only route and
  `plane_add_lifecycle_comment` as a compatibility tool that must fail closed.
- Its lifecycle, tool-boundary, family-map, workflow and retrospective references
  could reintroduce the same obsolete mechanics after loading the root skill.

Local root/reference scope notices now route shared MCP 0.3.1+ mechanics to
`plane-mcp-operations`; native connector rules are not silently changed. Business
readiness, explicit authorization, taxonomy and honest evidence remain intact.

## Reproduction and ownership

The MCP repository versions only the portable skill, installer and contract tests.
It does not vendor the twelve Hermes-specific skill trees or their private task
records. On another host, review its own Plane-family routing; do not assume the
local scope notices were distributed just because this portable skill was installed.

Versioned source: `skills/plane-mcp-operations/` in this repository.
Global deployment: `~/.agents/skills/plane-mcp-operations/`.
Install/check through `scripts/install_operating_skill.py`; SHA-256 parity is the
synchronization check. No same-name copy is needed in ~/.hermes/skills.
