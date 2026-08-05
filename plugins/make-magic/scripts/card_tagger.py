#!/usr/bin/env -S uv run --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",
#     "httpx",
#     "typer",
# ]
# [tool.uv.sources]
# make-magic-pipeline = { path = "../pipeline", editable = true }
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
MTG card mechanic tagger — otag-bucket sourced, with deck-fit scoring.

A card's mechanic tags come straight from the pipeline card dim's `otag_buckets`
(the crosswalk over its rolled-up oracle tags), resolved via the package
`CardResolver` seam (`pipeline.collection.resolver.default_card_resolver()`).
otags are more accurate than regex (regex leaves 60%+ of nonlands uncategorized;
otags land 84-92%).

The deck-fit scoring engine (`score_card_for_deck`) is unique to this script —
the pipeline deliberately has no equivalent (it emits neutral facts, defers
scoring to reasoning). Its tag input vocabulary is crosswalk bucket names,
adapted via `BUCKET_STRATEGY_SYNONYMS`.

Tags are the crosswalk buckets (see pipeline `transforms/crosswalk.py`):
    removal ramp draw tokens counters burn tutor sac counterspells flicker typal
    anthem combat protection stax extra_combat wincon

Package access (house convention, same as `scripts/scryfall_batch`): this PEP-723
script pins the local package via `[tool.uv.sources]` as an editable path dep, so
`uv run` resolves `pipeline` on invocation — the package never imports `scripts/`.

Usage:
    ./card_tagger.py tag-card "Storm-Kiln Artist"
    ./card_tagger.py tag-set stx --output /tmp/stx-tagged.json
    ./card_tagger.py tag-file input.json --output /tmp/tagged.json

Maintenance:
    uvx ruff format card_tagger.py
    uvx ruff check card_tagger.py
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import typer

from pipeline.transforms.crosswalk import BUCKET_ROOTS

sys.path.insert(0, str(Path(__file__).parent))

if TYPE_CHECKING:
    from pipeline.contracts import Card

app = typer.Typer()


# ── Card resolver seam ──────────────────────────────────────────────────


class _Resolver(Protocol):
    """The card-resolver seam: name -> enriched `Card` (or None). Structurally the
    package `CardResolver` port; injected for tests, defaulted to the lake-backed
    resolver in the CLI."""

    def get_card(self, name: str) -> Card | None: ...


def _default_resolver() -> _Resolver:
    """The package default resolver (lake-backed, offline-first, live-fallback)."""
    from pipeline.collection.resolver import default_card_resolver

    return default_card_resolver()


# ── Bucket→Strategy synonym layer ───────────────────────────────────────
# Maps each crosswalk otag bucket to the deck-strategy synonym keywords the
# scoring engine's overlap check keys on. The keys are bucket names (crosswalk
# vocabulary). A card's `tags` are its `otag_buckets`.
BUCKET_STRATEGY_SYNONYMS: dict[str, list[str]] = {
    "removal": ["removal", "control", "interaction"],
    "ramp": ["ramp", "mana", "big mana", "lands-matter"],
    "draw": ["card advantage", "draw", "value"],
    "tokens": ["tokens", "go-wide", "aristocrats", "sacrifice"],
    "counters": ["counters", "+1/+1", "voltron", "proliferate"],
    "burn": ["burn", "firebending", "removal", "drain", "aristocrats"],
    "tutor": ["toolbox", "consistency", "tutor"],
    "sac": ["aristocrats", "sacrifice", "graveyard"],
    "counterspells": ["counterspells", "control", "spellslinger"],
    "flicker": ["blink", "etb", "flicker", "value"],
    "typal": ["typal", "tribal", "go-wide"],
    "anthem": ["anthem", "tokens", "go-wide", "combat", "voltron"],
    "combat": ["combat", "aggro", "voltron", "evasion"],
    "protection": ["protection", "voltron", "control"],
    "stax": ["stax", "control", "prison"],
    "extra_combat": ["combat", "aggro", "extra combats"],
    "wincon": ["combo", "wincon", "value"],
}


# ── Bucket→Scryfall discovery map ──────────────────────────────────────
# Sibling to BUCKET_STRATEGY_SYNONYMS: where that maps a bucket to the deck-
# strategy keywords the scoring engine keys on, this maps each bucket to the
# Scryfall functional-search fragments the discovery step runs to pull a real,
# cross-Magic, in-identity, format-legal candidate pool.
#
# Derived, not hand-authored. Our otag vocabulary is Scryfall's oracle-tagger
# vocabulary — the crosswalk (`pipeline.transforms.crosswalk.BUCKET_ROOTS`) was
# built from it — so `otag:<root>` is a live `/cards/search` query by construction.
# Deriving means the map can never drift from the crosswalk and can never carry a
# guessed/dead slug. Every root is live-validated against the Scryfall API; the
# env-gated `live` test (MAKE_MAGIC_LIVE=1) re-checks each returns cards.
#
# Surgical discovery queries one specific root from a bucket's set (e.g.
# `otag:gives-double-strike` rather than the whole `combat` bucket) — every root is
# a live tag, so this is safe and finer-grained. Multiple roots per bucket are
# OR-joined by build_discovery_query.
BUCKET_TO_SCRYFALL_OTAG: dict[str, list[str]] = {
    bucket: [f"otag:{root}" for root in sorted(roots)] for bucket, roots in BUCKET_ROOTS.items()
}


# ── Discovery query builder (pure, offline, unit-testable) ──


def build_discovery_query(
    color_identity: str,
    buckets: list[str],
    *,
    cmc_max: int | None = None,
    extra: str | None = None,
) -> str:
    """Build a Scryfall functional-search query for discovery.

    Pure / offline (no network) so it is unit-testable: it only assembles a query
    string. The caller runs it via `scryfall_cache.py search "<query>"`.

    Shape: `id<=<colors> f:commander (<frag> or <frag> ...) [cmc<=<n>] [extra]`
      - `f:commander` is always present (format legality is a hard pre-filter).
      - `id<=<colors>` is the color-identity pre-filter — native to the query, so
        the returned pool is already in-identity (empty colors -> `id<=c`, colorless).
      - the mapped otag/`o:` fragments for the given buckets are OR-joined inside a
        single parenthesised clause (widest honest net for the role).
      - `cmc<=<n>` bounds the curve slot when supplied.
      - `extra` is appended verbatim (escape hatch for hand-tuned refinements).

    Raises:
      ValueError: if `buckets` is empty (no functional clause = meaningless query).
      KeyError:   if a bucket has no BUCKET_TO_SCRYFALL_OTAG entry — a discovery gap
                  must fail loudly, never silently drop a role.
    """
    if not buckets:
        raise ValueError("build_discovery_query needs at least one bucket")

    fragments: list[str] = []
    for bucket in buckets:
        if bucket not in BUCKET_TO_SCRYFALL_OTAG:
            raise KeyError(
                f"unknown bucket {bucket!r}: no BUCKET_TO_SCRYFALL_OTAG entry "
                f"(known: {sorted(BUCKET_TO_SCRYFALL_OTAG)})"
            )
        for frag in BUCKET_TO_SCRYFALL_OTAG[bucket]:
            if frag not in fragments:  # dedupe, preserve order
                fragments.append(frag)

    colors = "".join(ch for ch in color_identity.lower() if ch in "wubrg") or "c"
    clause = " or ".join(fragments)

    parts = [f"id<={colors}", "f:commander", f"({clause})"]
    if cmc_max is not None:
        parts.append(f"cmc<={cmc_max}")
    if extra:
        parts.append(extra)
    return " ".join(parts)


# ── Otag-bucket tag source ──────────────────────────────────────────────


def tags_for_card(card: Card | None) -> list[str]:
    """A card's mechanic tags = its otag buckets (crosswalk vocabulary).

    Fail-open: an unresolved card (None) or a card with no otag buckets
    yields an empty list — an honest "uncategorized" signal, never a crash.
    """
    if card is None:
        return []
    return list(card.otag_buckets or [])


def process_card(name: str, *, resolver: _Resolver) -> dict:
    """Resolve `name` to an enriched `Card` and project it to a tagged record.

    Tags are sourced from the card dim's `otag_buckets` (not regex). An
    unresolved name degrades to a name-only record with empty tags (fail-open).
    """
    card = resolver.get_card(name)
    if card is None:
        return {
            "name": name,
            "tags": [],
            "type_line": None,
            "mana_cost": None,
            "cmc": None,
            "color_identity": [],
            "oracle_text": None,
            "art_crop": None,
            "scryfall_uri": None,
            "power_toughness": None,
            "keywords": [],
            "set": None,
        }

    power = card.power
    toughness = card.toughness
    power_toughness = f"{power}/{toughness}" if power and toughness else None

    return {
        "name": card.name,
        "tags": tags_for_card(card),
        "type_line": card.type_line,
        "mana_cost": card.mana_cost,
        "cmc": card.mana_value,
        "color_identity": list(card.color_identity or []),
        "oracle_text": card.oracle_text,
        "art_crop": card.art_crop,
        "scryfall_uri": card.scryfall_uri,
        "power_toughness": power_toughness,
        "keywords": list(card.keywords or []),
        "set": card.set_name,
    }


# ── Scoring functions ──────────────────────────────────────────────────
# Deck-fit weighting. The tag input vocabulary is crosswalk buckets, and
# oracle-text pattern checks use plain substring matching (no regex).


def parse_color_identity(color_str: str) -> set[str]:
    return {ch for ch in color_str.upper() if ch in "WUBRG"}


def card_fits_color_identity(card_colors: list[str], deck_colors: set[str]) -> bool:
    if not card_colors:
        return True
    return set(card_colors).issubset(deck_colors)


def compute_tag_strategy_overlap(
    card_tags: list[str], strategy_keywords: list[str]
) -> tuple[float, list[str]]:
    """Score how well a card's otag buckets align with a deck's strategy via the
    bucket->strategy synonym layer."""
    kw_set = {k.lower() for k in strategy_keywords}
    if not kw_set:
        return 0.0, []

    score = 0.0
    matches = []

    for tag in card_tags:
        synonyms = BUCKET_STRATEGY_SYNONYMS.get(tag, [])
        overlap = set(synonyms) & kw_set
        if overlap:
            tag_score = len(overlap) * 1.5
            score += tag_score
            matches.append(f"{tag}->{','.join(sorted(overlap))}")

    return score, matches


def _kw_lower(card: dict) -> list[str]:
    return [k.lower() for k in card.get("keywords", [])]


def score_card_for_deck(card: dict, deck: dict) -> tuple[float, list[str], str]:
    """Score a card's fit for a deck. Returns (score, match_reasons, why_chase).

    Fed by otag-bucket `tags` (via the synonym layer) plus oracle-text substring
    signals.
    """
    score = 0.0
    reasons: list[str] = []

    oracle = (card.get("oracle_text") or "").lower()
    primary_strategy = (
        card.get("primary_strategy") or deck.get("primary_strategy", "")
    ).lower()
    synergy_kw = [kw.lower() for kw in deck.get("synergy_keywords", [])]

    # 1. Bucket->Strategy synonym scoring
    tag_score, tag_matches = compute_tag_strategy_overlap(
        card.get("tags", card.get("mechanic_tags", [])),
        deck.get("synergy_keywords", []),
    )
    score += tag_score
    if tag_matches:
        reasons.append(f"Tag synergy: {'; '.join(tag_matches[:3])}")

    # 2. Oracle text keyword matching
    oracle_hits = sum(1 for kw in synergy_kw if kw in oracle)
    if oracle_hits > 0:
        score += oracle_hits * 1.0
        reasons.append(f"Oracle text matches {oracle_hits} synergy keywords")

    # 3. Strategy-specific deep patterns (substring signals — no regex)
    if "lands-matter" in primary_strategy or "sacrifice" in primary_strategy:
        if "land" in oracle and ("graveyard" in oracle or "sacrifice" in oracle):
            score += 4.0
            reasons.append("Land + graveyard/sacrifice synergy")
        if "landfall" in oracle or "whenever a land enters" in oracle:
            score += 3.0
            reasons.append("Landfall trigger")
        if "sacrifice" in oracle and "creature" in oracle:
            score += 2.0
            reasons.append("Creature sacrifice")

    if "+1/+1 counter" in primary_strategy or "tokens" in primary_strategy:
        if "double" in oracle and "counter" in oracle:
            score += 4.0
            reasons.append("Counter doubling")
        if "+1/+1 counter" in oracle:
            score += 2.0
            reasons.append("+1/+1 counter synergy")

    if "deathtouch" in primary_strategy or "fight" in primary_strategy:
        if "deathtouch" in oracle or "deathtouch" in _kw_lower(card):
            score += 3.5
            reasons.append("Has deathtouch")
        if "fight" in oracle:
            score += 3.5
            reasons.append("Fight effect")
        if "gain control" in oracle:
            score += 2.5
            reasons.append("Theft synergy")

    if "spellslinger" in primary_strategy:
        if (
            "magecraft" in oracle
            or "whenever you cast or copy an instant or sorcery" in oracle
        ):
            score += 5.0
            reasons.append("Magecraft / spellslinger trigger")
        if "instant" in oracle and "sorcery" in oracle and "whenever" in oracle:
            score += 3.0
            reasons.append("Instant/sorcery trigger")
        if "prowess" in oracle or "prowess" in _kw_lower(card):
            score += 2.5
            reasons.append("Has prowess")
        type_line = (card.get("type_line") or card.get("card_type") or "").lower()
        if "instant" in type_line or "sorcery" in type_line:
            score += 2.0
            reasons.append("Is instant/sorcery")
            mc = (card.get("mana_cost") or "").lower()
            if "{x}" in mc:
                score += 2.0
                reasons.append("X-cost instant/sorcery")
        if "treasure" in oracle:
            score += 1.5
            reasons.append("Treasure generation for spell fuel")
        if "exile" in oracle and ("play" in oracle or "cast" in oracle):
            score += 1.5
            reasons.append("Impulse draw for card advantage")

    if "blink" in primary_strategy or "etb" in primary_strategy:
        if (
            "exile" in oracle and "return" in oracle and "battlefield" in oracle
        ) or "flicker" in oracle:
            score += 5.0
            reasons.append("Blink/flicker effect")
        if "enters" in oracle and ("when " in oracle or "whenever" in oracle):
            score += 2.5
            reasons.append("ETB trigger")

    if "-1/-1" in primary_strategy or "aristocrat" in primary_strategy:
        if "-1/-1" in oracle:
            score += 5.0
            reasons.append("-1/-1 counter synergy")
        if "whenever" in oracle and "dies" in oracle:
            score += 3.0
            reasons.append("Death trigger for aristocrats")
        if "sacrifice" in oracle:
            score += 2.0
            reasons.append("Sacrifice synergy")
        if "each opponent" in oracle and (
            "loses" in oracle or "sacrifices" in oracle or "discards" in oracle
        ):
            score += 2.5
            reasons.append("Group punishment / drain")
        if "persist" in _kw_lower(card) or "undying" in _kw_lower(card):
            score += 4.0
            reasons.append("Has persist/undying")

    if "burn" in primary_strategy or "firebending" in primary_strategy:
        mc = (card.get("mana_cost") or "").lower()
        if "{x}" in mc:
            score += 4.0
            reasons.append("X-cost spell for big mana burn")
        if "damage" in oracle and ("deal" in oracle or "deals" in oracle):
            score += 2.5
            reasons.append("Direct damage")
        if "add" in oracle and "mana" in oracle:
            score += 2.0
            reasons.append("Mana generation")
        if "treasure" in oracle:
            score += 2.0
            reasons.append("Treasure for mana acceleration")
        if ("cost" in oracle and "less" in oracle) or "without paying" in oracle:
            score += 2.5
            reasons.append("Cost reduction for big spells")
        if "exile" in oracle and ("play" in oracle or "cast" in oracle):
            score += 1.5
            reasons.append("Impulse draw")

    if "voltron" in primary_strategy or "equipment" in primary_strategy:
        type_line = (card.get("type_line") or card.get("card_type") or "").lower()
        if "equipment" in type_line:
            score += 5.0
            reasons.append("Is equipment")
        if "equip" in oracle or "equipped creature" in oracle:
            score += 3.0
            reasons.append("Equipment synergy")
        if "double strike" in oracle or "double strike" in _kw_lower(card):
            score += 3.5
            reasons.append("Double strike for voltron")
        if "trample" in _kw_lower(card):
            score += 1.0
            reasons.append("Has trample")
        if "indestructible" in oracle or "hexproof" in oracle:
            score += 2.0
            reasons.append("Protection for commander")
        if "creatures you control get +" in oracle:
            score += 2.0
            reasons.append("Anthem buffs commander")

    if "lesson" in primary_strategy:
        type_line = (card.get("type_line") or card.get("card_type") or "").lower()
        if "lesson" in type_line:
            score += 4.0
            reasons.append("Is a Lesson spell")
        if "learn" in oracle:
            score += 3.0
            reasons.append("Has Learn")

    # 4. Small bonuses
    if card.get("is_legendary") and card.get("is_creature"):
        score += 0.2

    why_chase = "; ".join(reasons[:4]) if reasons else "Matches deck strategy"
    return score, reasons, why_chase


def get_confidence(score: float) -> str:
    if score >= 8.0:
        return "very high"
    elif score >= 5.0:
        return "high"
    elif score >= 3.0:
        return "medium"
    else:
        return "low"


def generate_recommendations(
    cards: list[dict],
    decks: list[dict],
    min_score: float = 2.5,
) -> dict:
    """Generate recommendations with no hard cap — uses score threshold."""
    results = {
        "generated_at": datetime.now(UTC).isoformat(),
        "pipeline_version": "v5-otag",
        "card_pool_size": len(cards),
        "min_score_threshold": min_score,
        "decks": [],
    }

    all_recs = []

    for deck in decks:
        deck_colors = parse_color_identity(deck.get("color_identity", ""))
        deck_name = deck.get("deck_name", "")

        valid_cards = [
            c
            for c in cards
            if card_fits_color_identity(c.get("color_identity", []), deck_colors)
        ]

        scored = []
        for card in valid_cards:
            tags = card.get("tags", card.get("mechanic_tags", []))
            if card.get("is_land", False) or (
                "Land" in (card.get("type_line") or card.get("card_type") or "")
            ):
                if not any(t in tags for t in ["ramp", "flicker"]):
                    continue

            score, match_reasons, why_chase = score_card_for_deck(card, deck)
            if score >= min_score:
                scored.append(
                    {
                        "card_name": card["name"],
                        "set": card.get("set"),
                        "mana_cost": card.get("mana_cost"),
                        "cmc": card.get("cmc"),
                        "card_type": card.get("type_line", card.get("card_type")),
                        "color_identity": card.get("color_identity", []),
                        "oracle_text": card.get("oracle_text"),
                        "mechanic_tags": tags,
                        "match_reasons": match_reasons,
                        "why_chase": why_chase,
                        "confidence": get_confidence(score),
                        "rarity": card.get("rarity"),
                        "score": round(score, 2),
                    }
                )

        scored.sort(key=lambda x: x["score"], reverse=True)

        results["decks"].append(
            {
                "deck_name": deck_name,
                "commander": deck.get("commander", ""),
                "color_identity": deck.get("color_identity", ""),
                "primary_strategy": deck.get("primary_strategy", ""),
                "recommendations": scored,
                "recommendation_count": len(scored),
            }
        )

        for s in scored:
            all_recs.append({"card": s["card_name"], "deck": deck_name})

    card_counts = Counter(r["card"] for r in all_recs)
    results["summary"] = {
        "total_recommendations": len(all_recs),
        "unique_cards": len(card_counts),
        "most_recommended": [
            {"card": c, "deck_count": n} for c, n in card_counts.most_common(10)
        ],
    }

    return results


# ── CLI commands ───────────────────────────────────────────────────────


@app.command()
def tag_card(name: str) -> None:
    """Tag a single card's mechanics from its otag buckets (via the card dim)."""
    resolver = _default_resolver()
    if resolver.get_card(name) is None:
        typer.echo(f"Not found: {name}", err=True)
        raise typer.Exit(1)
    result = process_card(name, resolver=resolver)
    typer.echo(json.dumps(result, indent=2))


@app.command()
def tag_set(
    code: str,
    output: Path = typer.Option(None, "--output", "-o"),
) -> None:
    """Tag all cards in a set (otag buckets via the card dim)."""
    # Set enumeration still goes through the live Scryfall façade (no lake set
    # index yet); each card's TAGS come from the resolver's otag buckets.
    from scryfall_cache import ScryfallCache

    cache = ScryfallCache()
    resolver = _default_resolver()
    names = [c.get("name") for c in cache.get_set(code) if c.get("name")]
    processed = [process_card(name, resolver=resolver) for name in names]

    result = {
        "tagged_at": datetime.now(UTC).isoformat(),
        "set_code": code,
        "total_cards": len(processed),
        "cards": processed,
    }

    if output:
        output.write_text(json.dumps(result, indent=2))
        typer.echo(f"Tagged {len(processed)} cards -> {output}")
    else:
        typer.echo(json.dumps(result, indent=2))

    all_tags = [t for c in processed for t in c["tags"]]
    tc = Counter(all_tags)
    zero = sum(1 for c in processed if not c["tags"])
    typer.echo(f"\nTotal: {len(processed)} cards, {len(tc)} unique tags", err=True)
    if processed:
        typer.echo(
            f"Zero-tag cards: {zero} ({zero / len(processed) * 100:.1f}%)", err=True
        )


@app.command()
def tag_file(
    input_path: Path = typer.Argument(
        ..., help="JSON file with card names (or objects with a `name`)"
    ),
    output: Path = typer.Option(None, "--output", "-o"),
) -> None:
    """Tag cards named in a JSON input file (otag buckets via the card dim)."""
    data = json.loads(input_path.read_text())
    entries = data.get("cards", data) if isinstance(data, dict) else data
    names = [e.get("name") if isinstance(e, dict) else e for e in entries]
    resolver = _default_resolver()
    processed = [process_card(name, resolver=resolver) for name in names if name]

    result = {
        "tagged_at": datetime.now(UTC).isoformat(),
        "total_cards": len(processed),
        "cards": processed,
    }

    if output:
        output.write_text(json.dumps(result, indent=2))
        typer.echo(f"Tagged {len(processed)} cards -> {output}")
    else:
        typer.echo(json.dumps(result, indent=2))


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
