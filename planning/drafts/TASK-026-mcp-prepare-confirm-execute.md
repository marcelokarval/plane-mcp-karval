# TASK-026 — MCP prepare/confirm/execute compatibility draft

> **Local draft only.** This file is not a Plane lifecycle record and does not
> claim that any Plane event occurred.

## Compatibility contract

1. **Prepare** validates the requested operation, exact target, payload
   fingerprint, idempotency key, and authorization context without performing
   the mutation.
2. **Explicit confirm** requires a caller-supplied, single-use authorization
   bound to the prepared operation, target, payload fingerprint, and key hash.
3. **Execute** performs the governed mutation only after the explicit confirm,
   then records a safe receipt and performs the required readback. Replays must
   use the idempotency contract and never silently bypass confirmation.

Compatibility work should preserve fail-closed behavior, runtime-owned ledger
selection, secret redaction, and clear terminal outcomes for provider failures.

Any later Plane registration is historical evidence only; it must reference
observable records and must not invent lifecycle events, timestamps, or
outcomes.
