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

import json
import os
import sys
import time
from pathlib import Path

import pytest

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


# --------------------------------------------------------------------------- #
# BUCKET_TO_SCRYFALL_OTAG — the discovery-channel map from crosswalk buckets to
# Scryfall functional-search fragments (otag:/function: slugs, or a curated o:
# oracle-text fallback where a bucket has no clean otag). Sibling to
# BUCKET_STRATEGY_SYNONYMS; the primary discovery channel (A) keys on it.
# --------------------------------------------------------------------------- #


def test_every_bucket_has_a_scryfall_fragment():
    """No silent gaps: EVERY crosswalk bucket in BUCKET_STRATEGY_SYNONYMS must have
    a non-empty BUCKET_TO_SCRYFALL_OTAG entry (a missing bucket is a discovery hole)."""
    buckets = set(card_tagger.BUCKET_STRATEGY_SYNONYMS)
    mapped = set(card_tagger.BUCKET_TO_SCRYFALL_OTAG)
    missing = buckets - mapped
    assert not missing, f"buckets with no Scryfall fragment (discovery gap): {missing}"
    for bucket, fragments in card_tagger.BUCKET_TO_SCRYFALL_OTAG.items():
        assert fragments, f"bucket {bucket!r} maps to an empty fragment list"
        assert all(isinstance(f, str) and f for f in fragments)


def test_scryfall_map_only_references_known_buckets():
    """The map must not invent buckets outside the crosswalk vocabulary."""
    buckets = set(card_tagger.BUCKET_STRATEGY_SYNONYMS)
    mapped = set(card_tagger.BUCKET_TO_SCRYFALL_OTAG)
    extra = mapped - buckets
    assert not extra, f"BUCKET_TO_SCRYFALL_OTAG references unknown buckets: {extra}"


def test_scryfall_fragments_are_query_shaped():
    """Every fragment is a Scryfall query token — an otag:/function: slug or a
    curated o: oracle-text fallback (documented as such)."""
    valid_prefixes = ("otag:", "function:", "o:")
    for bucket, fragments in card_tagger.BUCKET_TO_SCRYFALL_OTAG.items():
        for frag in fragments:
            assert frag.startswith(valid_prefixes), (
                f"bucket {bucket!r} fragment {frag!r} is not a known Scryfall token"
            )


def test_scryfall_map_is_derived_from_crosswalk_roots():
    """The map is DERIVED from crosswalk BUCKET_ROOTS, not hand-authored — so it
    can never drift from the crosswalk nor carry a guessed slug. Each bucket's
    fragments are exactly `otag:<root>` for its roots. This is the invariant that
    replaced the earlier hand-authored map (which shipped dead slugs)."""
    from pipeline.transforms.crosswalk import BUCKET_ROOTS

    expected = {
        bucket: [f"otag:{root}" for root in sorted(roots)] for bucket, roots in BUCKET_ROOTS.items()
    }
    assert expected == card_tagger.BUCKET_TO_SCRYFALL_OTAG


@pytest.mark.skipif(
    os.getenv("MAKE_MAGIC_LIVE") != "1",
    reason="live Scryfall check — set MAKE_MAGIC_LIVE=1 to run (network).",
)
def test_scryfall_buckets_are_live():
    """LIVE guard (opt-in): EVERY bucket's OR-joined discovery query must return
    cards on Scryfall. This is the invariant discovery relies on — a bucket that
    yields no pool is a silent discovery hole. Tested at the BUCKET level (the
    OR of its roots) so a single dead leaf inside an otherwise-live bucket is
    tolerated; a fully-dead bucket fails with its name. Re-validates the crosswalk
    vocabulary against the live tagger taxonomy (5-colour identity so colour never
    zeroes a legitimately mono-colour bucket)."""
    import urllib.error
    import urllib.parse
    import urllib.request

    def total_cards(query: str) -> int:
        url = f"https://api.scryfall.com/cards/search?q={urllib.parse.quote(query)}"
        # Scryfall requires a descriptive User-Agent + Accept header (rejects the
        # urllib default with 400/403), so send them explicitly.
        req = urllib.request.Request(
            url, headers={"User-Agent": "make-magic-tests/1.0", "Accept": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return int(json.load(resp).get("total_cards", 0))
        except urllib.error.HTTPError:
            return 0  # 404 = genuine zero-result → treat as empty (dead pool)

    dead: list[str] = []
    for bucket in card_tagger.BUCKET_TO_SCRYFALL_OTAG:
        query = card_tagger.build_discovery_query("wubrg", [bucket])
        if total_cards(query) == 0:
            dead.append(bucket)
        time.sleep(0.12)  # Scryfall rate-limit courtesy
    assert not dead, f"buckets whose discovery query returns NO cards (dead pool): {dead}"


# --------------------------------------------------------------------------- #
# build_discovery_query — the PURE, offline query builder for discovery channel A.
# --------------------------------------------------------------------------- #


def test_build_discovery_query_single_bucket():
    """A single role builds the canonical `id<= f:commander (<otag>)` shape."""
    q = card_tagger.build_discovery_query("wg", ["removal"])
    assert q == "id<=wg f:commander (otag:removal)"


def test_build_discovery_query_with_cmc():
    """cmc_max appends a `cmc<=` bound."""
    q = card_tagger.build_discovery_query("wg", ["removal"], cmc_max=3)
    assert q == "id<=wg f:commander (otag:removal) cmc<=3"


def test_build_discovery_query_multi_bucket_or_joins():
    """Multiple buckets OR-join all their mapped fragments inside one clause."""
    q = card_tagger.build_discovery_query("r", ["ramp", "removal"])
    # ramp maps to two fragments; all fragments across both buckets OR-join.
    assert q.startswith("id<=r f:commander (")
    assert " or " in q
    assert "otag:removal" in q
    assert "otag:ramp" in q or "otag:mana-ramp" in q


def test_build_discovery_query_dedupes_fragments():
    """Repeated buckets do not duplicate fragments in the OR clause."""
    q = card_tagger.build_discovery_query("g", ["ramp", "ramp"])
    # 'otag:ramp' must appear exactly once.
    assert q.count("otag:ramp") == 1


def test_build_discovery_query_extra_appended():
    """An `extra` fragment is appended verbatim (escape hatch for hand tuning)."""
    q = card_tagger.build_discovery_query("u", ["draw"], extra="-is:reserved")
    assert q.endswith("-is:reserved")


def test_build_discovery_query_colorless_identity():
    """An empty color identity yields `id<=c` (colorless — the strictest identity)."""
    q = card_tagger.build_discovery_query("", ["ramp"])
    assert q.startswith("id<=c f:commander (")


def test_build_discovery_query_unknown_bucket_raises():
    """An unknown bucket is a programming error, not a silent skip — it raises so a
    discovery gap can never pass unnoticed."""
    with pytest.raises(KeyError):
        card_tagger.build_discovery_query("wg", ["not_a_bucket"])


def test_build_discovery_query_no_buckets_raises():
    """No buckets = no functional clause = a meaningless query; reject it."""
    with pytest.raises(ValueError):
        card_tagger.build_discovery_query("wg", [])
