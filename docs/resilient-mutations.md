# Native mutation recovery — candidate executor v2

Repository-only candidate. No production activation, provider smoke, package
installation or historical-ledger rewrite is implied by this document.

## Identity and compatibility

The existing hashed idempotency key and request fingerprint are preserved. A
new operation ID binds provider-instance hash + key hash + request fingerprint
(including path parameters, query and caller payload). Caller payload remains
the binding input; generated correlation fields do not change old key semantics.
`attempts=1` means one logical operation invocation, not a guarantee that only
one TCP connection is attempted. Existing public mutation/reconcile arguments
remain valid. No operation registry/contract hash changes are required.

New issue/comment creates receive a deterministic `external_id` when omitted or
null, plus `external_source=plane-mcp-karval` when omitted. Explicit IDs/sources
are preserved. Recovery compares the exact ID and, when present, exact source at
the registry-scoped project/issue collection endpoint. Workspace UUIDs in provider
rows are not compared to a workspace slug. Any returned project/issue bindings
must agree with the requested target.

Legacy receipts are not upgraded or classified as not-sent. Ordinary old executor
semantics remain unless a bound native successor exists. An old `attempt_started`
with matching provider/fingerprint/key and caller-supplied explicit ID **and source**
can first receive read-only recovery in a separate `native_recoveries` section;
its original receipt remains unchanged. Explicit successor authorization is
separate from recovery and is described below.
Uncorrelated historical UNKNOWN cannot be automatically repaired.

## Delivery and retries

Only pre-response `EAI_AGAIN` DNS failures and `ECONNREFUSED` connection failures
are treated as proven not-sent, and only on the no-redirect native urllib path or
explicit test fakes. Arbitrary injected production transports are conservative:
they cannot establish that a DNS error happened before an internally redirected
write. Native mutations do not follow HTTP redirects. Errors while reading or
decoding an acquired response are always uncertain. Timeout, reset, TLS errors,
HTTP 429/5xx and opaque transport errors never trigger automatic write replay.

One request has at most three transport attempts and a two-second retry-wait
budget (provider timeout applies separately to each attempt). Exponential delay
with bounded jitter applies only to not-sent writes and retryable GET errors.
Ordinary registry reads, native readback and correlation recovery retry network
errors and HTTP 429/502/503/504; validation/permission failures are not retried.
Numeric or HTTP-date `Retry-After` becomes safe numeric metadata, never raw headers.
A delay exceeding the wait budget returns pending/error rather than sleeping
unboundedly or retrying early. Native calls sharing one ledger share provider-hash
cooldowns following HTTP 429, including across different operation keys. Ordinary
reads have their own bounded waits but no cross-process ledger cooldown: their
read API deliberately has no ledger argument. This is reactive throttling, not a
proactive global token bucket or account-wide quota service.

## Ownership and crash boundaries

The existing locked/fsynced JSON ledger is the storage authority; no database or
new daemon is introduced. Reservation, send boundary, acknowledgment and validation
are separate durable transitions. Locks cover state only, never provider I/O.
Each operation has an owner token, incrementing fence and bounded lease. Every
post-I/O transition checks owner/fence and a live lease. Concurrent same-key
requests return `in_progress` rather than launching a second POST.

An expired `reserved` or `sending` lease enters read-only recovery, never an
automatic POST. A crash before the durable send marker is still held for recovery
rather than guessed safe. A response acknowledged before validation is stored as
`applied_pending_readback` with `ack_validated=false`; recovery cannot turn that
unvalidated ACK into success merely by trusting its metadata. Failed readback of
a validated ACK may be repeated, but the acknowledged write is never resent.
A stale process cannot overwrite the replacement owner's receipt.

## Recovery inventory

`resilience.recovery_policy(action)` is a total inventory over the registry;
regression tests enumerate every mutation rather than maintaining a stale list.

| Operations | Uncertain outcome policy |
| --- | --- |
| `issue__add_issue`, `issue_comment__add_issue_comment` | Complete exact correlation lookup; no automatic absent-create replay |
| Every other registered POST, including memberships/archive/relation changes | Readback only after validated ACK; unacknowledged outcome stays recovery-required |
| Every PATCH | Never assume generic idempotence; validated-ACK desired-state readback only |
| Every DELETE | Validated-ACK absence proof via existing contract; no uncertain-write replay |
| Registry GET | Bounded retry, no mutation |

Correlation scans require dictionary cursor envelopes and explicit boolean
`next_page_results` on every page. They reject unproven terminal cursors,
repeated row IDs, cursor cycles, and inconsistent or mismatched `total_results`,
`total_count`, and `total_pages` declarations across the entire scan. `count`,
when supplied, must match that page's row count; booleans are not numeric totals.
A read-only provider check confirmed Plane returns a computed nonempty cursor
(such as `100:1:0`) even on a terminal page. With an explicit false flag, this
is accepted only when both global totals and the total page count are supplied
and agree with the fully collected rows/pages. Cursor presence alone is neither
proof of another page nor proof of completion. Synthetic public-tool fixtures
also retain the observed comment shape: actor UUID string, no `name` field;
existing action-scoped response compatibility preserves identity validation.
Page/row budgets remain bounded. All pages are read even after a
match is found so a second match cannot be hidden. Zero matches, incomplete or
failed reads remain pending. Multiple stable IDs produce `correlation_conflict`.
Recovery evidence records completeness, pages, rows, match count, timestamp and
safe failure code. Correlation proves operation attribution/resource existence,
not every desired field. Scalar effects and set-like label/assignee IDs are
checked separately. HTML comparison reports only normalized text equivalence,
never full HTML equivalence; unknown effects are `not_evaluated`.

## Explicit operator resolution

The local-only `plane_authorize_mutation_reattempt` tool writes no provider data.
It is excluded from the offline HTTP allowlist. It requires the exact operation,
provider/request/key binding, an inactive lease, a fresh (within five minutes)
complete zero-match correlation scan without errors, explicit
`operator_acknowledged_duplicate_risk=true`, and a bounded meaningful reason.
Native v2 correlated-create receipts qualify. A legacy `attempt_started` receipt
may qualify only through a separately stored `native_recoveries` successor:
reconciliation requires the original caller's exact explicit external ID/source,
request fingerprint, provider and original key binding. The historical receipt is
never changed. A recovered completion is returned on ordinary repeats without a
POST; an absent successor requires the same fresh scan and explicit risk decision
before ordinary execute may consume its one-shot authorization. Old
unbound/uncorrelated receipts and generic PATCH/DELETE operations cannot be unlocked.

This is a deliberate operator risk decision: absence is NOT proof of non-delivery.
The original ambiguous write diagnostics are retained append-only in
`uncertain_write_attempts` (at most nine uncertain writes under the eight-authorization
cap), including safe delivery/phase/timestamp and active authorization linkage.
A `durable_send_attempt` marker is persisted before dispatch. An expired `sending`
lease archives that marker as unknown delivery even when the process died without
catching an exception. Expired `reserved` receipts instead record the distinct
before-send reservation boundary. Neither marker proves delivery to the provider.
Recovery GET diagnostics remain a 32-entry ring; they cannot erase this immutable
write proof. Native GETs honor finite `Retry-After` for retryable 503 responses as
well as 429; waits outside the budget stop rather than retry early.
Authorization snapshots preserve both in `uncertain_attempts`;
a linked one-shot authorization stores a reason SHA-256, acknowledgment and the
absence evidence. The next same-key mutation consumes that authorization under
the ledger lock before issuing a linked retry with the same stable correlation.
Read-only reconciliation neither sends nor consumes authorization. Completed
repeats return the durable completion receipt with zero provider requests.
At most eight explicit reattempt authorizations may be appended per operation.

## Statuses and diagnostic limits

- `not_sent_retryable`: resume the identical key/request; no ambiguous replay.
- `in_progress`: another owner holds the lease; wait.
- `outcome_unknown` / `recovery_required`: perform read-only recovery.
- `correlation_conflict`: inspect conflicting remote identities; do not resend.
- `applied_pending_readback` / `write_succeeded_readback_validation_failed`:
  acknowledged write, incomplete proof; never resend.
- `rejected_not_applied`: existing deterministic rejection contract; no guessed absence.
- `authorized_retry`: explicit operator authorization pending same-key execution.
- `verified`: stored proof of completion, not a claim of fresh current provider state.

Attempt diagnostics retain the newest 32 sanitized timestamp/phase/errno/HTTP
metadata entries. No transport exception text, token, raw response, request body,
URL or caller reason is persisted in attempt history. Existing receipt fields
and rejection sanitization retain their legacy contracts. Request fingerprints,
reason fingerprints and provider hashes are not reversible raw payload logs.

## Proof scope and remaining limits

The regression suite uses deterministic fake providers plus loopback HTTP redirect
and stdio tests; it is not live Plane evidence. No exactly-once provider guarantee,
server-side idempotency, atomic PATCH, atomic lifecycle transaction, cross-ledger
rate-limit coordination or proactive account-wide throttling is claimed. Shared
storage requires all cooperating processes to use the same local ledger on a
filesystem with working flock/fsync semantics. Exhausted retries or missing
provider evidence remain actionable pending states, not fabricated success.
