# Release 0.3.5

- Freeze FastMCP 4.1.7 and MCP 2.3.0 for native `server/discover` support
  at protocol 2026-07-28 while retaining the legacy initialize handshake.
- Adapt the HTTP offline tool allowlist to the supported FastMCP provider APIs;
  do not expose provider reads, mutations or ledger reconciliation over HTTP.
- Exercise modern discovery, catalog calls, legacy compatibility and denied
  provider tools with offline wire-level regression tests.
- Keep the installed-release verifier on the SDK v2 `input_schema` API.
- Version the bundled operating skill with the package.

Source validation does not establish activation or OpenCode pooling. Promotion
requires an explicit source manifest, installed wheel/lock parity, offline smoke
and authorized client binding updates. Preserve credentials only in the canonical
working directory; never include `.env` or durable ledgers in release artifacts.

Any preexisting uncommitted provider-client changes present in a release source
snapshot must be identified by hash and independently accepted before activation.
No live Plane operation is required by this release's offline verification.
