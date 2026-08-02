# make-magic-pipeline

The Python data layer + pipeline behind the **make-magic** Claude Code plugin: a
DuckDB "medallion" lake over Scryfall card data, Pydantic edge contracts, a
backend-agnostic collection store (local YAML or Airtable), and the Forge-backed
game-simulation subsystem (`pipeline.sim`).

This package is normally driven through the plugin's PEP-723 CLI wrappers
(`scripts/collection`, `scripts/simulate`) rather than imported directly, but
`pipeline.sim` also exposes a small documented API surface (see
`pipeline/sim/__init__.py`).

- **Plugin usage, install, quickstart, and the `simulate` feature:** see the
  [plugin README](../README.md).
- **License:** GPL-3.0-or-later. See the repository [LICENSE](../../../LICENSE) and
  [NOTICE](../../../NOTICE) (the latter documents the arms-length, fetched-at-runtime
  MTG Forge / Temurin JRE relationship).

## Development

```bash
cd plugins/make-magic/pipeline
uv run --extra dev pytest          # offline test suite (fast; no Forge, no network)
uv run --extra dev pytest -m forge # gated: real Forge games (needs a Forge install)
uv run --extra dev ruff check      # lint
uv run --extra dev pyright         # type-check
```

The `live` and `forge` pytest markers are deselected by default (they need Airtable
credentials / a real Forge install respectively). See [CONTRIBUTING](../../../CONTRIBUTING.md).
