#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",
#     "typer",
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
MTG card mechanic tagger — extracted from mtg_pipeline_v4.py.

54 regex-based mechanic tag patterns + tag→strategy synonym layer.
Uses scryfall_cache.py for all Scryfall lookups.

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
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import typer

app = typer.Typer()

# ── Tag→Strategy synonym layer ──────────────────────────────────────────
TAG_STRATEGY_SYNONYMS: dict[str, list[str]] = {
    "Magecraft": ["spellslinger", "instant", "sorcery", "noncreature", "prowess"],
    "Instant/Sorcery matters": ["spellslinger", "noncreature", "prowess"],
    "Cast Trigger": ["spellslinger", "noncreature", "storm"],
    "Copy Spell": ["spellslinger", "noncreature"],
    "Learn": ["lesson", "spellslinger"],
    "Lesson": ["lesson", "learn"],
    "Prowess": ["spellslinger", "noncreature"],
    "ETB trigger": ["blink", "etb", "flicker"],
    "Blink/flicker": ["blink", "etb", "flicker"],
    "ETB interaction": ["blink", "etb", "flicker", "stax"],
    "Token generation": ["tokens", "go-wide", "aristocrats", "sacrifice"],
    "Death trigger": ["aristocrats", "sacrifice", "graveyard"],
    "Sacrifice Outlet": ["aristocrats", "sacrifice"],
    "Group Punishment": ["aristocrats", "drain", "burn"],
    "+1/+1 counters": ["counters", "+1/+1", "voltron", "tokens"],
    "Counter Doubling": ["counters", "+1/+1"],
    "Proliferate": ["counters", "+1/+1", "-1/-1"],
    "-1/-1 counters": ["-1/-1", "wither", "aristocrats"],
    "Deathtouch": ["deathtouch", "fight", "removal"],
    "Fight": ["deathtouch", "fight", "removal"],
    "Bite": ["deathtouch", "fight", "removal"],
    "Equipment/Aura synergy": ["voltron", "equipment", "aura"],
    "Double Strike": ["voltron", "equipment", "combat"],
    "First Strike": ["voltron", "combat"],
    "Trample": ["voltron", "combat", "counters"],
    "Direct damage": ["burn", "firebending", "removal"],
    "X-cost damage": ["burn", "firebending", "big mana"],
    "Ramp/mana acceleration": ["ramp", "mana", "big mana", "lands-matter"],
    "Graveyard recursion": ["graveyard", "recursion", "sacrifice", "lands-matter"],
    "Graveyard exchange": ["graveyard", "recursion"],
    "Mind control": ["theft", "control", "deathtouch"],
    "Cost Reduction": ["spellslinger", "big mana", "storm", "burn"],
    "Power doubling": ["voltron", "counters", "combat"],
    "Mill/Self-mill": ["graveyard", "self-mill"],
    "Treasure generation": ["ramp", "mana", "big mana", "spellslinger", "burn"],
    "Impulse draw": ["burn", "spellslinger", "card advantage", "firebending"],
    "Investigate": ["tokens", "card advantage", "sacrifice"],
    "Food token": ["sacrifice", "deathtouch", "tokens"],
    "Anthem": ["tokens", "go-wide", "combat", "voltron"],
    "Ward": ["protection", "control"],
    "Card draw": ["card advantage", "spellslinger"],
    "Removal": ["removal", "control"],
    "Flying": ["evasion"],
    "Lifelink": ["lifegain"],
    "Vigilance": ["combat"],
    "Protection/hexproof/indestructible": ["protection", "voltron"],
    "Tutor effect": ["toolbox", "consistency"],
    "Untap": ["combo", "value"],
    "Extra Turn": ["combo", "extra turns"],
    "Damage Redirection": ["burn", "enrage"],
    "Enrage": ["enrage", "fight"],
}


# ── Card helpers ────────────────────────────────────────────────────────

def get_art_crop_url(card: dict) -> str | None:
    if "image_uris" in card and card["image_uris"]:
        return card["image_uris"].get("art_crop")
    if "card_faces" in card and card["card_faces"]:
        face = card["card_faces"][0]
        if "image_uris" in face and face["image_uris"]:
            return face["image_uris"].get("art_crop")
    return None


def get_oracle_text(card: dict) -> str:
    if "oracle_text" in card:
        return card.get("oracle_text", "")
    if "card_faces" in card and card["card_faces"]:
        texts = [face.get("oracle_text", "") for face in card["card_faces"]]
        return "\n\n".join(filter(None, texts))
    return ""


def get_mana_cost(card: dict) -> str:
    if "mana_cost" in card and card["mana_cost"]:
        return card["mana_cost"]
    if "card_faces" in card and card["card_faces"]:
        costs = [face.get("mana_cost", "") for face in card["card_faces"]]
        return " // ".join(filter(None, costs))
    return ""


# ── Mechanic tagger ────────────────────────────────────────────────────

def tag_mechanics(oracle_text: str, keywords: list[str], type_line: str, mana_cost: str = "") -> list[str]:
    tags: set[str] = set()
    t = oracle_text.lower()
    kw = [k.lower() for k in keywords]
    tl = type_line.lower()
    mc = mana_cost.lower()

    # X-cost damage
    if re.search(r"deals?.*x.*damage|times x damage|deals? .* times .* damage", t):
        tags.add("X-cost damage")
    if "{x}" in mc and re.search(r"damage|destroy|exile", t):
        tags.add("X-cost damage")

    # Power doubling
    if re.search(r"double.*power|power.*double|double.*toughness|"
                 r"base power and toughness \d+/\d+", t):
        tags.add("Power doubling")

    # ETB interaction
    if re.search(r"whenever a (permanent|creature|artifact|enchantment) enter(s|ing)|"
                 r"enters (the battlefield|under)|"
                 r"entering (the battlefield )?cause|"
                 r"enters.*trigger", t):
        tags.add("ETB interaction")

    # Theft / Mind control
    if re.search(r"gain control of (target|up to|each|all)|"
                 r"gains control of|"
                 r"exchange control|"
                 r"put .* onto the battlefield under your control", t):
        tags.add("Mind control")

    # Mill / Self-mill
    if re.search(r"\bmill\b|put the top .* card.*into.*graveyard|"
                 r"each player.*mill|surveil", t):
        tags.add("Mill/Self-mill")
    if "mill" in kw or "surveil" in kw:
        tags.add("Mill/Self-mill")

    # Ward
    if "ward" in kw or re.search(r"\bward\b", t):
        tags.add("Ward")

    # Treasure generation
    if re.search(r"create .* treasure|treasure token", t):
        tags.add("Treasure generation")

    # Impulse draw
    if re.search(r"exile .* top .* (card|cards) .* (play|cast)|"
                 r"exile .* from the top .* (play|cast)|"
                 r"exile that many cards .* (play|cast)|"
                 r"exile .* you may (play|cast)", t):
        tags.add("Impulse draw")

    # -1/-1 counters
    if re.search(r"-1/-1 counter|put a -1/-1|\bwither\b", t):
        tags.add("-1/-1 counters")
    if "wither" in kw or "infect" in kw:
        tags.add("-1/-1 counters")

    # Counter Doubling
    if re.search(r"plus one of each|double the number of.*counter|twice that many.*counter|"
                 r"that many plus one|if .* would (put|place).*counter.*puts? .* (that many|twice)", t):
        tags.add("Counter Doubling")

    # Power-Based Mana
    if re.search(r"add .* mana.*where x is.*power|add x mana.*power|"
                 r"where x is .*(its |this creature.s |.*'s )?power", t):
        tags.add("Power-Based Mana")

    # Damage Redirection
    if re.search(r"(is dealt damage|dealt damage).*deal.*(that much|damage equal)", t):
        tags.add("Damage Redirection")

    # Enrage
    if re.search(r"whenever .* is dealt damage", t) or "enrage" in kw:
        tags.add("Enrage")

    # Cast Restriction
    if re.search(r"can't cast (spells|instants|sorceries) during|"
                 r"opponents can't cast.*during your turn", t):
        tags.add("Cast Restriction")

    # Cost Reduction
    if re.search(r"cost(s)? \{?\d+\}? less|costs? .* less to cast|"
                 r"without paying (its |their |the )?mana cost|"
                 r"reduce the cost|you may cast .* without paying|"
                 r"affinity for", t):
        tags.add("Cost Reduction")
    if "convoke" in kw or "affinity" in kw or "delve" in kw:
        tags.add("Cost Reduction")

    # Copy Spell
    if re.search(r"copy (it|that spell|target .* spell)|copies? of .* spell|"
                 r"when you next cast an instant or sorcery.*copy", t):
        tags.add("Copy Spell")

    # Extra Turn
    if re.search(r"extra turn|additional turn after this one", t):
        tags.add("Extra Turn")

    # Proliferate
    if "proliferate" in t or "proliferate" in kw:
        tags.add("Proliferate")

    # Cast Trigger
    if re.search(r"whenever you cast", t):
        tags.add("Cast Trigger")

    # Magecraft
    if re.search(r"whenever you cast or copy an instant or sorcery|magecraft", t):
        tags.add("Magecraft")
    if "magecraft" in kw:
        tags.add("Magecraft")

    # Instant/Sorcery matters
    if re.search(r"instant(s)? (and|or) sorcer(y|ies)|"
                 r"noncreature spell|"
                 r"for each instant and sorcery|"
                 r"instant or sorcery card in your graveyard|"
                 r"whenever you cast .* instant or sorcery", t):
        tags.add("Instant/Sorcery matters")

    # Tribal Token
    if re.search(r"whenever you cast.*(hero|villain|creature).*create.*token|"
                 r"create.*(hero|villain).*token", t):
        tags.add("Tribal Token")

    # Untap
    if re.search(r"untap (all |another |target |each |it|that|those|up to )", t):
        tags.add("Untap")

    # Group Punishment
    if re.search(r"each opponent (loses|sacrifices|discards|mills|takes)|"
                 r"each opponent loses \d+ life", t):
        tags.add("Group Punishment")

    # Phase out / Blink
    if re.search(r"phase(s)? out|phased out", t):
        tags.add("Blink/flicker")

    # Double strike in oracle text
    if "double strike" in t:
        tags.add("Double Strike")

    # ETB triggers
    if re.search(r"when .* enters", t):
        tags.add("ETB trigger")

    # Death triggers
    if re.search(r"when(ever)? .* dies", t):
        tags.add("Death trigger")

    # Attack triggers
    if re.search(r"whenever .* attacks", t):
        tags.add("Attack trigger")

    # Token generation
    if "create" in t and "token" in t:
        tags.add("Token generation")

    # Card draw
    if "draw" in t and "card" in t:
        tags.add("Card draw")

    # Removal
    if any(word in t for word in ["destroy target", "exile target", "destroy all", "exile all"]):
        tags.add("Removal")
    if re.search(r"deals? \d+ damage to (target|any|each)", t):
        tags.add("Removal")

    # +1/+1 counters
    if re.search(r"\+1/\+1 counter|put a \+1/\+1|"
                 r"power-up.*put.*counter|connive|"
                 r"distribute .* \+1/\+1", t):
        tags.add("+1/+1 counters")

    # Sacrifice outlets
    if re.search(r"sacrifice (a |an |another |target )?(creature|artifact|permanent|enchantment|token)|"
                 r"as an additional cost.*sacrifice", t):
        tags.add("Sacrifice Outlet")

    # Blink/flicker
    if re.search(r"exile .* return .* battlefield|"
                 r"exile .* then return|"
                 r"return .* from exile .* battlefield|"
                 r"flicker", t):
        tags.add("Blink/flicker")

    # Equipment/Aura synergy
    if re.search(r"equip|equipped creature|equip cost|"
                 r"whenever .* equip|attach .* to|attached to|"
                 r"reconfigure", t):
        tags.add("Equipment/Aura synergy")
    if "equipment" in tl or "aura" in tl:
        tags.add("Equipment/Aura synergy")

    # Deathtouch / Fight / Bite
    if "deathtouch" in kw or "deathtouch" in t:
        tags.add("Deathtouch")
    if re.search(r"\bfight\b|fights? target", t):
        tags.add("Fight")
    if re.search(r"deals damage equal to .* power", t):
        tags.add("Bite")

    # Ramp/mana acceleration
    if re.search(r"add \{|search your library for a .* land|land onto the battlefield|"
                 r"add .* mana|add one mana|add x mana", t):
        tags.add("Ramp/mana acceleration")
    if "search your library for" in t and ("basic land" in t or "land card" in t):
        tags.add("Ramp/mana acceleration")

    # Graveyard recursion
    if "graveyard" in t and any(w in t for w in ["return", "cast", "to your hand"]):
        tags.add("Graveyard recursion")
    if re.search(r"from .*graveyard.*onto the battlefield|"
                 r"graveyard onto the battlefield", t):
        tags.add("Graveyard recursion")

    # Graveyard exchange
    if re.search(r"exchange your hand and graveyard|"
                 r"return all .* cards from your graveyard|"
                 r"return .* cards from your graveyard to your hand", t):
        tags.add("Graveyard exchange")

    # Protection/hexproof/indestructible
    if any(w in kw for w in ["hexproof", "indestructible", "protection"]):
        tags.add("Protection/hexproof/indestructible")
    if any(w in t for w in ["hexproof", "indestructible", "protection from"]):
        tags.add("Protection/hexproof/indestructible")

    # Tutor effects
    if "search your library" in t and "land" not in t:
        tags.add("Tutor effect")

    # Copy effects (general)
    if re.search(r"becomes? a copy|copy of .* creature|copy target", t):
        tags.add("Copy effect")

    # Direct damage
    if re.search(r"deals? \d+ damage", t):
        tags.add("Direct damage")

    # Learn (STX)
    if re.search(r"\blearn\b", t):
        tags.add("Learn")

    # Lesson (STX)
    if "lesson" in tl:
        tags.add("Lesson")

    # Combat keywords
    if "trample" in kw:
        tags.add("Trample")
    if "flying" in kw:
        tags.add("Flying")
    if "first strike" in kw:
        tags.add("First Strike")
    if "double strike" in kw:
        tags.add("Double Strike")
    if "vigilance" in kw:
        tags.add("Vigilance")
    if "lifelink" in kw:
        tags.add("Lifelink")
    if "menace" in kw:
        tags.add("Menace")
    if "reach" in kw:
        tags.add("Reach")
    if "haste" in kw:
        tags.add("Haste")

    # Adventure
    if "adventure" in kw or "adventure" in tl:
        tags.add("Adventure")

    # Food tokens
    if "food" in t and ("create" in t or "token" in t):
        tags.add("Food token")

    # Investigate / Clue tokens
    if "investigate" in t or "clue" in t:
        tags.add("Investigate")

    # Anthem
    if re.search(r"creatures you control get \+\d+/\+\d+|"
                 r"other creatures you control get \+|"
                 r"creatures you control have", t):
        tags.add("Anthem")

    # Landfall
    if re.search(r"whenever a land enters|landfall", t):
        tags.add("Landfall")

    return sorted(tags)


def process_card(card: dict) -> dict:
    type_line = card.get("type_line", "")
    oracle_text = get_oracle_text(card)
    keywords = card.get("keywords", [])
    mana_cost = get_mana_cost(card)

    power = card.get("power")
    toughness = card.get("toughness")
    power_toughness = f"{power}/{toughness}" if power and toughness else None

    return {
        "name": card.get("name"),
        "tags": tag_mechanics(oracle_text, keywords, type_line, mana_cost),
        "type_line": type_line,
        "mana_cost": mana_cost,
        "cmc": card.get("cmc"),
        "color_identity": card.get("color_identity", []),
        "oracle_text": oracle_text,
        "art_crop": get_art_crop_url(card),
        "scryfall_uri": card.get("scryfall_uri"),
        "price_usd": card.get("prices", {}).get("usd"),
        "power_toughness": power_toughness,
        "keywords": keywords,
        "rarity": card.get("rarity"),
        "set": card.get("set"),
    }


# ── Scoring functions ──────────────────────────────────────────────────

def parse_color_identity(color_str: str) -> set[str]:
    match = re.match(r"([WUBRG]+)", color_str)
    return set(match.group(1)) if match else set()


def card_fits_color_identity(card_colors: list[str], deck_colors: set[str]) -> bool:
    if not card_colors:
        return True
    return set(card_colors).issubset(deck_colors)


def compute_tag_strategy_overlap(card_tags: list[str], strategy_keywords: list[str]) -> tuple[float, list[str]]:
    """Score how well a card's tags align with a deck's strategy via the synonym layer."""
    kw_set = set(k.lower() for k in strategy_keywords)
    if not kw_set:
        return 0.0, []

    score = 0.0
    matches = []

    for tag in card_tags:
        synonyms = TAG_STRATEGY_SYNONYMS.get(tag, [])
        overlap = set(synonyms) & kw_set
        if overlap:
            tag_score = len(overlap) * 1.5
            score += tag_score
            matches.append(f"{tag}->{','.join(sorted(overlap))}")

    return score, matches


def score_card_for_deck(card: dict, deck: dict) -> tuple[float, list[str], str]:
    """Score a card's fit for a deck. Returns (score, match_reasons, why_chase)."""
    score = 0.0
    reasons: list[str] = []

    oracle = card.get("oracle_text", "").lower()
    primary_strategy = deck.get("primary_strategy", "").lower()
    synergy_kw = [kw.lower() for kw in deck.get("synergy_keywords", [])]

    # 1. Tag→Strategy synonym scoring
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

    # 3. Strategy-specific deep patterns
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
        if "deathtouch" in oracle or "deathtouch" in [k.lower() for k in card.get("keywords", [])]:
            score += 3.5
            reasons.append("Has deathtouch")
        if re.search(r"\bfight\b", oracle):
            score += 3.5
            reasons.append("Fight effect")
        if "gain control" in oracle:
            score += 2.5
            reasons.append("Theft synergy")

    if "spellslinger" in primary_strategy:
        if re.search(r"magecraft|whenever you cast or copy an instant or sorcery", oracle):
            score += 5.0
            reasons.append("Magecraft / spellslinger trigger")
        if "instant" in oracle and "sorcery" in oracle and "whenever" in oracle:
            score += 3.0
            reasons.append("Instant/sorcery trigger")
        if re.search(r"prowess", oracle) or "prowess" in [k.lower() for k in card.get("keywords", [])]:
            score += 2.5
            reasons.append("Has prowess")
        type_line = card.get("type_line", card.get("card_type", "")).lower()
        if "instant" in type_line or "sorcery" in type_line:
            score += 2.0
            reasons.append("Is instant/sorcery")
            mc = card.get("mana_cost") or ""
            if "{x}" in mc.lower():
                score += 2.0
                reasons.append("X-cost instant/sorcery")
        if "treasure" in oracle:
            score += 1.5
            reasons.append("Treasure generation for spell fuel")
        if re.search(r"exile .* (play|cast)", oracle):
            score += 1.5
            reasons.append("Impulse draw for card advantage")

    if "blink" in primary_strategy or "etb" in primary_strategy:
        if re.search(r"exile .* return .* battlefield|flicker", oracle):
            score += 5.0
            reasons.append("Blink/flicker effect")
        if re.search(r"when .* enters", oracle):
            score += 2.5
            reasons.append("ETB trigger")
        if re.search(r"whenever .* entering|enters.*trigger", oracle):
            score += 3.0
            reasons.append("ETB interaction/synergy")

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
        if re.search(r"each opponent (loses|sacrifices|discards)", oracle):
            score += 2.5
            reasons.append("Group punishment / drain")
        if "persist" in [k.lower() for k in card.get("keywords", [])] or "undying" in [k.lower() for k in card.get("keywords", [])]:
            score += 4.0
            reasons.append("Has persist/undying")

    if "burn" in primary_strategy or "firebending" in primary_strategy:
        mc = card.get("mana_cost") or ""
        if "{x}" in mc.lower():
            score += 4.0
            reasons.append("X-cost spell for big mana burn")
        if re.search(r"deals? \d+ damage|deals?.*x.*damage", oracle):
            score += 2.5
            reasons.append("Direct damage")
        if "add" in oracle and "mana" in oracle:
            score += 2.0
            reasons.append("Mana generation")
        if "treasure" in oracle:
            score += 2.0
            reasons.append("Treasure for mana acceleration")
        if re.search(r"cost.*less|without paying", oracle):
            score += 2.5
            reasons.append("Cost reduction for big spells")
        if re.search(r"exile .* (play|cast)", oracle):
            score += 1.5
            reasons.append("Impulse draw")

    if "voltron" in primary_strategy or "equipment" in primary_strategy:
        type_line = card.get("type_line", card.get("card_type", "")).lower()
        if "equipment" in type_line:
            score += 5.0
            reasons.append("Is equipment")
        if "equip" in oracle or "equipped creature" in oracle:
            score += 3.0
            reasons.append("Equipment synergy")
        if "double strike" in oracle or "double strike" in [k.lower() for k in card.get("keywords", [])]:
            score += 3.5
            reasons.append("Double strike for voltron")
        if "trample" in [k.lower() for k in card.get("keywords", [])]:
            score += 1.0
            reasons.append("Has trample")
        if "indestructible" in oracle or "hexproof" in oracle:
            score += 2.0
            reasons.append("Protection for commander")
        if re.search(r"creatures you control get \+\d+/\+\d+", oracle):
            score += 2.0
            reasons.append("Anthem buffs commander")

    if "lesson" in primary_strategy:
        type_line = card.get("type_line", card.get("card_type", "")).lower()
        if "lesson" in type_line:
            score += 4.0
            reasons.append("Is a Lesson spell")
        if "learn" in oracle:
            score += 3.0
            reasons.append("Has Learn")

    # 4. Small bonuses
    rarity = card.get("rarity", "")
    if rarity == "mythic":
        score += 0.3
    elif rarity == "rare":
        score += 0.15

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
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline_version": "v4-extracted",
        "card_pool_size": len(cards),
        "min_score_threshold": min_score,
        "decks": [],
    }

    all_recs = []

    for deck in decks:
        deck_colors = parse_color_identity(deck.get("color_identity", ""))
        deck_name = deck.get("deck_name", "")

        valid_cards = [c for c in cards if card_fits_color_identity(c.get("color_identity", []), deck_colors)]

        scored = []
        for card in valid_cards:
            tags = card.get("tags", card.get("mechanic_tags", []))
            if card.get("is_land", False) or ("Land" in card.get("type_line", card.get("card_type", ""))):
                if not any(t in tags for t in ["Ramp/mana acceleration", "Landfall", "ETB trigger"]):
                    continue

            score, match_reasons, why_chase = score_card_for_deck(card, deck)
            if score >= min_score:
                scored.append({
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
                })

        scored.sort(key=lambda x: x["score"], reverse=True)

        results["decks"].append({
            "deck_name": deck_name,
            "commander": deck.get("commander", ""),
            "color_identity": deck.get("color_identity", ""),
            "primary_strategy": deck.get("primary_strategy", ""),
            "recommendations": scored,
            "recommendation_count": len(scored),
        })

        for s in scored:
            all_recs.append({"card": s["card_name"], "deck": deck_name})

    card_counts = Counter(r["card"] for r in all_recs)
    results["summary"] = {
        "total_recommendations": len(all_recs),
        "unique_cards": len(card_counts),
        "most_recommended": [{"card": c, "deck_count": n} for c, n in card_counts.most_common(10)],
    }

    return results


# ── CLI commands ───────────────────────────────────────────────────────

def _get_cache():
    script_dir = Path(__file__).parent
    sys.path.insert(0, str(script_dir))
    from scryfall_cache import ScryfallCache
    return ScryfallCache()


@app.command()
def tag_card(name: str) -> None:
    """Tag a single card's mechanics (fetches from Scryfall via cache)."""
    cache = _get_cache()
    card = cache.get_card(name)
    if not card:
        typer.echo(f"Not found: {name}", err=True)
        raise typer.Exit(1)
    result = process_card(card)
    typer.echo(json.dumps(result, indent=2))


@app.command()
def tag_set(
    code: str,
    output: Path = typer.Option(None, "--output", "-o"),
) -> None:
    """Tag all cards in a set."""
    cache = _get_cache()
    cards = cache.get_set(code)
    processed = [process_card(c) for c in cards]

    result = {
        "tagged_at": datetime.now(timezone.utc).isoformat(),
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
    typer.echo(f"Zero-tag cards: {zero} ({zero / len(processed) * 100:.1f}%)", err=True)


@app.command()
def tag_file(
    input_path: Path = typer.Argument(..., help="JSON file with Scryfall card objects"),
    output: Path = typer.Option(None, "--output", "-o"),
) -> None:
    """Tag cards from a JSON input file."""
    data = json.loads(input_path.read_text())
    cards = data.get("cards", data) if isinstance(data, dict) else data
    processed = [process_card(c) for c in cards]

    result = {
        "tagged_at": datetime.now(timezone.utc).isoformat(),
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
