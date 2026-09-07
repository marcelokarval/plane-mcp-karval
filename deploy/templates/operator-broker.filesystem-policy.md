# Operator broker filesystem policy (template; inactive)

Root deployment is required. This document does not create users, directories,
sockets, services, or permissions.

- Keep broker state under a root-owned directory writable only by the broker.
- Expose request and operator-control interfaces as separate protected sockets.
- Permit the request interface to prepare/execute only; permit confirmation only
  through a separately authenticated trusted operator-control interface.
- Do not place private keys, API tokens, or auto-approve material in this
  repository or environment variables.
- Review ownership, mode bits, MAC policy, rotation, backup, and audit retention
  before activation.
