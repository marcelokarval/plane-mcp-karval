# Security and disclosure

Never post credentials, customer data, operational receipts or private environment details in an issue, PR, log or artifact. Use GitHub private vulnerability reporting if enabled; otherwise request a private reporting channel from a maintainer without disclosing sensitive details publicly. Do not assume that private vulnerability reporting is enabled.

The MCP is intended for trusted local stdio operation. Its HTTP transport is offline-only; publishing the repository does not authorize exposing a live provider connector to the Internet. Live tests require explicit owner scope and isolated test records.

An ambiguous mutation must not be retried. Preserve the ledger and use read-only reconciliation. A GET failure other than a typed target-specific 404 is not evidence of deletion.

Before making any repository public, audit all reachable history, branches, tags, PR references, release notes and package assets. Removing a file from the current tree is not historical erasure. Preserve recoverable private backups and do not claim zero risk from a scanner result alone.
