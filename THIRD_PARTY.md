# Third-party provenance

The operation registry is derived from official Plane API documentation. Its immutable source commit, tree, file provenance and contract hashes are retained in `src/plane_api/source_contract.json` and `src/plane_api/operation_registry.json`.

The root MIT license applies to material owned by Karval; it does not relicense
Plane documentation or dependencies.

The source is `makeplane/developer-docs` at commit
`e5dc6310b80a3a9c8376b9b799319103cc18e28c`. Its root `package.json` declares
`Apache-2.0`. The source tree has no separate LICENSE or NOTICE file.
Source declaration:
https://github.com/makeplane/developer-docs/blob/e5dc6310b80a3a9c8376b9b799319103cc18e28c/package.json

The registry and provenance contract adapt that documentation into operation
schemas, validation rules and contract hashes; they are not unmodified copies.
Documentation-derived material retains Apache-2.0 terms. This project is not
an official Plane product and grants no rights to Plane trademarks.

The full Apache-2.0 license is included at `licenses/Apache-2.0.txt` in the
source and `plane_api/licenses/Apache-2.0.txt` in the wheel, together with this
provenance notice. The root package license field alone is not a complete
third-party license inventory.

Runtime and build dependencies are declared in `pyproject.toml` and frozen in `uv.lock`; their upstream licenses remain applicable. No dependency license is replaced by the repository rights notice.
