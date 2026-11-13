# Bounded HTML readback contract

Issue detail effect verification accepts exact typed equality, or, for an
explicitly enabled HTML field, exactly one additional plain outer `<div>` and
`</div>` around the unchanged submitted string. This is not HTML sanitization or
semantic equivalence. Links, attributes, entities, whitespace and interior bytes
must remain unchanged; recursive or symmetric unwrapping is not performed.

Issue creation uses the provider-issued ID and registry-bound detail GET. Its
original membership contract declares no payload effects; the existing detail
proof therefore explicitly selects every caller-supplied effect field, excluding
only unset scheduling values omitted during request preparation. Missing or
changed selected fields fail proof. Generated correlation stays separate.
The registry itself and other membership contracts are not rewritten.

Issue PATCH, issue-create detail proof and legacy issue/comment helpers use the
shared comparator. Other generic detail contracts default to no HTML tolerance;
module PATCH does not opt in. Native comment-create membership/correlation proof
does not claim HTML-content validation. Diagnostic `text_equivalent` is not
`desired_state_verified`.

Caller payloads, request fingerprints and idempotency keys are not normalized.
An acknowledged mutation with failed effect proof can be reconciled by reads,
never by replaying its write. Verified repeats return stored completion evidence,
not a fresh observation.

Regression coverage includes the real native create/PATCH paths, changed or
missing HTML, changed links/attributes/text, other submitted effect fields,
identity, scalar types, unchanged bindings and GET-only failed-proof recovery.
Tests use fake providers and must run in the repository's isolated offline test
environment. Local source tests do not establish installed-runtime adoption.
