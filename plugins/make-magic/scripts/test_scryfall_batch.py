"""Tests for scryfall_batch — the resolver-consumer batch metadata fetcher.

#5 Task 6b made ``scryfall_batch`` a CONSUMER of the package card resolver
(enrichment) plus a separate live price lookup, dropping its own hand-rolled
Scryfall projection. This test pins the OUTPUT JSON SHAPE the managing-inventory
skill consumes — the ``scryfall`` metadata block keys must be byte-stable — and
proves the resolver + price seams are the source (both mocked, zero network).

Run:
    uv run --with pytest --with typer --with pydantic \
        pytest plugins/make-magic/scripts/test_scryfall_batch.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

_PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import scryfall_batch  # noqa: E402
from pipeline.contracts import Card  # noqa: E402

# The exact metadata keys the managing-inventory skill consumes. Byte-stable.
_META_KEYS = {
    "card_type",
    "mana_cost",
    "cmc",
    "oracle_text",
    "power",
    "toughness",
    "art_crop",
    "scryfall_uri",
    "price_usd",
    "set_name",
    "color_identity",
}


def _card() -> Card:
    return Card(
        name="Sol Ring",
        oracle_id="sol-ring-oid",
        mana_value=1.0,
        mana_cost="{1}",
        type_line="Artifact",
        color_identity=[],
        oracle_text="{T}: Add {C}{C}.",
        power=None,
        toughness=None,
        art_crop="https://img/sol-ring.jpg",
        scryfall_uri="https://scryfall.com/card/c21/263/sol-ring",
        set_name="Commander 2021",
    )


def test_metadata_from_card_preserves_output_shape():
    """The metadata block built from a resolved Card carries EXACTLY the keys the
    skill consumes, mapped from Card enrichment + the live price."""
    meta = scryfall_batch.metadata_from_card(_card(), price_usd="1.23")
    dumped = meta.model_dump()
    assert set(dumped) == _META_KEYS
    assert dumped["card_type"] == "Artifact"
    assert dumped["mana_cost"] == "{1}"
    assert dumped["cmc"] == 1.0
    assert dumped["oracle_text"] == "{T}: Add {C}{C}."
    assert dumped["art_crop"] == "https://img/sol-ring.jpg"
    assert dumped["scryfall_uri"] == "https://scryfall.com/card/c21/263/sol-ring"
    assert dumped["set_name"] == "Commander 2021"
    assert dumped["color_identity"] == []
    assert dumped["price_usd"] == "1.23"
    assert dumped["power"] is None
    assert dumped["toughness"] is None


def test_unresolved_card_maps_nulls_and_defaults():
    """An unresolved Card (name only) still produces a valid, correctly-typed
    metadata block (empty strings / null price)."""
    meta = scryfall_batch.metadata_from_card(
        Card(name="Pre-Release Card"), price_usd=None
    )
    dumped = meta.model_dump()
    assert set(dumped) == _META_KEYS
    assert dumped["card_type"] == ""
    assert dumped["mana_cost"] == ""
    assert dumped["cmc"] == 0
    assert dumped["oracle_text"] == ""
    assert dumped["price_usd"] is None


class _FakeResolver:
    """A stub CardResolver — resolves one known name, misses everything else."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.calls.append(name)
        return _card() if name == "Sol Ring" else None


def test_run_batch_merges_resolver_and_price_zero_network(monkeypatch, tmp_path):
    """End-to-end shape: the batch resolves via the (mocked) resolver, prices via
    the (mocked) price seam, and merges into the record-keyed JSON — no network."""
    resolver = _FakeResolver()
    prices = {"Sol Ring": "1.23"}

    entries = scryfall_batch.run_batch(
        [{"name": "Sol Ring", "id": "recAAA"}, {"name": "Bogus Card", "id": "recBBB"}],
        resolver=resolver,
        price_lookup=lambda name: prices.get(name),
    )

    # Airtable record id passed through unchanged.
    assert entries[0]["id"] == "recAAA"
    assert entries[1]["id"] == "recBBB"

    # Resolved card -> full metadata block; unresolved -> scryfall null + error.
    assert set(entries[0]["scryfall"]) == _META_KEYS
    assert entries[0]["scryfall"]["price_usd"] == "1.23"
    assert entries[1]["scryfall"] is None
    assert "error" in entries[1]
    assert entries[1]["error"] == "Not found on Scryfall: Bogus Card"

    # The resolver seam was the source for both names.
    assert resolver.calls == ["Sol Ring", "Bogus Card"]


def test_run_batch_prices_only_resolved_cards(monkeypatch):
    """Price is looked up ONLY for resolved cards (no wasted lookup on a miss)."""
    resolver = _FakeResolver()
    priced: list[str] = []

    def price_lookup(name: str):
        priced.append(name)
        return "2.00"

    scryfall_batch.run_batch(
        [{"name": "Sol Ring", "id": "r1"}, {"name": "Bogus Card", "id": "r2"}],
        resolver=resolver,
        price_lookup=price_lookup,
    )
    assert priced == ["Sol Ring"]
