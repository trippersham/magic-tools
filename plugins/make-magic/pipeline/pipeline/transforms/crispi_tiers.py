"""Curated named-card tier tables for the CRISPI scorer (static, human-auditable).

The CRISPI rubric (``research/crispi-deep-dive-full.txt``) names specific cards as
the canonical example of each tier — Demonic Tutor is the archetypal premium
tutor, Rhystic Study the archetypal premium-asymmetric draw engine, Toxic Deluge
the archetypal premium hard-scope board wipe. These named cards are the
**alignment anchors** to DeckCheck's battle-tested numbers: encoding them lets
our engine track DeckCheck's tiering wherever the rubric is explicit, and the
structured heuristics (Phase 1) only fill in the cards the rubric does NOT name.

Three lookup tables, each mapping a NORMALIZED card name to
``(tier_label, points)``:

    - ``TUTOR_TIERS``       — the Consistency tutor column (rubric "Tutor Density").
    - ``DRAW_TIERS``        — the Consistency draw column (rubric "Draw Engine Sources").
    - ``INTERACTION_TIERS`` — the Interaction stack/timing points (rubric "Stack/Timing Points").

The ``points`` are the rubric's per-card contribution to THAT column's total:
tutor/draw points are the tier's face value; interaction points are the STACK
delta a card adds (a counterspell adds 2, a premium instant adds +1, a hard-scope
wipe adds +1). The ladder math that turns a column total into a 1-10 axis lives in
Phase 2 (``transforms/crispi.py``) — this module is static data only.

Keys are normalized at rest (see :func:`normalize_card_name`) so a lookup is O(1)
and the query is normalized the same way. Look a card up with :func:`tier_for`;
an unnamed card returns ``None`` (the caller then falls to heuristics).

Three name-list frozensets — ``GAME_CHANGERS``, ``MASS_LAND_DENIAL``,
``EXTRA_TURNS`` — supply the Commander-Bracket rule signals consumed in Phase 6.

``GAME_CHANGERS`` is the AUTHORITATIVE live WotC Game Changers list, verified in
Phase 6 (2026-08-14) against Scryfall's ``is:gamechanger`` query (``total_cards``
53, ``has_more`` False) and cross-checked with the commanderbrackets.com listing.
It reflects the WotC updates through 2026-02-09 (which added Farewell and unbanned
Biorhythm directly onto the list). This list is actively churned by the Commander
Format Panel, so re-verify against ``is:gamechanger`` on each future bracket-rubric
pass. See the reconciliation note above the frozenset for what changed vs the
original Phase-0 scaffold.

``MASS_LAND_DENIAL`` and ``EXTRA_TURNS`` remain CURATED lists (WotC publishes no
single canonical enumeration of either — the bracket rules describe the CATEGORY,
not a card list). They carry the format-defining, uncontroversial members; treat
them as high-coverage but not provably exhaustive.
"""

from __future__ import annotations

import re
from typing import Final

# --------------------------------------------------------------------------- #
# Name normalization — lowercase, strip punctuation, collapse whitespace.
#
# Card names carry apostrophes ("Green Sun's Zenith"), hyphens ("Cold-Eyed
# Selkie"), and commas ("Tatyova, Benthic Druid"). Normalizing the key AND the
# query the same way makes the lookup tolerant of a caller's punctuation/case
# variance without a per-card alias list.
# --------------------------------------------------------------------------- #

#: Apostrophes are ELIDED (Green Sun's -> green suns); every other punctuation
#: char becomes a SPACE (Cold-Eyed -> cold eyed) so a hyphen and a space agree.
_APOS_RE: Final = re.compile("['\u2019]")
_PUNCT_RE: Final = re.compile(r'[^\w\s]')
_WS_RE: Final = re.compile(r'\s+')


def normalize_card_name(name: str) -> str:
    """Normalize a card name for tier lookup: casefold, drop punctuation, collapse space.

    Idempotent: ``normalize_card_name(normalize_card_name(x)) == normalize_card_name(x)``.
    So ``"Green Sun's Zenith"``, ``"green suns zenith"``, and ``"GREEN  SUNS ZENITH"``
    all normalize to the same key. Punctuation is replaced with a SPACE (not
    dropped) so a hyphen and a space agree: ``"Cold-Eyed Selkie"`` and
    ``"Cold Eyed Selkie"`` both normalize to ``"cold eyed selkie"``.
    """
    elided = _APOS_RE.sub('', name)
    despaced = _PUNCT_RE.sub(' ', elided)
    collapsed = _WS_RE.sub(' ', despaced)
    return collapsed.strip().casefold()


def _norm_table(raw: dict[str, tuple[str, int]]) -> dict[str, tuple[str, int]]:
    """Normalize a raw ``display-name -> (label, points)`` table's keys at import."""
    return {normalize_card_name(name): tier for name, tier in raw.items()}


def _norm_set(names: frozenset[str]) -> frozenset[str]:
    """Normalize a raw name-list's members at import."""
    return frozenset(normalize_card_name(name) for name in names)


def tier_for(name: str, table: dict[str, tuple[str, int]]) -> tuple[str, int] | None:
    """Look ``name`` up in a normalized tier ``table``; ``None`` if not named.

    The query is normalized the same way the table keys were, so case and
    punctuation do not matter. A miss is an honest "the rubric doesn't name this
    card" signal — the caller falls back to structured heuristics, not a guess.
    """
    return table.get(normalize_card_name(name))


# --------------------------------------------------------------------------- #
# TUTOR_TIERS — the Consistency tutor column (rubric "Tutor Density").
#
# Tier labels and points are the rubric's, verbatim:
#   premium (6)           — premium true tutors, CMC <=2, unrestricted.
#   repeatable-engine (6) — a permanent that tutors every turn, unconditionally.
#   combat-tutor (4)      — attack-trigger tutor engines (body must connect).
#   standard (4)          — standard true tutors, CMC 3-4 or restricted.
#   combo-enabler (4)     — combo-enablers-that-tutor.
#   graveyard-tutor (4)   — graveyard-destination tutors; the engine gates them
#                           to 0 without a recursion package (that gate is Phase 1
#                           logic; the tier here is the tutor's face value).
# Narrow / expensive true tutors (CMC 5+) score 2 but the rubric names no such
# card, so they resolve via the Phase-1 CMC heuristic, not this table.
# --------------------------------------------------------------------------- #

TUTOR_TIERS: Final[dict[str, tuple[str, int]]] = _norm_table(
    {
        # 6pt — premium true tutors, CMC <=2, unrestricted.
        'Demonic Tutor': ('premium', 6),
        'Vampiric Tutor': ('premium', 6),
        'Imperial Seal': ('premium', 6),
        'Enlightened Tutor': ('premium', 6),
        'Mystical Tutor': ('premium', 6),
        'Worldly Tutor': ('premium', 6),
        'Gamble': ('premium', 6),
        # 6pt — repeatable tutor engines (tutor every turn, unconditionally).
        'Survival of the Fittest': ('repeatable-engine', 6),
        'Birthing Pod': ('repeatable-engine', 6),
        'Fauna Shaman': ('repeatable-engine', 6),
        # 4pt — combat-conditioned tutor engines (attack triggers).
        'Zur the Enchanter': ('combat-tutor', 4),
        'Armored Skyhunter': ('combat-tutor', 4),
        # 4pt — standard true tutors, CMC 3-4 or restricted.
        'Grim Tutor': ('standard', 4),
        'Wishclaw Talisman': ('standard', 4),
        'Fabricate': ('standard', 4),
        'Eldritch Evolution': ('standard', 4),
        'Finale of Devastation': ('standard', 4),
        "Green Sun's Zenith": ('standard', 4),
        # 4pt — combo-enablers-that-tutor.
        'Demonic Consultation': ('combo-enabler', 4),
        'Tainted Pact': ('combo-enabler', 4),
        'Doomsday': ('combo-enabler', 4),
        # 4pt tier — graveyard-destination tutors (gated on a recursion package
        # in Phase 1; without one they score 0). The 4 here is the ungated face.
        'Entomb': ('graveyard-tutor', 4),
        'Buried Alive': ('graveyard-tutor', 4),
    }
)

# --------------------------------------------------------------------------- #
# DRAW_TIERS — the Consistency draw column (rubric "Draw Engine Sources").
#
#   burst (6)              — burst draw.
#   premium-asymmetric (5) — premium asymmetric / opponent-inevitable engines.
#   standard-repeatable (4)— engines off normal game actions (landfall, cast).
#   combat-conditioned (3) — draw gated on combat (own attack / connect / political).
#   selection (3)          — card selection / filtering (counts toward draw <=30).
#   one-shot (2)           — one-shot draw incl. ETB / sac-itself.
#   symmetric (2)          — symmetric engines (opponents draw first).
#
# Opponent-draw PUNISHERS (Sheoldred / Orcish Bowmasters / Underworld Dreams) are
# NOT here — the rubric scores them as Interaction, never as draw sources. They
# are absent so ``tier_for(..., DRAW_TIERS)`` returns None for them.
# --------------------------------------------------------------------------- #

DRAW_TIERS: Final[dict[str, tuple[str, int]]] = _norm_table(
    {
        # 6pt — burst draw.
        'Ad Nauseam': ('burst', 6),
        'Wheel of Fortune': ('burst', 6),
        'Necropotence': ('burst', 6),
        # 5pt — premium asymmetric engines (+ premium opponent-inevitable).
        'Rhystic Study': ('premium-asymmetric', 5),
        'Mystic Remora': ('premium-asymmetric', 5),
        'Esper Sentinel': ('premium-asymmetric', 5),
        'Sylvan Library': ('premium-asymmetric', 5),
        'Consecrated Sphinx': ('premium-asymmetric', 5),
        # 4pt — standard repeatable engines (fire off normal game actions).
        'Tatyova, Benthic Druid': ('standard-repeatable', 4),
        'Beast Whisperer': ('standard-repeatable', 4),
        # 3pt — combat-conditioned draw.
        'Cold-Eyed Selkie': ('combat-conditioned', 3),
        'Toski, Bearer of Secrets': ('combat-conditioned', 3),
        'Breena, the Demagogue': ('combat-conditioned', 3),
        'Mangara, the Diplomat': ('combat-conditioned', 3),
        # 3pt — card selection / filtering.
        'Brainstorm': ('selection', 3),
        'Ponder': ('selection', 3),
        'Preordain': ('selection', 3),
        "Sensei's Divining Top": ('selection', 3),
        # 2pt — one-shot draw (ETB / sac-itself; "one card, once").
        'Prime Speaker Zegana': ('one-shot', 2),
        "Commander's Sphere": ('one-shot', 2),
        # 2pt — one-shot draw (non-repeatable "draw N").
        "Night's Whisper": ('one-shot', 2),
        'Read the Bones': ('one-shot', 2),
        # 2pt — symmetric engines (three opponents drink first).
        'Howling Mine': ('symmetric', 2),
        'Temple Bell': ('symmetric', 2),
        'Dictate of Kruphix': ('symmetric', 2),
    }
)

# --------------------------------------------------------------------------- #
# INTERACTION_TIERS — the Interaction STACK/TIMING points (rubric names these
# per class). ``points`` is the STACK delta the card contributes:
#
#   counterspell (2)     — every counterspell (incl. "effective counterspells").
#   free (2)             — every free / alternate-cost reactive spell (also covers
#                          free protection and free removal).
#   turn-protection (2)  — protects your own turn (Silence / Grand Abolisher class).
#   premium-instant (1)  — premium instant-speed removal: the +1 instant-speed
#                          credit (it also counts in Total Interaction upstream).
#   hard-scope-wipe (1)  — premium HARD-scope board wipe: the +1 stack credit
#                          (exile-all / each-sacrifices / -X toughness at CMC <=4).
#
# NOTE: a card can qualify under several classes at once (a free COUNTERspell is
# both free (2) and counterspell (2), plus +1 instant); this table encodes the
# single rubric-named CLASS. Composing the stack deltas across classes is Phase 2
# ladder logic, not this static table. Destroy-wipes (Wrath of God class) earn 0
# stack points, so they are deliberately absent.
# --------------------------------------------------------------------------- #

INTERACTION_TIERS: Final[dict[str, tuple[str, int]]] = _norm_table(
    {
        # 2pt — effective counterspells.
        'Veil of Summer': ('counterspell', 2),
        "Autumn's Veil": ('counterspell', 2),
        "Imp's Mischief": ('counterspell', 2),
        'Bolt Bend': ('counterspell', 2),
        'Pyroblast': ('counterspell', 2),
        # 2pt — free interaction spells (0-mana / alternate-cost reactive),
        # covering free protection and free removal per the rubric.
        'Force of Will': ('free', 2),
        'Fierce Guardianship': ('free', 2),
        'Deflecting Swat': ('free', 2),
        'Flawless Maneuver': ('free', 2),
        'Deadly Rollick': ('free', 2),
        'Snuff Out': ('free', 2),
        # 2pt — turn-protection effects (protect your own turn).
        'Silence': ('turn-protection', 2),
        'Grand Abolisher': ('turn-protection', 2),
        # +1 — premium instant-speed removal.
        'Swords to Plowshares': ('premium-instant', 1),
        'Path to Exile': ('premium-instant', 1),
        "Assassin's Trophy": ('premium-instant', 1),
        # +1 — premium HARD-scope board wipe.
        'Toxic Deluge': ('hard-scope-wipe', 1),
        'Culling Ritual': ('hard-scope-wipe', 1),
    }
)


# --------------------------------------------------------------------------- #
# Commander-Bracket rule-signal name lists (Phase 6).
#
# The captured brackets doc lists the RULES (no Game Changers, no MLD, no extra
# turns, 2-card-combo limits) but names no specific cards. GAME_CHANGERS below is
# the AUTHORITATIVE live WotC list (verified 2026-08-14 vs Scryfall
# `is:gamechanger`; 53 cards). MLD / extra-turn lists stay curated (no canonical
# WotC enumeration exists). Members are normalized so a membership test matches
# `tier_for` keys and a deck-name lookup.
# --------------------------------------------------------------------------- #

#: The official WotC "Game Changers" list — VERIFIED authoritative (2026-08-14).
#:
#: Source of truth: Scryfall `is:gamechanger` (total_cards 53, has_more False),
#: cross-checked with commanderbrackets.com. Reflects WotC updates through
#: 2026-02-09. Re-verify on each bracket-rubric pass (the Format Panel churns it).
#:
#: Reconciliation vs the Phase-0 scaffold: this replaces a 40-card guess.
#:  * ADDED (were missing): Biorhythm, Bolas's Citadel, Chrome Mox, Consecrated
#:    Sphinx, Crop Rotation, Farewell, Field of the Dead, Gifts Ungiven, Glacial
#:    Chasm, Grand Arbiter Augustin IV, Humility, Intuition, Jeska's Will, Lion's
#:    Eye Diamond, Mishra's Workshop, Mox Diamond, Mystical Tutor, Narset Parter
#:    of Veils, Natural Order, Necropotence, Ad Nauseam, Orcish Bowmasters, Serra's
#:    Sanctum, Survival of the Fittest, Teferi's Protection, Tergrid God of Fright,
#:    The Tabernacle at Pendrell Vale, Worldly Tutor.
#:  * REMOVED (scaffold guesses NOT on the live list): Mana Crypt (banned),
#:    Jeweled Lotus (banned), Mystic Remora, Smothering Tithe*, Grim Monolith,
#:    Deflecting Swat (delisted 2025-10), Grim Tutor, Tainted Pact, Demonic
#:    Consultation, Jin-Gitaxias Core Augur (delisted), Hullbreacher (banned),
#:    Trinisphere (delisted), Winota (delisted), Yuriko (delisted), Kinnan
#:    (delisted), Yawgmoth, Food Chain (delisted), Panoptic Mirror -> IS on list;
#:    Seedborn Muse -> IS on list. (Smothering Tithe/Panoptic Mirror/Seedborn Muse
#:    were correct guesses and remain.) DFC entry stored by front-face name
#:    ("Tergrid, God of Fright") to match deck-list naming.
GAME_CHANGERS: Final[frozenset[str]] = _norm_set(
    frozenset(
        {
            'Ad Nauseam',
            'Ancient Tomb',
            'Aura Shards',
            'Biorhythm',
            "Bolas's Citadel",
            'Braids, Cabal Minion',
            'Chrome Mox',
            'Coalition Victory',
            'Consecrated Sphinx',
            'Crop Rotation',
            'Cyclonic Rift',
            'Demonic Tutor',
            'Drannith Magistrate',
            'Enlightened Tutor',
            'Farewell',
            'Field of the Dead',
            'Fierce Guardianship',
            'Force of Will',
            "Gaea's Cradle",
            'Gamble',
            'Gifts Ungiven',
            'Glacial Chasm',
            'Grand Arbiter Augustin IV',
            'Grim Monolith',
            'Humility',
            'Imperial Seal',
            'Intuition',
            "Jeska's Will",
            "Lion's Eye Diamond",
            'Mana Vault',
            "Mishra's Workshop",
            'Mox Diamond',
            'Mystical Tutor',
            'Narset, Parter of Veils',
            'Natural Order',
            'Necropotence',
            'Notion Thief',
            'Opposition Agent',
            'Orcish Bowmasters',
            'Panoptic Mirror',
            'Rhystic Study',
            'Seedborn Muse',
            "Serra's Sanctum",
            'Smothering Tithe',
            'Survival of the Fittest',
            "Teferi's Protection",
            'Tergrid, God of Fright',
            "Thassa's Oracle",
            'The One Ring',
            'The Tabernacle at Pendrell Vale',
            'Underworld Breach',
            'Vampiric Tutor',
            'Worldly Tutor',
        }
    )
)

#: Starter subset of Mass Land Denial cards. Not an otag bucket; small curated
#: list, finalized in Phase 6. INCOMPLETE by design.
MASS_LAND_DENIAL: Final[frozenset[str]] = _norm_set(
    frozenset(
        {
            'Armageddon',
            'Ravages of War',
            'Catastrophe',
            'Cataclysm',
            'Decree of Annihilation',
            'Jokulhaups',
            'Obliterate',
            'Wildfire',
            'Boom // Bust',
            'Impending Disaster',
            'Winter Orb',
            'Static Orb',
            'Blood Moon',
            'Back to Basics',
            'Sunder',
        }
    )
)

#: Starter subset of extra-turn cards. Not an otag bucket; small curated list,
#: finalized in Phase 6. INCOMPLETE by design.
EXTRA_TURNS: Final[frozenset[str]] = _norm_set(
    frozenset(
        {
            'Time Warp',
            'Temporal Manipulation',
            'Capture of Jingzhou',
            'Time Stretch',
            'Walk the Aeons',
            'Temporal Mastery',
            'Nexus of Fate',
            'Time Sieve',
            'Expropriate',
            "Alrund's Epiphany",
            "Karn's Temporal Sundering",
            'Beacon of Tomorrows',
            'Part the Waterveil',
            'Seedtime',
        }
    )
)


__all__ = (
    'DRAW_TIERS',
    'EXTRA_TURNS',
    'GAME_CHANGERS',
    'INTERACTION_TIERS',
    'MASS_LAND_DENIAL',
    'TUTOR_TIERS',
    'normalize_card_name',
    'tier_for',
)
