# Third-party provenance

The operation registry is derived from official Plane API documentation. Its immutable source commit, tree, file provenance and contract hashes are retained in `src/plane_api/source_contract.json` and `src/plane_api/operation_registry.json`.

The root rights notice applies only to material owned by Karval. It does not relicense Plane documentation or dependencies. Redistribution rights for the documentation-derived snapshot must be resolved before public distribution. This is a publication gate, not a claim of permission.

Runtime and build dependencies are declared in `pyproject.toml` and frozen in `uv.lock`; their upstream licenses remain applicable. No dependency license is replaced by the repository rights notice.
