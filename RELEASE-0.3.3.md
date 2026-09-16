# Plane MCP Karval 0.3.3

## Delivered

- Promotes `plane_operator_lifecycle_transition` as the versioned operator
  lifecycle v3 contract.
- Retains the portable `plane-mcp-operations` skill and the governed
  stdio-only Plane MCP surface.

## Release identity

- Source commit: `df88c2e1c65dd954afff767a5b83697b7b1c68d9` plus this
  release-metadata commit.
- Release tag: `v0.3.3`.
- Distribution artifacts are built from the tagged release commit and their
  SHA-256 values are recorded at cutover.

## Verification

- `uv lock --check`
- `git diff --check`
- full `pytest -q` suite
- clean wheel build and installed stdio release verification

Publication does not itself reconnect MCP clients or mutate Plane records.