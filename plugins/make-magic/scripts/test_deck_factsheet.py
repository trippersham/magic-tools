"""Tests for deck_factsheet — structured facts, the otag integration, and fallback.

Run:
    uv run --with pytest --with typer --with httpx --with duckdb --with pydantic \
        pytest plugins/make-magic/scripts/test_deck_factsheet.py -q

Phase 4b retired the script's OWN regex interaction/draw/ETB/land-fetch census and
delegated the fact sheet to the pipeline's ``factsheet_for`` (multi-label otag
buckets + data-grounded susceptibility). What stays in the script are the
STRUCTURED-field facts (cmc curve, color pips, produced_mana ramp/fixing,
keywords, instant-speed) — these back the graceful FALLBACK when the pipeline
package or its otag snapshot is unavailable.

Governing invariants:
  - The output ALWAYS validates against ``contracts.FactSheet`` (same top-level
    shape, whether pipeline-backed or fallback).
  - Graceful fallback (I5): pipeline unavailable -> still emit structured facts,
    ``otag_buckets == {}``, a clear "otag layer unavailable" signal, no crash.
  - No role/quadrant labels anywhere in the output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The pipeline package lives beside the scripts dir; the script itself adds it to
# sys.path via a shim, but the tests validate against the contract directly.
_PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))

from deck_factsheet import (
    _avg_cmc,
    _fallback_factsheet,
    _load_card_otag,
    _pip_counts,
    _top_end_count,
    build_factsheet,
    cmc_histogram,
    is_land,
    keyword_census,
    ramp_and_fixing,
)

# --------------------------------------------------------------------------- #
# Hand-built card dicts (Scryfall-shaped). No network.
# --------------------------------------------------------------------------- #


def card(
    name: str,
    *,
    type_line: str = "",
    oracle_text: str = "",
    cmc: float = 0.0,
    keywords: list[str] | None = None,
    produced_mana: list[str] | None = None,
    mana_cost: str = "",
    oracle_id: str | None = None,
) -> dict:
    return {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "cmc": cmc,
        "keywords": keywords or [],
        "produced_mana": produced_mana,
        "mana_cost": mana_cost,
        "oracle_id": oracle_id,
    }


SOL_RING = card(
    "Sol Ring",
    type_line="Artifact",
    oracle_text="{T}: Add {C}{C}.",
    cmc=1,
    produced_mana=["C"],
    mana_cost="{1}",
)
LLANOWAR_ELVES = card(
    "Llanowar Elves",
    type_line="Creature — Elf Druid",
    oracle_text="{T}: Add {G}.",
    cmc=1,
    produced_mana=["G"],
    mana_cost="{G}",
)
CHROMATIC_LANTERN = card(
    "Chromatic Lantern",
    type_line="Artifact",
    oracle_text="{T}: Add one mana of any color.",
    cmc=3,
    produced_mana=["W", "U", "B", "R", "G"],
    mana_cost="{3}",
)
BLASPHEMOUS_ACT = card(
    "Blasphemous Act",
    type_line="Sorcery",
    oracle_text="This spell costs {1} less to cast for each creature on the battlefield.",
    cmc=9,
    mana_cost="{8}{R}",
)
SWORDS = card(
    "Swords to Plowshares",
    type_line="Instant",
    oracle_text="Exile target creature.",
    cmc=1,
    mana_cost="{W}",
)
AMBUSH_VIPER = card(
    "Ambush Viper",
    type_line="Creature — Snake",
    oracle_text="",
    cmc=3,
    keywords=["Flash", "Deathtouch"],
    mana_cost="{1}{G}{G}",
)
WRATH = card(
    "Wrath of God",
    type_line="Sorcery",
    oracle_text="Destroy all creatures.",
    cmc=4,
    mana_cost="{2}{W}{W}",
)
SYNERGY_BLANK = card(
    "Metallic Mimic",
    type_line="Artifact Creature — Shapeshifter",
    oracle_text="As Metallic Mimic enters the battlefield, choose a creature type.",
    cmc=2,
    mana_cost="{2}",
)
FOREST = card("Forest", type_line="Basic Land — Forest", produced_mana=["G"])


# --------------------------------------------------------------------------- #
# is_land — KEPT structured fact (front-face governs).
# --------------------------------------------------------------------------- #


def test_is_land():
    assert is_land("Basic Land — Forest") is True
    assert is_land("Sorcery") is False
    assert is_land("Artifact") is False


def test_is_land_uses_front_face_of_mdfc():
    assert is_land("Instant // Land") is False
    assert is_land("Creature — Werewolf // Creature — Werewolf") is False
    assert is_land("Land // Land") is True


# --------------------------------------------------------------------------- #
# KEPT structured facts: cmc curve, pips, ramp/fixing (produced_mana), keywords.
# --------------------------------------------------------------------------- #


def test_cmc_histogram_buckets():
    cards = [
        card("Zero", cmc=0),
        card("One", cmc=1),
        card("Two", cmc=2),
        card("Six", cmc=6),
        card("Eight", cmc=8),
    ]
    hist = cmc_histogram(cards)
    assert hist["0"] == 1
    assert hist["1"] == 1
    assert hist["2"] == 1
    assert hist["6"] == 1
    assert hist["7+"] == 1  # the 8-drop collapses into 7+


def test_cmc_histogram_bucket_keys():
    hist = cmc_histogram([])
    assert set(hist) == {"0", "1", "2", "3", "4", "5", "6", "7+"}
    assert all(v == 0 for v in hist.values())


def test_avg_cmc_nonland_only():
    cards = [
        card("A", type_line="Artifact", cmc=2),
        card("B", type_line="Sorcery", cmc=4),
        FOREST,  # land excluded
    ]
    assert _avg_cmc(cards) == pytest.approx(3.0)


def test_top_end_count():
    cards = [card("Big", type_line="Creature", cmc=7), card("Small", cmc=2)]
    assert _top_end_count(cards) == 1


def test_pip_counts_keys_and_symbols():
    pc = _pip_counts([SOL_RING, LLANOWAR_ELVES, CHROMATIC_LANTERN])
    assert set(pc) == {"W", "U", "B", "R", "G", "C"}
    # Llanowar Elves has a single {G} pip.
    assert pc["G"] >= 1


def test_ramp_from_produced_mana():
    assert ramp_and_fixing([SOL_RING])["ramp_sources"] == 1


def test_creature_dork_is_ramp():
    assert ramp_and_fixing([LLANOWAR_ELVES])["ramp_sources"] == 1


def test_fixing_from_multi_color_rock():
    r = ramp_and_fixing([CHROMATIC_LANTERN])
    assert r["ramp_sources"] == 1
    assert r["fixing_sources"] == 1


def test_single_color_rock_is_ramp_not_fixing():
    r = ramp_and_fixing([SOL_RING])
    assert r["ramp_sources"] == 1
    assert r["fixing_sources"] == 0


def test_cost_reduction_is_not_ramp():
    # No produced_mana -> not a ramp source (structured signal only).
    r = ramp_and_fixing([BLASPHEMOUS_ACT])
    assert r["ramp_sources"] == 0
    assert r["fixing_sources"] == 0


def test_land_does_not_count_as_ramp():
    r = ramp_and_fixing([FOREST])
    assert r["ramp_sources"] == 0
    assert r["fixing_sources"] == 0


def test_keyword_census_counts():
    cards = [
        card("A", type_line="Creature", keywords=["Flying", "Vigilance"]),
        card("B", type_line="Creature", keywords=["Flying"]),
        card("C", type_line="Creature", keywords=["Deathtouch"]),
    ]
    census = keyword_census(cards)
    assert census["Flying"] == 2
    assert census["Vigilance"] == 1
    assert census["Deathtouch"] == 1


def test_keyword_census_omits_zero():
    census = keyword_census([card("A", keywords=["Flying"])])
    assert "Trample" not in census
    assert census == {"Flying": 1}


# --------------------------------------------------------------------------- #
# Fallback path — pipeline/otag unavailable: structured facts, empty buckets.
# --------------------------------------------------------------------------- #


def _validate_factsheet(fs: dict) -> None:
    """Every fact sheet — pipeline-backed or fallback — validates the contract."""
    from pipeline.contracts.models import FactSheet

    FactSheet.model_validate(fs)


def test_fallback_emits_structured_facts_and_empty_buckets():
    cards = [SOL_RING, LLANOWAR_ELVES, CHROMATIC_LANTERN, AMBUSH_VIPER, FOREST]
    fs = _fallback_factsheet(cards, deck="Fallback Deck", missing=[])
    # Structured facts survive the fallback.
    assert fs["shape"]["nonland_count"] == 4
    assert fs["shape"]["land_count"] == 1
    assert fs["mana"]["ramp_sources"] == 3  # Sol Ring, Llanowar, Chromatic Lantern
    assert fs["mana"]["fixing_sources"] == 1  # Chromatic Lantern
    assert fs["interaction"]["instant_speed"] == 1  # Ambush Viper (Flash)
    # otag layer is degraded: empty buckets + a clear signal.
    assert fs["otag_buckets"] == {}
    assert any("otag layer unavailable" in s.lower() for s in fs["susceptibility"])
    _validate_factsheet(fs)


def test_fallback_validates_contract_with_empty_deck():
    fs = _fallback_factsheet([], deck=None, missing=["Bogus Card"])
    assert fs["otag_buckets"] == {}
    assert fs["missing"] == ["Bogus Card"]
    _validate_factsheet(fs)


def test_build_factsheet_falls_back_when_card_otag_none(monkeypatch):
    # Simulate the pipeline being unavailable: force _load_card_otag/_pipeline
    # delegation to fail. build_factsheet must degrade, never crash.
    import deck_factsheet

    monkeypatch.setattr(
        deck_factsheet,
        "_pipeline_factsheet",
        lambda *a, **k: None,  # signal "pipeline unavailable"
    )
    fs = build_factsheet([SOL_RING, WRATH, SWORDS], deck="Deck", card_otag=None)
    assert fs["otag_buckets"] == {}
    assert any("otag layer unavailable" in s.lower() for s in fs["susceptibility"])
    _validate_factsheet(fs)


# --------------------------------------------------------------------------- #
# Pipeline-backed path — otag buckets populate from the real snapshot.
# --------------------------------------------------------------------------- #


def test_load_card_otag_is_populated():
    # The otag layer must load and roll up to a nonempty map (via the puller
    # path, which itself fails open to the bundled snapshot when offline).
    card_otag = _load_card_otag()
    assert card_otag is not None, "card_otag must load (puller or snapshot)"
    assert len(card_otag) > 1000
    # Values are slug closures (sets of strings).
    sample = next(iter(card_otag.values()))
    assert isinstance(sample, set)


# --------------------------------------------------------------------------- #
# _load_card_otag source selection — bundled + self-refreshing (Pilot #1).
#
# 1) Puller-backed: run() lands raw/oracle_tags (watermark-gated, fail-open to
#    snapshot internally); we read the landed tags back and roll them up.
# 2) Snapshot fallback: if the puller/store path raises, load the bundled
#    snapshot directly.
# 3) None: if even the snapshot cannot load -> structured-only fallback (I5).
# --------------------------------------------------------------------------- #

# Synthetic 2-tag DAG: leaf `sweeper` rolls up to parent `removal`. One card
# (oid="oid-a") carries the leaf, so both slugs land in its closure.
_FAKE_TAGS = [
    {
        "id": "tid-removal",
        "slug": "removal",
        "parent_ids": [],
        "taggings": [],
    },
    {
        "id": "tid-sweeper",
        "slug": "sweeper",
        "parent_ids": ["tid-removal"],
        "taggings": [{"oracle_id": "oid-a"}],
    },
]


def test_load_card_otag_routes_through_puller(monkeypatch):
    # PREFERRED path: _load_card_otag must drive the puller (run -> land) and read
    # the LANDED tags back, NOT call _load_snapshot directly. Prove run() is
    # invoked and the rolled-up map comes from the puller-landed tags.
    import deck_factsheet
    from pipeline.ingest import oracle_tags
    from pipeline.transforms import otag_rollup

    calls: dict[str, int] = {"run": 0, "read_raw": 0, "snapshot": 0}

    def fake_run(*a, **k):
        calls["run"] += 1
        return None  # landed path (return value unused by the script)

    def fake_read_raw():
        calls["read_raw"] += 1
        return _FAKE_TAGS

    def fake_snapshot():
        calls["snapshot"] += 1
        return _FAKE_TAGS

    monkeypatch.setattr(oracle_tags, "run", fake_run)
    monkeypatch.setattr(otag_rollup, "_load_raw_tags", fake_read_raw)
    monkeypatch.setattr(oracle_tags, "_load_snapshot", fake_snapshot)

    card_otag = deck_factsheet._load_card_otag()

    assert calls["run"] == 1, "must drive the puller's fetch/watermark/land path"
    assert calls["read_raw"] == 1, "must read the LANDED raw tags back"
    assert calls["snapshot"] == 0, (
        "must NOT bypass the puller with a direct snapshot load"
    )
    # Rollup: the card carries the leaf, so its closure has leaf + ancestor.
    assert card_otag == {"oid-a": {"sweeper", "removal"}}


def test_load_card_otag_falls_back_to_snapshot_when_puller_raises(monkeypatch):
    # If the store/puller machinery is unusable (run raises), fall back to the
    # bundled snapshot directly — still fail-open, never crash.
    import deck_factsheet
    from pipeline.ingest import oracle_tags

    calls: dict[str, int] = {"snapshot": 0}

    def boom(*a, **k):
        raise RuntimeError("store unusable")

    def fake_snapshot():
        calls["snapshot"] += 1
        return _FAKE_TAGS

    monkeypatch.setattr(oracle_tags, "run", boom)
    monkeypatch.setattr(oracle_tags, "_load_snapshot", fake_snapshot)

    card_otag = deck_factsheet._load_card_otag()

    assert calls["snapshot"] == 1, "must fall back to the bundled snapshot"
    assert card_otag == {"oid-a": {"sweeper", "removal"}}


def test_load_card_otag_returns_none_when_all_sources_fail(monkeypatch):
    # No puller, no snapshot -> None so the caller degrades to structured-only.
    import deck_factsheet
    from pipeline.ingest import oracle_tags

    def boom(*a, **k):
        raise RuntimeError("dead")

    monkeypatch.setattr(oracle_tags, "run", boom)
    monkeypatch.setattr(oracle_tags, "_load_snapshot", boom)

    assert deck_factsheet._load_card_otag() is None


def test_build_factsheet_populates_buckets_with_mocked_otag():
    # A card whose oracle_id maps to ramp+tutor slugs must count in both buckets.
    ramped = card(
        "Cultivate",
        type_line="Sorcery",
        oracle_text="Search your library for up to two basic land cards.",
        cmc=3,
        mana_cost="{2}{G}",
        oracle_id="cultivate-oid",
    )
    card_otag = {"cultivate-oid": {"ramp", "tutor"}}
    fs = build_factsheet([ramped], deck="Buckets", card_otag=card_otag)
    assert fs["otag_buckets"].get("ramp") == 1
    assert fs["otag_buckets"].get("tutor") == 1
    _validate_factsheet(fs)


def test_build_factsheet_untagged_card_stays_uncategorized():
    blank = card("No Tags", type_line="Artifact", cmc=2, oracle_id="none-oid")
    fs = build_factsheet([blank], deck="Uncat", card_otag={})
    assert fs["otag_buckets"] == {}
    assert "No Tags" in fs["coverage"]["uncategorized_cards"]
    _validate_factsheet(fs)


# --------------------------------------------------------------------------- #
# Focus-relative analysis — build_factsheet READS an optional focus (never writes).
# --------------------------------------------------------------------------- #


def test_build_factsheet_without_focus_leaves_focus_fields_empty():
    ramped = card(
        "Cultivate",
        type_line="Sorcery",
        oracle_text="Search your library for up to two basic land cards.",
        cmc=3,
        mana_cost="{2}{G}",
        oracle_id="cultivate-oid",
    )
    card_otag = {"cultivate-oid": {"ramp", "tutor"}}
    fs = build_factsheet([ramped], deck="NoFocus", card_otag=card_otag)
    assert fs["focus"] == []
    assert fs["focus_relative"] == {
        "coverage_of_focus": {},
        "thin_focus": [],
        "off_focus": [],
    }
    _validate_factsheet(fs)


def test_build_factsheet_reads_focus_and_computes_signals():
    ramped = card(
        "Cultivate",
        type_line="Sorcery",
        oracle_text="Search your library for up to two basic land cards.",
        cmc=3,
        mana_cost="{2}{G}",
        oracle_id="cultivate-oid",
    )
    card_otag = {"cultivate-oid": {"ramp", "tutor"}}
    # Focus declares ramp (well-supported) + tokens (declared, no support -> thin).
    fs = build_factsheet(
        [ramped], deck="Focus", card_otag=card_otag, focus=["ramp", "tokens"]
    )
    assert fs["focus"] == ["ramp", "tokens"]
    fr = fs["focus_relative"]
    assert fr["coverage_of_focus"]["ramp"] == 1
    assert fr["coverage_of_focus"]["tokens"] == 0
    assert "tokens" in fr["thin_focus"]
    # tutor is a prominent non-focus bucket -> off_focus.
    assert "tutor" in fr["off_focus"]
    _validate_factsheet(fs)


def test_fallback_factsheet_focus_fields_present_when_pipeline_absent():
    # Even in the structured-only fallback, the focus fields exist (empty) so the
    # contract holds; the fallback does not compute focus-relative signals.
    cards = [SOL_RING]
    fs = _fallback_factsheet(cards, deck="Fallback", missing=[])
    assert fs["focus"] == []
    assert fs["focus_relative"] == {
        "coverage_of_focus": {},
        "thin_focus": [],
        "off_focus": [],
    }
    _validate_factsheet(fs)


# --------------------------------------------------------------------------- #
# build_factsheet — full schema, no role labels (contract-level guarantees).
# --------------------------------------------------------------------------- #


def test_build_factsheet_schema_shape():
    cards = [SOL_RING, WRATH, SWORDS, SYNERGY_BLANK, FOREST]
    fs = build_factsheet(cards, deck="Test Deck", card_otag={})
    assert set(fs) >= {
        "deck",
        "shape",
        "mana",
        "keywords",
        "interaction",
        "card_advantage",
        "structural",
        "coverage",
        "cards",
        "missing",
        "otag_buckets",
        "susceptibility",
    }
    assert fs["deck"] == "Test Deck"
    assert fs["shape"]["nonland_count"] == 4
    assert fs["shape"]["land_count"] == 1
    _validate_factsheet(fs)


def test_build_factsheet_per_card_records():
    fs = build_factsheet([SOL_RING], deck=None, card_otag={})
    rec = fs["cards"][0]
    assert set(rec) >= {
        "name",
        "cmc",
        "type_line",
        "keywords",
        "produced_mana",
        "is_land",
        "oracle_text",
    }
    assert rec["name"] == "Sol Ring"
    assert rec["is_land"] is False


def test_build_factsheet_has_no_role_labels():
    cards = [SOL_RING, WRATH, SWORDS]
    fs = build_factsheet(cards, deck=None, card_otag={})
    blob = str(fs).lower()
    for banned in ["development", "parity", "winning", "losing", "quadrant", "wincon"]:
        assert banned not in blob, f"fact sheet must not contain role label '{banned}'"


# --------------------------------------------------------------------------- #
# _parse_decklist — kept from v1, unchanged behavior
# --------------------------------------------------------------------------- #


def test_parse_decklist_handles_counts_comments_and_annotations():
    from deck_factsheet import _parse_decklist

    raw = (
        "# Put That Thang Down\n"
        "# Commander: Nick Fury\n"
        "\n"
        "1 Nick Fury, Agent of S.H.I.E.L.D.  # COMMANDER\n"
        "# --- Nonbasic ---\n"
        "1 Swords to Plowshares\n"
        "2x Lightning Bolt\n"
        "Sol Ring (C21) 263\n"
        "Commander:\n"
    )
    parsed = _parse_decklist(raw)
    assert (1, "Nick Fury, Agent of S.H.I.E.L.D.") in parsed
    assert (1, "Swords to Plowshares") in parsed
    assert (2, "Lightning Bolt") in parsed
    assert (1, "Sol Ring") in parsed
    names = [n for _c, n in parsed]
    assert "Commander" not in names
    assert all("#" not in n for n in names)
