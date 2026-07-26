#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",  # transitive: required by scryfall_cache
#     "typer",
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
MTG deck fact sheet — emits a NEUTRAL, verifiable JSON fact sheet for a decklist.

This is NOT a scorer. It never assigns a role/quadrant, never decides if a card
is a wincon / ramp-vs-combo / engine-vs-clawback / "good." Those are contextual
roles → reasoning (LLM). This script emits only objective facts about the cards
that would NOT change if the card were moved to a different deck.

Governing test for every fact: "would the answer change if this card were in a
different deck?" If yes, it does not belong here. Counts are precision-first:
only claim what is unambiguous; report the residual as `uncategorized`. Uses
Scryfall STRUCTURED fields (`keywords`, `produced_mana`) rather than regex
wherever possible.

Companion to card_tagger.py; reuses scryfall_cache.py for all Scryfall lookups.

Usage:
    ./deck_factsheet.py factsheet decklist.txt --output /tmp/deck-facts.json

Testing:
    uv run --with pytest --with typer pytest test_deck_factsheet.py -q

Maintenance:
    uvx ruff format deck_factsheet.py
    uvx ruff check deck_factsheet.py
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import typer

app = typer.Typer()

# CMC histogram buckets. 7+ collects everything at CMC >= 7.
_CMC_BUCKETS = ("0", "1", "2", "3", "4", "5", "6", "7+")
_PIP_SYMBOLS = ("W", "U", "B", "R", "G", "C")


# --------------------------------------------------------------------------- #
# Pure fact functions — no network, no I/O. Unit-tested in test_deck_factsheet.
#
# Each takes a list of Scryfall-shaped card dicts (already face-normalized via
# _card_fields at the CLI boundary) and returns objective counts. Precision
# first: when a pattern is ambiguous, we UNDER-claim and let the card fall into
# `uncategorized` rather than guess (guessing is what inverted the retired v1).
# --------------------------------------------------------------------------- #


def is_land(type_line: str) -> bool:
    """A card is a land iff its FRONT face is a land.

    Uses only the front face so a modal DFC spell // land (e.g. Malakir Rebirth //
    Malakir Mire, type line "Instant // Land") is treated as the castable spell it
    is, not silently dropped from the nonland census/coverage.
    """
    front = (type_line or "").split("//")[0]
    return "land" in front.lower()


def _text(card: dict) -> str:
    return (card.get("oracle_text") or "").lower()


def _type_line(card: dict) -> str:
    return card.get("type_line") or ""


def _is_creature(card: dict) -> bool:
    return "creature" in _type_line(card).lower()


def _is_instant_speed(card: dict) -> bool:
    """Instant-speed iff type line is an Instant OR the card has Flash."""
    if "instant" in _type_line(card).lower():
        return True
    kw = [k.lower() for k in card.get("keywords", []) or []]
    return "flash" in kw


# --- interaction census ---------------------------------------------------- #

_BOARD_WIPE_PATTERNS = (
    re.compile(r"destroy all creatures"),
    re.compile(r"destroy all (nonland )?permanents"),
    re.compile(r"exile all creatures"),
    # "deals N damage to each creature" (any-source damage sweeper).
    re.compile(r"deals? \d+ damage to each creature"),
    # "All creatures get -X/-X" or "-1/-1" — variable or fixed toughness sweeper.
    re.compile(r"all creatures get -"),
)

_SPOT_REMOVAL_PATTERN = re.compile(
    r"(destroy|exile) target "
    r"(creature|permanent|artifact|enchantment|planeswalker|land|"
    r"nonland permanent|creature or planeswalker|creature or enchantment|"
    r"artifact or enchantment|artifact or creature)"
)

_COUNTER_PATTERN = re.compile(r"counter target")

_PROTECTION_KEYWORDS = {"hexproof", "indestructible", "ward", "shroud"}
_PROTECTION_TEXT_PATTERNS = (
    re.compile(r"\b(hexproof|indestructible|shroud)\b"),
    re.compile(r"\bward\b"),
    re.compile(r"protection from"),
    re.compile(r"phases? out"),
)


def _is_board_wipe(card: dict) -> bool:
    t = _text(card)
    return any(p.search(t) for p in _BOARD_WIPE_PATTERNS)


def _is_spot_removal(card: dict) -> bool:
    return bool(_SPOT_REMOVAL_PATTERN.search(_text(card)))


def _is_counterspell(card: dict) -> bool:
    return bool(_COUNTER_PATTERN.search(_text(card)))


def _is_protection(card: dict) -> bool:
    kw = {k.lower() for k in card.get("keywords", []) or []}
    if kw & _PROTECTION_KEYWORDS:
        return True
    t = _text(card)
    return any(p.search(t) for p in _PROTECTION_TEXT_PATTERNS)


def interaction_census(cards: list[dict]) -> dict:
    """Precision-first interaction counts by type & speed.

    - board_wipes: destroy all creatures/permanents | "deals N damage to each
      creature" | "all creatures get -" (fixed or variable -X/-X sweeper)
    - spot_removal: explicit "destroy/exile target <permanent-type>"
    - counterspells: "counter target"
    - protection: hexproof/indestructible/ward/shroud keyword OR "protection
      from" OR "phase(s) out"
    - instant_speed: type line Instant OR Flash keyword
    """
    board_wipes = spot_removal = counterspells = protection = instant_speed = 0
    for c in cards:
        if _is_board_wipe(c):
            board_wipes += 1
        if _is_spot_removal(c):
            spot_removal += 1
        if _is_counterspell(c):
            counterspells += 1
        if _is_protection(c):
            protection += 1
        if _is_instant_speed(c):
            instant_speed += 1
    return {
        "board_wipes": board_wipes,
        "spot_removal": spot_removal,
        "counterspells": counterspells,
        "protection": protection,
        "instant_speed": instant_speed,
    }


# --- ramp & fixing --------------------------------------------------------- #

# Explicit land-fetch that puts a land into play (ramp, not a tutor to hand).
_LAND_FETCH_PATTERN = re.compile(r"search your library for .{0,60}?\bland", re.DOTALL)
_LAND_TO_BATTLEFIELD = re.compile(r"onto the battlefield")


def _produces_mana(card: dict) -> bool:
    """A nonland produces mana iff Scryfall's structured produced_mana is set.

    This is the precise, structured signal — no regex over 'add {...}'. Cards
    whose own cast is cheapened ("this spell costs {1} less") have no
    produced_mana and correctly do NOT count as ramp.
    """
    pm = card.get("produced_mana")
    return bool(pm)


def _is_land_fetch_ramp(card: dict) -> bool:
    """Explicit 'search your library for … land … onto the battlefield'."""
    t = _text(card)
    return bool(_LAND_FETCH_PATTERN.search(t)) and bool(_LAND_TO_BATTLEFIELD.search(t))


def _is_ramp_source(card: dict) -> bool:
    """A NONLAND is ramp if it produces mana OR fetches a land into play."""
    if is_land(_type_line(card)):
        return False
    return _produces_mana(card) or _is_land_fetch_ramp(card)


def _is_fixing_source(card: dict) -> bool:
    """A NONLAND ramp source that produces >1 distinct color or any-color.

    'Any color' shows up in produced_mana as all five colors (Scryfall lists the
    concrete colors a source can produce). >1 distinct WUBRG color => fixing.
    Colorless-only (['C']) is ramp but not fixing.
    """
    if is_land(_type_line(card)):
        return False
    pm = card.get("produced_mana") or []
    colors = {m for m in pm if m in ("W", "U", "B", "R", "G")}
    return len(colors) > 1


def _pip_counts(cards: list[dict]) -> dict:
    """Count colored/colorless mana pips across nonland mana costs.

    Structural fact from the mana cost symbols. Hybrid/Phyrexian pips are
    counted once per listed color symbol.
    """
    counts = {sym: 0 for sym in _PIP_SYMBOLS}
    for c in cards:
        if is_land(_type_line(c)):
            continue
        cost = c.get("mana_cost") or ""
        for sym in re.findall(r"\{([^}]+)\}", cost):
            for part in sym.split("/"):
                if part in counts:
                    counts[part] += 1
    return counts


def ramp_and_fixing(cards: list[dict]) -> dict:
    """Count ramp sources, fixing sources, and pip distribution (nonland only)."""
    ramp = sum(1 for c in cards if _is_ramp_source(c))
    fixing = sum(1 for c in cards if _is_fixing_source(c))
    return {
        "ramp_sources": ramp,
        "fixing_sources": fixing,
        "pip_counts": _pip_counts(cards),
    }


# --- keyword census -------------------------------------------------------- #


def keyword_census(cards: list[dict]) -> dict:
    """Count Scryfall `keywords` across the deck. Nonzero only, structured."""
    counter: Counter[str] = Counter()
    for c in cards:
        for kw in c.get("keywords", []) or []:
            counter[kw] += 1
    return dict(counter)


# --- card advantage -------------------------------------------------------- #

# Repeatable draw: a triggered/recurring draw engine.
_REPEATABLE_DRAW_PATTERNS = (
    re.compile(r"at the beginning of .{0,80}?draw", re.DOTALL),
    re.compile(r"whenever .{0,80}?draw a card", re.DOTALL),
)
# One-shot draw: "draw N cards" not covered by a repeatable trigger.
_ONE_SHOT_DRAW_PATTERN = re.compile(
    r"draw (a|one|two|three|four|five|six|seven|\d+) cards?"
)


def _is_repeatable_draw(card: dict) -> bool:
    t = _text(card)
    return any(p.search(t) for p in _REPEATABLE_DRAW_PATTERNS)


def _is_one_shot_draw(card: dict) -> bool:
    t = _text(card)
    return bool(_ONE_SHOT_DRAW_PATTERN.search(t))


def card_advantage(cards: list[dict]) -> dict:
    """Count repeatable-draw engines vs one-shot draw spells (precision-first).

    A card counts as repeatable_draw if it has a recurring draw trigger. It
    counts as one_shot_draw only if it draws cards and is NOT repeatable (a
    card is one or the other, never both).
    """
    repeatable = one_shot = 0
    for c in cards:
        if _is_repeatable_draw(c):
            repeatable += 1
        elif _is_one_shot_draw(c):
            one_shot += 1
    return {"repeatable_draw": repeatable, "one_shot_draw": one_shot}


# --- structural flags ------------------------------------------------------ #

_ETB_PATTERN = re.compile(r"when(ever)? .{0,40}?enters", re.DOTALL)
_GRAVEYARD_RECURSION_PATTERN = re.compile(
    r"return .{0,60}?from .{0,30}?graveyard to the battlefield", re.DOTALL
)


def _has_etb(card: dict) -> bool:
    return bool(_ETB_PATTERN.search(_text(card)))


def structural(cards: list[dict]) -> dict:
    """Structural facts: ETB creatures count, graveyard-recursion presence.

    etb_creatures: a CREATURE whose oracle text has a "when(ever) ~ enters"
    trigger. graveyard_recursion_present: any card returns something from a
    graveyard to the battlefield.
    """
    etb_creatures = sum(1 for c in cards if _is_creature(c) and _has_etb(c))
    recursion = any(_GRAVEYARD_RECURSION_PATTERN.search(_text(c)) for c in cards)
    return {
        "etb_creatures": etb_creatures,
        "graveyard_recursion_present": recursion,
    }


# --- shape ----------------------------------------------------------------- #


def _cmc_bucket(cmc: float) -> str:
    n = int(cmc or 0)
    return "7+" if n >= 7 else str(n)


def cmc_histogram(cards: list[dict]) -> dict:
    """CMC histogram over NONLAND cards, bucketed 0..6 and 7+."""
    hist = {b: 0 for b in _CMC_BUCKETS}
    for c in cards:
        if is_land(_type_line(c)):
            continue
        hist[_cmc_bucket(c.get("cmc") or 0)] += 1
    return hist


def _avg_cmc(cards: list[dict]) -> float:
    nonland = [c for c in cards if not is_land(_type_line(c))]
    if not nonland:
        return 0.0
    total = sum(float(c.get("cmc") or 0) for c in nonland)
    return round(total / len(nonland), 2)


def _top_end_count(cards: list[dict]) -> int:
    return sum(
        1 for c in cards if not is_land(_type_line(c)) and float(c.get("cmc") or 0) >= 6
    )


# --- coverage (the synergy tell) ------------------------------------------- #


def _is_categorized(card: dict) -> bool:
    """A NONLAND card is categorized if it hits >=1 of interaction / ramp /
    repeatable_draw. Anything else is uncategorized — the synergy tell."""
    return (
        _is_board_wipe(card)
        or _is_spot_removal(card)
        or _is_counterspell(card)
        or _is_protection(card)
        or _is_ramp_source(card)
        or _is_repeatable_draw(card)
    )


def coverage(cards: list[dict]) -> dict:
    """% of NONLAND cards matched to a census category vs uncategorized.

    High uncategorized_pct is a FEATURE: it flags a synergy-driven deck whose
    value is invisible to precision-first counts (trust the plan, not the
    numbers). Lands are excluded from the denominator.
    """
    nonland = [c for c in cards if not is_land(_type_line(c))]
    total = len(nonland)
    if total == 0:
        return {
            "categorized_pct": 0.0,
            "uncategorized_pct": 0.0,
            "uncategorized_cards": [],
        }
    uncategorized = [c for c in nonland if not _is_categorized(c)]
    categorized_n = total - len(uncategorized)
    categorized_pct = round(categorized_n / total * 100, 2)
    uncategorized_pct = round(len(uncategorized) / total * 100, 2)
    return {
        "categorized_pct": categorized_pct,
        "uncategorized_pct": uncategorized_pct,
        "uncategorized_cards": [c.get("name", "") for c in uncategorized],
    }


# --- per-card records ------------------------------------------------------ #


def _card_record(card: dict) -> dict:
    """Raw per-card facts so the LLM has material without re-fetching."""
    return {
        "name": card.get("name", ""),
        "cmc": card.get("cmc"),
        "type_line": _type_line(card),
        "keywords": card.get("keywords", []) or [],
        "produced_mana": card.get("produced_mana"),
        "is_land": is_land(_type_line(card)),
        "oracle_text": card.get("oracle_text") or "",
    }


def build_factsheet(
    cards: list[dict],
    deck: str | None = None,
    missing: list[str] | None = None,
) -> dict:
    """Assemble the full neutral fact sheet from face-normalized card dicts.

    NO role/quadrant labels appear anywhere in the output — only facts.
    """
    nonland_count = sum(1 for c in cards if not is_land(_type_line(c)))
    land_count = sum(1 for c in cards if is_land(_type_line(c)))
    return {
        "deck": deck,
        "shape": {
            "nonland_count": nonland_count,
            "land_count": land_count,
            "cmc_histogram": cmc_histogram(cards),
            "avg_cmc": _avg_cmc(cards),
            "top_end_count": _top_end_count(cards),
        },
        "mana": ramp_and_fixing(cards),
        "keywords": keyword_census(cards),
        "interaction": interaction_census(cards),
        "card_advantage": card_advantage(cards),
        "structural": structural(cards),
        "coverage": coverage(cards),
        "cards": [_card_record(c) for c in cards],
        "missing": missing or [],
    }


# --------------------------------------------------------------------------- #
# Decklist parsing (kept verbatim from v1 — inline-comment + set-annotation
# stripping; skips blanks / comments / section headers).
# --------------------------------------------------------------------------- #

_DECK_LINE = re.compile(r"^\s*(?:(\d+)x?\s+)?(.+?)\s*$")


def _parse_decklist(raw: str) -> list[tuple[int, str]]:
    """Parse 'N Card Name' / 'Nx Card Name' / 'Card Name' lines. Skips blanks,
    comments (#, //), and section headers (lines ending in ':')."""
    out: list[tuple[int, str]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "//")):
            continue
        # Strip inline comments ("1 Sol Ring  # COMMANDER" -> "1 Sol Ring").
        s = re.split(r"\s+(?:#|//)", s, maxsplit=1)[0].strip()
        if not s or s.endswith(":"):
            continue
        m = _DECK_LINE.match(s)
        if not m:
            continue
        count = int(m.group(1)) if m.group(1) else 1
        name = m.group(2).strip()
        # Drop trailing set/collector annotations like "(C21) 123".
        name = re.sub(r"\s*\([0-9A-Za-z]{2,5}\)\s*[\d\-A-Za-z]*$", "", name).strip()
        if name:
            out.append((count, name))
    return out


# --------------------------------------------------------------------------- #
# Scryfall-backed CLI — thin shell over the pure functions above.
# --------------------------------------------------------------------------- #


def _get_cache():
    """Import ScryfallCache from the sibling script (same shim as card_tagger)."""
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from scryfall_cache import ScryfallCache

    return ScryfallCache()


def _card_fields(card: dict) -> dict:
    """Normalize a Scryfall card into the flat shape the fact functions expect,
    falling back to card_faces[0] for double-faced cards (DFCs)."""
    face = card
    if card.get("oracle_text") is None and card.get("card_faces"):
        face = card["card_faces"][0]
    return {
        "name": card.get("name", ""),
        "oracle_text": face.get("oracle_text", "") or "",
        "type_line": card.get("type_line") or face.get("type_line", "") or "",
        "cmc": card.get("cmc"),
        "keywords": card.get("keywords", []) or [],
        "produced_mana": card.get("produced_mana"),
        "mana_cost": face.get("mana_cost", card.get("mana_cost", "")) or "",
    }


@app.command()
def factsheet(
    path: str,
    output: str = typer.Option(
        None, "--output", help="Write JSON here instead of stdout"
    ),
) -> None:
    """Build a neutral fact sheet for a decklist file (fetches via Scryfall cache)."""
    cache = _get_cache()
    raw = Path(path).read_text()
    entries = _parse_decklist(raw)

    cards: list[dict] = []
    missing: list[str] = []
    for _count, name in entries:
        card = cache.get_card(name)
        if not card:
            missing.append(name)
            continue
        cards.append(_card_fields(card))

    deck_name = _deck_name_from_header(raw)
    report = build_factsheet(cards, deck=deck_name, missing=missing)
    payload = json.dumps(report, indent=2)
    if output:
        Path(output).write_text(payload)
        typer.echo(
            f"Wrote {output} — {report['shape']['nonland_count']} nonland, "
            f"{report['shape']['land_count']} lands, {len(missing)} missing"
        )
    else:
        typer.echo(payload)


def _deck_name_from_header(raw: str) -> str | None:
    """First non-empty comment line is treated as the deck name, if present."""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith("#"):
            return s.lstrip("#").strip() or None
        if s:
            return None
    return None


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == "__main__":
    app()
