"""Tests for card_tagger — otag-bucket sourced tags + the kept scoring engine.

#5 Phase 3a retired ``tag_mechanics`` (the ~54-pattern regex census). Card
mechanic tags now come STRAIGHT from the card dim's ``otag_buckets`` (via the
package ``CardResolver`` seam), and the deck-fit scoring engine — kept intact —
is fed those buckets through a bucket->strategy synonym layer.

Governing invariants:
  - ``tag-card`` / ``tag-set`` / ``tag-file`` emit otag-DERIVED tags; ZERO regex
    is involved (``re`` is not imported by the module at all).
  - Fail-open (I5): an unresolved card -> empty tags, never a crash.
  - The scoring engine's STRUCTURE is unchanged; only its tag INPUT vocabulary
    moved from regex labels to crosswalk buckets. The golden rankings below are
    the POST-OTAG rankings (they intentionally differ from the old regex ones).

Run:
    cd plugins/make-magic/scripts && uv run --with pytest --with typer \
        --with httpx --with duckdb --with pydantic --with-editable ../pipeline \
        pytest test_card_tagger.py -q
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

import card_tagger  # noqa: E402
from pipeline.contracts import Card  # noqa: E402


# --------------------------------------------------------------------------- #
# The regex census is GONE — the module must not even import `re`.
# --------------------------------------------------------------------------- #


def test_module_has_no_regex_import():
    """The regex census is retired: `re` is not a module attribute, and there is
    no `tag_mechanics` function left."""
    assert not hasattr(card_tagger, "re"), "card_tagger must not import `re` (regex census retired)"
    assert not hasattr(card_tagger, "tag_mechanics"), "tag_mechanics regex census must be deleted"


# --------------------------------------------------------------------------- #
# tags_for_card — the otag-bucket source of a card's mechanic tags.
# --------------------------------------------------------------------------- #


def _card(name: str, *, otag_buckets: list[str] | None = None, **kw) -> Card:
    return Card(name=name, otag_buckets=otag_buckets or [], **kw)


class _FakeResolver:
    def __init__(self, cards: dict[str, Card | None]) -> None:
        self._cards = cards
        self.calls: list[str] = []

    def get_card(self, name: str) -> Card | None:
        self.calls.append(name)
        return self._cards.get(name)


def test_tags_for_card_sources_from_otag_buckets():
    """A resolved card's tags are exactly its otag buckets (crosswalk vocabulary)."""
    card = _card("Cultivate", otag_buckets=["ramp", "tutor"])
    assert card_tagger.tags_for_card(card) == ["ramp", "tutor"]


def test_tags_for_card_unresolved_is_empty_fail_open():
    """Fail-open: a None card (unresolved) yields empty tags, never a crash."""
    assert card_tagger.tags_for_card(None) == []


def test_tags_for_card_no_buckets_is_empty():
    """A resolved card with no otag buckets yields empty tags (honest uncategorized)."""
    assert card_tagger.tags_for_card(_card("Vanilla Bear")) == []


def test_process_card_uses_resolver_otag_buckets():
    """process_card derives `tags` from the resolver's otag_buckets, not regex."""
    resolver = _FakeResolver(
        {
            "Cultivate": _card(
                "Cultivate",
                otag_buckets=["ramp", "tutor"],
                mana_value=3.0,
                mana_cost="{2}{G}",
                type_line="Sorcery",
                oracle_text="Search your library for up to two basic land cards.",
                color_identity=["G"],
            )
        }
    )
    rec = card_tagger.process_card("Cultivate", resolver=resolver)
    assert rec["tags"] == ["ramp", "tutor"]
    assert rec["name"] == "Cultivate"
    assert rec["type_line"] == "Sorcery"
    assert resolver.calls == ["Cultivate"]


def test_process_card_unresolved_degrades():
    """An unresolved name degrades to empty tags + a name-only record, no crash."""
    resolver = _FakeResolver({})
    rec = card_tagger.process_card("Nonexistent Card", resolver=resolver)
    assert rec["name"] == "Nonexistent Card"
    assert rec["tags"] == []


# --------------------------------------------------------------------------- #
# The kept scoring engine — now fed otag buckets via the bucket->strategy layer.
# --------------------------------------------------------------------------- #


def test_bucket_strategy_overlap_scores_matching_bucket():
    """A card whose bucket maps to a deck strategy keyword scores > 0."""
    score, matches = card_tagger.compute_tag_strategy_overlap(["ramp"], ["ramp", "big mana"])
    assert score > 0
    assert matches


def test_bucket_strategy_overlap_no_match_is_zero():
    score, matches = card_tagger.compute_tag_strategy_overlap(["removal"], ["spellslinger"])
    assert score == 0.0
    assert matches == []


def test_score_card_for_deck_uses_bucket_tags():
    """score_card_for_deck consumes the otag-bucket `tags` through the synonym layer."""
    card = {
        "name": "Cultivate",
        "tags": ["ramp", "tutor"],
        "oracle_text": "search your library for up to two basic land cards",
        "type_line": "Sorcery",
        "color_identity": ["G"],
    }
    deck = {
        "primary_strategy": "lands-matter ramp",
        "synergy_keywords": ["ramp", "mana", "lands-matter"],
    }
    score, reasons, _why = card_tagger.score_card_for_deck(card, deck)
    assert score > 0
    assert reasons


# --------------------------------------------------------------------------- #
# Golden ranking — POST-OTAG. These are the NEW rankings sourced from otag
# buckets (they differ from the old regex-census rankings by design).
# --------------------------------------------------------------------------- #


def _golden_pool() -> list[dict]:
    """A small pool whose tags are otag buckets (crosswalk vocabulary)."""
    return [
        {
            "name": "Storm-Kiln Artist",
            "tags": ["burn", "ramp"],
            "type_line": "Creature — Dwarf Wizard",
            "mana_cost": "{3}{R}",
            "cmc": 4,
            "color_identity": ["R"],
            "oracle_text": "magecraft — whenever you cast or copy an instant or sorcery spell, create a treasure token.",
            "rarity": "uncommon",
        },
        {
            "name": "Cultivate",
            "tags": ["ramp", "tutor"],
            "type_line": "Sorcery",
            "mana_cost": "{2}{G}",
            "cmc": 3,
            "color_identity": ["G"],
            "oracle_text": "search your library for up to two basic land cards.",
            "rarity": "common",
        },
        {
            "name": "Swords to Plowshares",
            "tags": ["removal"],
            "type_line": "Instant",
            "mana_cost": "{W}",
            "cmc": 1,
            "color_identity": ["W"],
            "oracle_text": "exile target creature. its controller gains life equal to its power.",
            "rarity": "uncommon",
        },
    ]


def test_golden_spellslinger_ranking_is_post_otag():
    """POST-OTAG golden: on a spellslinger deck, the magecraft/treasure card ranks
    top, driven by oracle-text patterns + the `burn`/`ramp` buckets. (This ranking
    is otag-sourced and intentionally differs from the retired regex census.)"""
    deck = {
        "deck_name": "Spellslinger",
        "commander": "Kalamax",
        "color_identity": "RUG",
        "primary_strategy": "spellslinger",
        "synergy_keywords": ["spellslinger", "burn", "ramp", "treasure"],
    }
    scored = sorted(
        ((c["name"], card_tagger.score_card_for_deck(c, deck)[0]) for c in _golden_pool()),
        key=lambda x: x[1],
        reverse=True,
    )
    names = [n for n, _ in scored]
    assert names[0] == "Storm-Kiln Artist", f"post-otag ranking changed: {scored}"
    # Every score is deterministic and reproducible.
    assert all(s >= 0 for _, s in scored)


def test_generate_recommendations_ranks_and_thresholds():
    """generate_recommendations sorts by score and applies the min_score cutoff on
    the otag-bucket-fed engine."""
    decks = [
        {
            "deck_name": "Ramp",
            "commander": "Azusa",
            "color_identity": "G",
            "primary_strategy": "lands-matter ramp",
            "synergy_keywords": ["ramp", "mana", "lands-matter"],
        }
    ]
    result = card_tagger.generate_recommendations(_golden_pool(), decks, min_score=0.1)
    recs = result["decks"][0]["recommendations"]
    # Sorted descending by score.
    scores = [r["score"] for r in recs]
    assert scores == sorted(scores, reverse=True)
    # Cultivate (ramp+tutor) is the strongest ramp fit.
    assert recs[0]["card_name"] == "Cultivate"
