"""Tests for deck_factsheet — pure fact-sheet functions and the decklist parser.

Run:
    uv run --with pytest --with typer pytest plugins/make-magic/scripts/test_deck_factsheet.py -q

These test the PURE functions only (no network, no Scryfall). The CLI is a thin
shell over these and is exercised by live behavioral verification (smoke run).

Governing principle: the fact sheet emits only objective, verifiable FACTS. No
role/quadrant labels. Counts are precision-first — under-claim and let the
residual fall into `uncategorized` rather than guess.
"""

from __future__ import annotations

import pytest

from deck_factsheet import (
    _parse_decklist,
    build_factsheet,
    card_advantage,
    cmc_histogram,
    coverage,
    interaction_census,
    is_land,
    keyword_census,
    ramp_and_fixing,
    structural,
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
) -> dict:
    return {
        "name": name,
        "type_line": type_line,
        "oracle_text": oracle_text,
        "cmc": cmc,
        "keywords": keywords or [],
        "produced_mana": produced_mana,
    }


WRATH = card(
    "Wrath of God",
    type_line="Sorcery",
    oracle_text="Destroy all creatures. They can't be regenerated.",
    cmc=4,
)
TOXIC_DELUGE = card(
    "Toxic Deluge",
    type_line="Sorcery",
    oracle_text="Pay X life. All creatures get -X/-X until end of turn.",
    cmc=3,
)
BLASPHEMOUS_ACT = card(
    "Blasphemous Act",
    type_line="Sorcery",
    oracle_text=(
        "This spell costs {1} less to cast for each creature on the battlefield.\n"
        "Blasphemous Act deals 13 damage to each creature."
    ),
    cmc=9,
)
SWORDS = card(
    "Swords to Plowshares",
    type_line="Instant",
    oracle_text="Exile target creature. Its controller gains life equal to its power.",
    cmc=1,
)
COUNTERSPELL = card(
    "Counterspell",
    type_line="Instant",
    oracle_text="Counter target spell.",
    cmc=2,
)
HEROIC_INTERVENTION = card(
    "Heroic Intervention",
    type_line="Instant",
    oracle_text="Permanents you control gain hexproof and indestructible until end of turn.",
    cmc=2,
)
SOL_RING = card(
    "Sol Ring",
    type_line="Artifact",
    oracle_text="{T}: Add {C}{C}.",
    cmc=1,
    produced_mana=["C"],
)
LLANOWAR_ELVES = card(
    "Llanowar Elves",
    type_line="Creature — Elf Druid",
    oracle_text="{T}: Add {G}.",
    cmc=1,
    produced_mana=["G"],
)
CHROMATIC_LANTERN = card(
    "Chromatic Lantern",
    type_line="Artifact",
    oracle_text="Lands you control have '{T}: Add one mana of any color.'\n{T}: Add one mana of any color.",
    cmc=3,
    produced_mana=["W", "U", "B", "R", "G"],
)
RAMPANT_GROWTH = card(
    "Rampant Growth",
    type_line="Sorcery",
    oracle_text=(
        "Search your library for a basic land card, put it onto the battlefield "
        "tapped, then shuffle."
    ),
    cmc=2,
)
PHYREXIAN_ARENA = card(
    "Phyrexian Arena",
    type_line="Enchantment",
    oracle_text="At the beginning of your upkeep, you draw a card and you lose 1 life.",
    cmc=3,
)
DIVINATION = card(
    "Divination",
    type_line="Sorcery",
    oracle_text="Draw two cards.",
    cmc=3,
)
ELVISH_VISIONARY = card(
    "Elvish Visionary",
    type_line="Creature — Elf",
    oracle_text="When Elvish Visionary enters the battlefield, draw a card.",
    cmc=2,
    keywords=[],
)
RESHAPE_THE_EARTH = card(
    "Living Death",
    type_line="Sorcery",
    oracle_text=(
        "Each player exiles all creature cards from their graveyard, then "
        "sacrifices all creatures they control, then puts all cards they exiled "
        "this way onto the battlefield."
    ),
    cmc=5,
)
GRAVE_RETURN = card(
    "Reanimate",
    type_line="Sorcery",
    oracle_text="Return target creature card from your graveyard to the battlefield.",
    cmc=1,
)
# A pure synergy blank — no interaction, no ramp, no draw. Should be uncategorized.
SYNERGY_BLANK = card(
    "Metallic Mimic",
    type_line="Artifact Creature — Shapeshifter",
    oracle_text=(
        "As Metallic Mimic enters the battlefield, choose a creature type.\n"
        "Metallic Mimic is the chosen type in addition to its other types.\n"
        "Each other creature you control of the chosen type enters with an "
        "additional +1/+1 counter on it."
    ),
    cmc=2,
)
FOREST = card("Forest", type_line="Basic Land — Forest", produced_mana=["G"])


# --------------------------------------------------------------------------- #
# is_land
# --------------------------------------------------------------------------- #


def test_is_land():
    assert is_land("Basic Land — Forest") is True
    assert is_land("Sorcery") is False
    assert is_land("Artifact") is False


def test_is_land_uses_front_face_of_mdfc():
    # Modal DFC spell // land (e.g. Malakir Rebirth // Malakir Mire) is a castable
    # spell, not a land — the front face governs. Regression for the census/coverage
    # signal silently dropping these.
    assert is_land("Instant // Land") is False
    assert is_land("Creature — Werewolf // Creature — Werewolf") is False
    assert is_land("Land // Land") is True  # true DFC land, front is still a land


# --------------------------------------------------------------------------- #
# interaction_census — precision-first
# --------------------------------------------------------------------------- #


def test_board_wipe_destroy_all():
    result = interaction_census([WRATH])
    assert result["board_wipes"] == 1
    assert result["spot_removal"] == 0


def test_board_wipe_minus_x():
    # "All creatures get -X/-X" must count as a board wipe.
    result = interaction_census([TOXIC_DELUGE])
    assert result["board_wipes"] == 1


def test_board_wipe_damage_to_each_creature():
    result = interaction_census([BLASPHEMOUS_ACT])
    assert result["board_wipes"] == 1


def test_spot_removal_exile_target():
    result = interaction_census([SWORDS])
    assert result["spot_removal"] == 1
    assert result["board_wipes"] == 0


def test_counterspell():
    result = interaction_census([COUNTERSPELL])
    assert result["counterspells"] == 1


def test_protection_hexproof_grant():
    result = interaction_census([HEROIC_INTERVENTION])
    assert result["protection"] == 1


def test_instant_speed_by_type():
    result = interaction_census([SWORDS])
    assert result["instant_speed"] == 1


def test_instant_speed_by_flash_keyword():
    flashy = card(
        "Ambush Viper",
        type_line="Creature — Snake",
        oracle_text="",
        cmc=3,
        keywords=["Flash", "Deathtouch"],
    )
    result = interaction_census([flashy])
    assert result["instant_speed"] == 1


def test_sorcery_is_not_instant_speed():
    result = interaction_census([WRATH])
    assert result["instant_speed"] == 0


def test_interaction_census_aggregates():
    cards = [WRATH, TOXIC_DELUGE, SWORDS, COUNTERSPELL, HEROIC_INTERVENTION]
    result = interaction_census(cards)
    assert result["board_wipes"] == 2
    assert result["spot_removal"] == 1
    assert result["counterspells"] == 1
    assert result["protection"] == 1
    # Swords, Counterspell, Heroic Intervention are instants.
    assert result["instant_speed"] == 3


# --------------------------------------------------------------------------- #
# ramp_and_fixing — from produced_mana + land-fetch
# --------------------------------------------------------------------------- #


def test_ramp_from_produced_mana():
    r = ramp_and_fixing([SOL_RING])
    assert r["ramp_sources"] == 1


def test_creature_dork_is_ramp():
    r = ramp_and_fixing([LLANOWAR_ELVES])
    assert r["ramp_sources"] == 1


def test_fixing_from_multi_color_rock():
    r = ramp_and_fixing([CHROMATIC_LANTERN])
    assert r["ramp_sources"] == 1
    assert r["fixing_sources"] == 1


def test_single_color_rock_is_ramp_not_fixing():
    r = ramp_and_fixing([SOL_RING])
    assert r["ramp_sources"] == 1
    assert r["fixing_sources"] == 0


def test_ramp_via_land_fetch():
    r = ramp_and_fixing([RAMPANT_GROWTH])
    assert r["ramp_sources"] == 1


def test_cost_reduction_is_not_ramp():
    # "This spell costs {1} less" cheapens its OWN cast — not ramp.
    r = ramp_and_fixing([BLASPHEMOUS_ACT])
    assert r["ramp_sources"] == 0
    assert r["fixing_sources"] == 0


def test_land_does_not_count_as_ramp():
    # A basic land produces mana but is not a ramp SOURCE (nonland only).
    r = ramp_and_fixing([FOREST])
    assert r["ramp_sources"] == 0
    assert r["fixing_sources"] == 0


def test_pip_counts():
    r = ramp_and_fixing([SOL_RING, LLANOWAR_ELVES, CHROMATIC_LANTERN])
    # produced_mana feeds fixing/ramp; pip_counts come from mana cost symbols.
    # We only assert the structure exists and is a dict of the six symbols.
    assert set(r["pip_counts"]) == {"W", "U", "B", "R", "G", "C"}


# --------------------------------------------------------------------------- #
# keyword_census — from Scryfall `keywords`
# --------------------------------------------------------------------------- #


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
# card_advantage
# --------------------------------------------------------------------------- #


def test_repeatable_draw_beginning_of():
    ca = card_advantage([PHYREXIAN_ARENA])
    assert ca["repeatable_draw"] == 1
    assert ca["one_shot_draw"] == 0


def test_repeatable_draw_whenever():
    rhystic = card(
        "Rhystic Study",
        type_line="Enchantment",
        oracle_text=(
            "Whenever an opponent casts a spell, you may draw a card unless that "
            "player pays {1}."
        ),
        cmc=3,
    )
    ca = card_advantage([rhystic])
    assert ca["repeatable_draw"] == 1


def test_one_shot_draw():
    ca = card_advantage([DIVINATION])
    assert ca["one_shot_draw"] == 1
    assert ca["repeatable_draw"] == 0


# --------------------------------------------------------------------------- #
# structural
# --------------------------------------------------------------------------- #


def test_etb_creature_counted():
    s = structural([ELVISH_VISIONARY])
    assert s["etb_creatures"] == 1


def test_etb_on_noncreature_not_counted():
    # ETB text on a noncreature permanent is not an "etb_creature".
    triome = card(
        "Some Artifact",
        type_line="Artifact",
        oracle_text="When Some Artifact enters the battlefield, draw a card.",
        cmc=2,
    )
    s = structural([triome])
    assert s["etb_creatures"] == 0


def test_graveyard_recursion_present():
    s = structural([GRAVE_RETURN])
    assert s["graveyard_recursion_present"] is True


def test_graveyard_recursion_absent():
    s = structural([SOL_RING, WRATH])
    assert s["graveyard_recursion_present"] is False


def test_graveyard_recursion_underclaims_puts_phrasing():
    # Precision-first: mass reanimation phrased "puts ... onto the battlefield"
    # (Living Death) is intentionally NOT counted — the flag keys on the
    # unambiguous "return ... from ... graveyard to the battlefield". Under-claim
    # rather than widen the pattern into false positives.
    s = structural([RESHAPE_THE_EARTH])
    assert s["graveyard_recursion_present"] is False


# --------------------------------------------------------------------------- #
# cmc_histogram
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
    assert hist["7+"] == 1  # the 8-drop
    assert hist["6"] == 1


def test_cmc_histogram_bucket_keys():
    hist = cmc_histogram([])
    assert set(hist) == {"0", "1", "2", "3", "4", "5", "6", "7+"}
    assert all(v == 0 for v in hist.values())


# --------------------------------------------------------------------------- #
# coverage — the synergy tell
# --------------------------------------------------------------------------- #


def test_coverage_categorizes_interaction_and_ramp():
    cards = [SOL_RING, WRATH, SWORDS]  # all categorized (ramp / interaction)
    cov = coverage(cards)
    assert cov["categorized_pct"] == 100.0
    assert cov["uncategorized_pct"] == 0.0
    assert cov["uncategorized_cards"] == []


def test_coverage_synergy_blank_raises_uncategorized():
    cards = [SOL_RING, WRATH, SYNERGY_BLANK]  # 2 of 3 categorized
    cov = coverage(cards)
    assert cov["categorized_pct"] == pytest.approx(66.67, abs=0.1)
    assert cov["uncategorized_pct"] == pytest.approx(33.33, abs=0.1)
    assert "Metallic Mimic" in cov["uncategorized_cards"]


def test_coverage_repeatable_draw_categorizes():
    cov = coverage([PHYREXIAN_ARENA])
    assert cov["categorized_pct"] == 100.0


def test_coverage_ignores_lands():
    # Lands are excluded from the nonland coverage denominator.
    cov = coverage([SOL_RING, FOREST])
    assert cov["categorized_pct"] == 100.0
    assert cov["uncategorized_cards"] == []


# --------------------------------------------------------------------------- #
# build_factsheet — full schema, no role labels
# --------------------------------------------------------------------------- #


def test_build_factsheet_schema_shape():
    cards = [SOL_RING, WRATH, SWORDS, SYNERGY_BLANK, FOREST]
    fs = build_factsheet(cards, deck="Test Deck")
    # Top-level keys per the locked schema.
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
    }
    assert fs["deck"] == "Test Deck"
    assert fs["shape"]["nonland_count"] == 4
    assert fs["shape"]["land_count"] == 1
    assert set(fs["shape"]) >= {
        "nonland_count",
        "land_count",
        "cmc_histogram",
        "avg_cmc",
        "top_end_count",
    }


def test_build_factsheet_avg_cmc_nonland_only():
    cards = [
        card("A", type_line="Artifact", cmc=2),
        card("B", type_line="Sorcery", cmc=4),
        FOREST,  # land — excluded from avg
    ]
    fs = build_factsheet(cards)
    assert fs["shape"]["avg_cmc"] == pytest.approx(3.0)


def test_build_factsheet_top_end_count():
    cards = [card("Big", type_line="Creature", cmc=7), card("Small", cmc=2)]
    fs = build_factsheet(cards)
    assert fs["shape"]["top_end_count"] == 1


def test_build_factsheet_has_no_role_labels():
    cards = [SOL_RING, WRATH, SWORDS]
    fs = build_factsheet(cards)
    blob = str(fs).lower()
    for banned in ["development", "parity", "winning", "losing", "quadrant", "wincon"]:
        assert banned not in blob, f"fact sheet must not contain role label '{banned}'"


def test_build_factsheet_per_card_records():
    fs = build_factsheet([SOL_RING])
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


# --------------------------------------------------------------------------- #
# _parse_decklist — kept from v1, unchanged behavior
# --------------------------------------------------------------------------- #


def test_parse_decklist_handles_counts_comments_and_annotations():
    raw = (
        "# Put That Thang Down\n"
        "# Commander: Nick Fury\n"
        "\n"
        "1 Nick Fury, Agent of S.H.I.E.L.D.  # COMMANDER\n"
        "# --- Nonbasic ---\n"
        "1 Swords to Plowshares\n"
        "2x Lightning Bolt\n"
        "Sol Ring (C21) 263\n"
        "Commander:\n"  # section header, skipped
    )
    parsed = _parse_decklist(raw)
    assert (1, "Nick Fury, Agent of S.H.I.E.L.D.") in parsed
    assert (1, "Swords to Plowshares") in parsed
    assert (2, "Lightning Bolt") in parsed
    assert (1, "Sol Ring") in parsed
    names = [n for _c, n in parsed]
    assert "Commander" not in names  # header dropped, not treated as a card
    assert all("#" not in n for n in names)  # no inline comments leak into names
