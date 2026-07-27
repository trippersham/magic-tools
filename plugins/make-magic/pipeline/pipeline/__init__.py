"""make-magic data pipeline.

A lightweight, normalized data layer over local files (DuckDB medallion lake:
raw/ -> normalized/ -> marts/) with Pydantic v2 contracts ONLY at the edges.

Governing constraint: this engine is STRICTLY ADDITIVE to the existing
Airtable-as-source-of-truth workflow. It never mutates human-edited Airtable
data; it only reads (pulls) and derives. See
~/.claude/plans/trippersham/magic-tools/2026-07-26-data-architecture/.

Subpackages:
    contracts/  Pydantic v2 boundary models + generated JSON Schema (edges only).
    store/      DuckDB database file + raw/normalized/marts conventions + io.
    ingest/     One hand-rolled puller per source (watermark + append-dedupe).
    transforms/ SQL-per-table + a thin driver + declarative DQ checks.
    adapters/   Airtable (per-field authority), text decklists, MCP tools.
"""

__version__ = "0.1.0"

__all__ = ("__version__",)
