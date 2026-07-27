"""Export each edge contract's JSON Schema to contracts/schema/<Model>.json.

`model_json_schema()` is the single source of truth for MCP inputSchema/
outputSchema and future auto-forms. The generated files are committed; a test
(test_committed_schemas_match_regeneration) guards against drift.

Run:
    uv run --project plugins/make-magic/pipeline \
        python -m pipeline.contracts.export_schemas

Output is pretty-printed with stable (sorted) key order and a trailing newline
so the committed bytes are deterministic and diff-friendly.
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from pipeline.contracts.models import (
    Card,
    Deck,
    DeckLine,
    FactSheet,
    InventoryRow,
    TradeRow,
)

# Directory where committed schemas live (next to this file).
SCHEMA_DIR = Path(__file__).resolve().parent / 'schema'

# The models to export. Order is irrelevant (one file each, sorted keys).
MODELS: tuple[type[BaseModel], ...] = (
    Card,
    DeckLine,
    Deck,
    FactSheet,
    InventoryRow,
    TradeRow,
)


def _schema_bytes(model: type[BaseModel]) -> str:
    """Deterministic, pretty-printed JSON Schema for one model."""
    schema = model.model_json_schema()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def export_all(out_dir: Path = SCHEMA_DIR) -> list[Path]:
    """Write <Model>.json for every model into out_dir. Returns the paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for model in MODELS:
        path = out_dir / f'{model.__name__}.json'
        path.write_text(_schema_bytes(model))
        written.append(path)
    return written


def main() -> None:
    written = export_all()
    for path in written:
        print(f'wrote {path}')


if __name__ == '__main__':
    main()
