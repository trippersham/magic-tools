"""CRISPI per-card tier classifier (Phase 1 — per-card classification only).

``classify_card(card, card_otag) -> CardTiers`` resolves, for ONE nonland card,
everything the four CRISPI axes need from it: its draw tier, its tutor tier, its
Interaction membership + stack/timing points, and its Resilience signals (threat /
recursion / protection). The axis-ladder math that turns these per-card facts into
1-10 axis scores lives in Phase 2/3 — this module is strictly per-card.

Resolution priority (rubric ``research/crispi-deep-dive-full.txt``):

  1. **Named table** (``crispi_tiers``) — the rubric NAMES the canonical card for
     every premium tier (Demonic Tutor -> premium-6, Rhystic Study ->
     premium-asymmetric-5, Toxic Deluge -> hard-scope-wipe). These are the
     alignment anchors to DeckCheck; they win over any heuristic.
  2. **Structured heuristic** — for cards the rubric does NOT name, a conservative
     heuristic keyed (in preference order) on otag slugs, then oracle_text, then
     type/CMC. UNNAMED cards can NEVER land in a premium tier (burst-6 /
     premium-asymmetric-5 draw, premium-6 tutor): the rubric names every premium
     example, so an unnamed card in one of those tiers would be a guess, not a read.

Every heuristic below carries a one-line rubric citation. Light oracle_text use is
acceptable here (CRISPI IS a scorer, unlike the neutral factsheet) but otag slugs
are preferred wherever a slug exists.

Graceful: an unresolved card (empty ``type_line`` / ``None`` cmc / no otags) simply
returns an all-inert :class:`CardTiers` — no tiers, not a threat — and never raises.
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass, field

from pipeline.contracts.models import CrispiAxis, CrispiBracket, CrispiResult
from pipeline.transforms.crispi_tiers import (
    DRAW_TIERS,
    EXTRA_TURNS,
    GAME_CHANGERS,
    INTERACTION_TIERS,
    MASS_LAND_DENIAL,
    TUTOR_TIERS,
    normalize_card_name,
    tier_for,
)
from pipeline.transforms.crosswalk import buckets_for
from pipeline.transforms.deck_factsheet import (
    _card_slugs,
    _is_instant_speed,
    _type_line,
    is_land,
)

# --------------------------------------------------------------------------- #
# The per-card classification record.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CardTiers:
    """Per-card CRISPI classification — the four axes' shared per-card view.

    Draw / tutor tiers are ``(tier_label, points) | None`` (None = the card is not
    a draw/tutor source). Interaction is a membership flag + composed stack points.
    Resilience signals are best-effort floats/flags the Phase-3 ladder consumes.
    Each carries a short ``*_why`` rationale so an axis can cite the card.
    """

    name: str

    #: Deck-row COPY count (basics/precon staples carry >1). Redundancy counting
    #: (``_tribal_bonus``) counts interchangeable copies by this, not deck rows —
    #: rubric: "25x Persistent Petitioners is one role filled 25 times".
    copies: int = 1

    # --- Consistency: draw column. -------------------------------------------
    draw: tuple[str, int] | None = None
    draw_why: str = ''

    # --- Consistency: tutor column. ------------------------------------------
    tutor: tuple[str, int] | None = None
    tutor_why: str = ''
    #: An activated/triggered BATTLEFIELD-tutor: a repeatable ability that puts a card
    #: from the library directly onto the battlefield (Sisay / Nick Fury class). The
    #: rubric treats such a commander as an ACCESS command-zone engine (R3-2), even
    #: when it carries no named tutor tier.
    is_battlefield_tutor: bool = False
    #: Graveyard-destination tutor (Entomb class): carries its tier here, but the
    #: rubric zeroes it without a recursion package. Phase 2 reads this flag.
    graveyard_gated: bool = False

    # --- Interaction. --------------------------------------------------------
    is_interaction: bool = False
    #: Composed stack/timing delta (counterspell 2 + free 2 + turn-protection 2 +
    #: instant +1 + hard-scope-wipe +1), summed across every class the card hits.
    stack_points: int = 0
    interaction_why: str = ''

    # --- Resilience signals (Phase 3 consumes these). ------------------------
    is_threat: bool = False
    #: Vanilla beatsticks weigh HALF (rubric threat-quality); everything else 1.0.
    threat_weight: float = 0.0
    recursion_points: float = 0.0
    protection_points: float = 0.0
    is_protection: bool = False
    #: Board-level protection effect (player/team grant, fog, phasing, mass blink) —
    #: the rubric counts these separately (row 6 needs 1, row 8 needs 2, row 9 needs 3).
    is_board_protection: bool = False
    resilience_why: str = ''
    #: Every rubric class the card matched (for auditing / axis citation).
    tags: frozenset[str] = field(default_factory=frozenset)
    #: The NARROW interchangeable-role slugs this card carries (a single-effect
    #: functional signal — ``land-ramp`` / ``repeatable-token-generator`` /
    #: ``repeatable-sacrifice-outlet`` / typal). The redundancy bonus keys on these,
    #: NOT the broad ``ramp``/``tokens``/``sac`` buckets: a pile of DIFFERENT mana
    #: rocks is variety, but a stack of ``land-ramp`` spells is one role filled many
    #: times (rubric ~127; DeckCheck's World Reclaimer land-ramp redundancy read).
    role_slugs: frozenset[str] = field(default_factory=frozenset)


# --------------------------------------------------------------------------- #
# Small text helpers (mirror the factsheet's precision-first style).
# --------------------------------------------------------------------------- #


def _text(card: dict) -> str:
    return (card.get('oracle_text') or '').lower()


def _cmc(card: dict) -> float:
    try:
        return float(card.get('cmc') or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _keywords(card: dict) -> set[str]:
    return {k.lower() for k in card.get('keywords') or []}


# Rubric draw sub-classes (unnamed heuristics).
_SYMMETRIC_DRAW_RE = re.compile(r'each player draws|each player may draw|that player draws')
_COMBAT_DRAW_RE = re.compile(
    r'(whenever .{0,60}?attacks|whenever .{0,60}?deals combat damage|whenever .{0,40}?connect)',
    re.DOTALL,
)
_SELECTION_RE = re.compile(r'\b(scry|surveil|explore)\b|look at the top|loot|connive')
_DRAW_N_RE = re.compile(r'draw (a|one|two|three|four|five|six|seven|\d+) cards?')
_ETB_RE = re.compile(r'when(ever)? .{0,40}?enters', re.DOTALL)
_SAC_ITSELF_RE = re.compile(r'sacrifice (this|~|it)\b|, sacrifice .{0,20}?: draw', re.DOTALL)
_REPEATABLE_DRAW_RE = re.compile(
    r'at the beginning of .{0,80}?draw|whenever .{0,80}?draw a card',
    re.DOTALL,
)

# Rubric tutor gating.
_GRAVEYARD_TUTOR_RE = re.compile(r'into your graveyard')

#: A battlefield-tutor tell (R3-2): text that puts a card from the library directly
#: onto the battlefield (Sisay: "search your library ... put ... onto the battlefield";
#: Nick Fury: look at top N, "put a ... card ... onto the battlefield"). The library
#: reference plus an "onto the battlefield" destination is the signal.
_BATTLEFIELD_TUTOR_RE = re.compile(
    # library reference BEFORE the put (Nick Fury / Sisay search-then-put) ...
    r'(search your library|look at the top .{0,30}?of your library|from among them|from your library)'
    r'.{0,160}?put .{0,80}?onto the battlefield'
    # ... or the put naming the library as its source (put X from your library onto bf).
    r'|put .{0,80}?(from your library|from among them).{0,60}?onto the battlefield'
    r'|put .{0,80}?onto the battlefield.{0,40}?from your library',
    re.DOTALL,
)
#: otag slugs marking a library->battlefield tutor ability (R3-2).
_BATTLEFIELD_TUTOR_SLUGS = frozenset({'tutor-to-battlefield', 'impulse-onto-battlefield', 'sneak-from-library'})
#: Tells that the battlefield-tutor is a REPEATABLE / activated-or-triggered ability
#: (not a one-shot spell): an activated cost, or a trigger word.
_REPEATABLE_ABILITY_RE = re.compile(r'^\s*(\{[^}]*\}\s*)*:|: |whenever|at the beginning', re.MULTILINE)

# Rubric interaction / protection.
_COUNTER_RE = re.compile(r'counter target')
_ATTACK_DETERRENT_RE = re.compile(r"can't attack you|attack you or planeswalkers you control")
#: Graveyard/exile -> battlefield recursion of a CREATURE or PERMANENT (not a land).
#: The source zone (graveyard or exile) and the battlefield destination are both
#: required, and the returned object must be a creature/permanent card — NOT a land
#: (land-ramp / land-animation is excluded, R3-1 over-count fix). "creature" | "it" |
#: "permanent" | "artifact"/"enchantment"/"planeswalker" target guards this.
_GRAVEYARD_RECURSION_RE = re.compile(
    r'return .{0,80}?(creature|artifact|enchantment|planeswalker|permanent|it) '
    r'card.{0,20}? from .{0,20}?(your |a )?(graveyard|exile) to the battlefield'
    r'|return .{0,80}?(creature|artifact|enchantment|planeswalker|permanent) '
    r'card from .{0,20}?(graveyard|exile) to the battlefield'
    r'|return target (creature|artifact|enchantment|planeswalker|permanent) card '
    r'from .{0,20}?(graveyard|exile) to the battlefield',
    re.DOTALL,
)
#: A permanent recast engine: unearth keyword, or a "cast/play ... from your
#: graveyard" clause for a nonland permanent (Conduit-class permanent recast). Used
#: alongside the recursion otag membership to BROADEN detection (R3-1 under-count).
_PERMANENT_RECAST_RE = re.compile(
    r'\bunearth\b|(cast|play) .{0,60}?(creature|artifact|enchantment|planeswalker|permanent).{0,40}?'
    r'from your graveyard',
    re.DOTALL,
)
#: otag slugs that mark a NONLAND graveyard/exile recursion or reanimation engine.
#: LAND recursion (``reanimate-land`` / ``recursion-land``) is deliberately absent —
#: it is land-ramp, not the permanent rebuild the combat rows measure (R3-1).
_RECURSION_OTAG_SLUGS = frozenset({'recursion', 'reanimate', 'reanimate-nonland', 'recursion-artifact'})
#: LAND-only recursion slugs — a card carrying ONLY these is land-ramp, not recursion.
_LAND_RECURSION_OTAG_SLUGS = frozenset({'reanimate-land', 'recursion-land'})
_TOKEN_ENGINE_RE = re.compile(r'at the beginning of .{0,60}?create', re.DOTALL)
_DRAIN_RE = re.compile(r'each opponent loses \d+ life|whenever .{0,40}?dies.{0,40}?loses \d+ life', re.DOTALL)


# --------------------------------------------------------------------------- #
# Draw classification.
# --------------------------------------------------------------------------- #


def _classify_draw(card: dict, slugs: set[str]) -> tuple[tuple[str, int] | None, str]:
    """Draw tier for a card. Named table first, else a conservative heuristic.

    Named lookup wins (the rubric's burst-6 / premium-asymmetric-5 anchors). For
    an UNNAMED card the otag layer marks as draw/card-advantage, the heuristic
    tiers per the Consistency "Draw Engine Sources" rubric — never premium/burst.
    """
    named = tier_for(card.get('name', ''), DRAW_TIERS)
    if named is not None:
        return named, f'named draw tier ({named[0]})'

    # Heuristic only for cards the otag layer flags as card-advantage/draw.
    # (rubric: draw sources are card-advantage; otag `draw` bucket = card-advantage root)
    if 'draw' not in buckets_for(slugs):
        return None, ''

    text = _text(card)

    # Symmetric ("each player draws") -> 2 (rubric: "three opponents drink first").
    if _SYMMETRIC_DRAW_RE.search(text):
        return ('symmetric', 2), 'heuristic: symmetric draw (each player draws) -> one-shot value [oracle_text]'

    # Combat-conditioned (attack / connect / combat damage) -> 3
    # (rubric: "Combat-conditioned draw of ANY kind scores 3").
    if _COMBAT_DRAW_RE.search(text):
        return ('combat-conditioned', 3), 'heuristic: draw gated on combat/attack -> 3 [oracle_text]'

    # Card selection / filtering (scry/surveil/loot/impulse) -> 3 (selection)
    # (rubric: "Card selection / filtering ... score 3").
    if _SELECTION_RE.search(text):
        return ('selection', 3), 'heuristic: card selection/filter (scry/surveil/loot) -> 3 [oracle_text]'

    # One-shot: ETB draw or sac-itself draw, or a bare non-repeatable "draw N" -> 2
    # (rubric: "Enter-the-battlefield draw ... and sacrifice-itself draw ... score 2").
    repeatable = bool(_REPEATABLE_DRAW_RE.search(text))
    if not repeatable and (_ETB_RE.search(text) or _SAC_ITSELF_RE.search(text) or _DRAW_N_RE.search(text)):
        return ('one-shot', 2), 'heuristic: one-shot draw (ETB / sac-itself / "draw N") -> 2 [oracle_text]'

    # Otherwise standard-repeatable -> 4 (rubric: "engines off ... normal game actions").
    # Conservatively NEVER premium-asymmetric(5)/burst(6): those are named-only.
    return ('standard-repeatable', 4), 'heuristic: repeatable draw engine (default) -> 4 [otag draw]'


# --------------------------------------------------------------------------- #
# Tutor classification.
# --------------------------------------------------------------------------- #


def _classify_tutor(card: dict, slugs: set[str]) -> tuple[tuple[str, int] | None, str, bool]:
    """Tutor tier + graveyard-gated flag. Named table first, else a CMC-band heuristic."""
    gy_gated = bool(_GRAVEYARD_TUTOR_RE.search(_text(card)))

    named = tier_for(card.get('name', ''), TUTOR_TIERS)
    if named is not None:
        gy = gy_gated or named[0] == 'graveyard-tutor'
        return named, f'named tutor tier ({named[0]})', gy

    # Heuristic only for cards the otag layer flags as a tutor.
    if 'tutor' not in buckets_for(slugs):
        return None, '', False

    cmc = _cmc(card)
    # rubric: "Narrow or expensive true tutors (CMC 5+)" score 2.
    if cmc >= 5:
        return ('narrow', 2), 'heuristic: expensive true tutor (CMC 5+) -> narrow/2 [otag tutor + cmc]', gy_gated
    # rubric: "Standard true tutors, CMC 3-4 or restricted" score 4. CMC <=2 UNNAMED
    # tutors are STANDARD not premium: premium is the named table only (conservative).
    return (
        ('standard', 4),
        'heuristic: unnamed true tutor (CMC <=4) -> standard/4 (premium is named-only) [otag tutor + cmc]',
        gy_gated,
    )


def _is_battlefield_tutor(card: dict, slugs: set[str]) -> bool:
    """Detect an activated/triggered library->battlefield tutor ability (R3-2).

    Rubric L125: an ACCESS command-zone engine includes "a repeatable battlefield-tutor
    — activated (Sisay, Weatherlight Captain) or triggered (Winota, Gishath)". We read
    this from the ``tutor-to-battlefield`` / ``impulse-onto-battlefield`` /
    ``sneak-from-library`` otag on a repeatable/activated ability, OR from the oracle
    text tell (a library reference feeding an "onto the battlefield" put). Requires the
    ability to be repeatable (activated cost or a trigger), never a one-shot spell.
    """
    text = _text(card)
    repeatable = bool(_REPEATABLE_ABILITY_RE.search(text))
    by_slug = bool(slugs & _BATTLEFIELD_TUTOR_SLUGS)
    by_text = bool(_BATTLEFIELD_TUTOR_RE.search(text))
    return (by_slug or by_text) and repeatable


# --------------------------------------------------------------------------- #
# Interaction classification (membership + composed stack points).
# --------------------------------------------------------------------------- #

#: otag buckets that constitute Total Interaction membership (rubric enumerates:
#: removal, counterspells, stax/hosers, protection, board wipes, attack deterrents,
#: goad, gy-hate, theft, hand attack, spot land interaction).
_INTERACTION_BUCKETS = frozenset({'removal', 'counterspells', 'protection', 'stax'})


def _classify_interaction(card: dict, slugs: set[str]) -> tuple[bool, int, str]:
    """Total-Interaction membership + composed stack/timing points.

    Stack points compose ACROSS classes (rubric: "This stacks with the classes
    above: a counterspell is an instant, so it prices at 3 total; a free counter
    at 5"):

      counterspell     +2 (otag `counterspell`/`counterspells` OR named counterspell OR "counter target")
      free spell       +2 (named `free` tier — 0-mana / alt-cost reactive)
      turn-protection  +2 (named turn-protection)
      instant credit   +1 (UNIVERSAL — every interaction member usable at instant speed)
      hard-scope wipe  +1 (named hard-scope-wipe)

    The INTERACTION_TIERS named table SEEDS the class (its ``points`` is that
    class's delta); the +1 instant credit is added STRUCTURALLY on top.
    """
    buckets = buckets_for(slugs)
    text = _text(card)

    named = tier_for(card.get('name', ''), INTERACTION_TIERS)
    named_label = named[0] if named else None

    # A card counts as a counterspell (rubric ~197 "What counts as a counterspell":
    # anything cast on the stack to stop a spell, INCLUDING "effective counterspells")
    # if it is in the otag `counterspells` bucket (fix B — an effective counterspell
    # like Redirect Lightning counts even without a named tier or a "counter target"
    # text match), OR is a named counterspell tier, OR reads "counter target".
    is_counter = (
        'counterspell' in slugs
        or bool(buckets & {'counterspells'})
        or named_label == 'counterspell'
        or bool(_COUNTER_RE.search(text))
    )

    # Membership: any Total-Interaction otag bucket, OR any named interaction class,
    # OR an instant/sorcery counter, OR an attack deterrent.
    is_member = (
        bool(buckets & _INTERACTION_BUCKETS)
        or named is not None
        or is_counter
        or bool(_ATTACK_DETERRENT_RE.search(text))
    )
    if not is_member:
        return False, 0, ''

    points = 0
    parts: list[str] = []

    if is_counter:
        points += 2
        parts.append('counterspell +2')
    if named_label == 'free':
        points += 2
        parts.append('free spell +2')
    if named_label == 'turn-protection':
        points += 2
        parts.append('turn-protection +2')

    # +1 instant credit: UNIVERSAL to every interaction member usable at instant speed
    # (rubric ~194: "1 point — every interaction piece usable at INSTANT speed (instant
    # type or flash). This stacks with the classes above."). This is NOT gated to
    # premium/named/counter status — a generic instant removal (Beast Within, Infernal
    # Grasp, Putrefy) earns +1; sorcery-speed removal, "however hard its scope", earns 0.
    if _is_instant_speed(card):
        points += 1
        parts.append('instant +1')

    # +1 hard-scope wipe (named hard-scope-wipe class).
    if named_label == 'hard-scope-wipe':
        points += 1
        parts.append('hard-scope wipe +1')

    why = 'interaction: ' + (', '.join(parts) if parts else 'Total-Interaction member (0 stack pts)')
    return True, points, why


# --------------------------------------------------------------------------- #
# Resilience signals (best-effort; Phase 3 consumes).
# --------------------------------------------------------------------------- #

_THREAT_KEYWORDS = {'flying', 'menace', 'trample', 'deathtouch', 'fear', 'intimidate', 'shadow', 'horsemanship'}


def _classify_resilience(card: dict, slugs: set[str]) -> tuple[bool, float, float, float, bool, bool, str]:
    """Threat / recursion / protection signals (rubric Resilience combat rows).

    Returns ``(is_threat, threat_weight, recursion_points, protection_points,
    is_protection, is_board_protection, why)``. Best-effort and DOCUMENTED; never raises.
    """
    type_line = _type_line(card).lower()
    text = _text(card)
    keywords = _keywords(card)
    buckets = buckets_for(slugs)
    parts: list[str] = []

    # --- Threat base (rubric: "a threat is anything the table must answer"). ---
    is_threat = False
    threat_weight = 0.0
    is_creature = 'creature' in type_line
    is_pw = 'planeswalker' in type_line
    power = _power(card)

    # Evasion / deathtouch / power>=4 creatures; planeswalkers, manlands,
    # token engines, theft/clone, drain engines always count.
    has_evasion = bool(keywords & _THREAT_KEYWORDS)
    has_deathtouch = 'deathtouch' in keywords
    token_engine = 'tokens' in buckets or bool(_TOKEN_ENGINE_RE.search(text))
    drain_engine = 'burn' in buckets or bool(_DRAIN_RE.search(text))
    manland = is_land(_type_line(card)) and 'becomes a' in text and 'creature' in text
    theft_clone = 'gain control' in text or 'a copy of' in text

    if is_pw or token_engine or drain_engine or manland or theft_clone:
        is_threat = True
        threat_weight = 1.0
        parts.append('threat (pw/token/drain/manland/theft engine)')
    elif is_creature and (power >= 4 or has_evasion or has_deathtouch):
        is_threat = True
        # Vanilla beatstick (big power, no evasion, no deathtouch, no value engine)
        # weighs HALF (rubric threat-quality: "Colossal Dreadmaw is not Sheoldred").
        value_engine = bool(buckets & {'draw', 'tutor', 'ramp', 'removal', 'flicker'})
        vanilla = power >= 4 and not has_evasion and not has_deathtouch and not value_engine and not text.strip()
        threat_weight = 0.5 if vanilla else 1.0
        parts.append('vanilla beatstick (half weight)' if vanilla else 'threat (evasion/deathtouch/power4+)')

    # --- Recursion points (rubric "Reading the combat rows"). ---
    # R3-1: recursion needs a graveyard/exile SOURCE and a creature/permanent target.
    # Land-ramp / land-animation text is excluded; a card tagged ONLY as land recursion
    # (reanimate-land / recursion-land) is land-ramp, not the permanent rebuild these
    # rows measure. Detection is BROADENED to catch otag-marked reanimation, unearth,
    # and permanent-recast (Conduit / Trading Post / Cityscape Leveler / unearth class).
    keywords = _keywords(card)
    recursion_points = 0.0
    land_only_recursion = bool(slugs & _LAND_RECURSION_OTAG_SLUGS) and not (slugs & _RECURSION_OTAG_SLUGS)
    otag_recursion = bool(slugs & _RECURSION_OTAG_SLUGS)
    gy_recursion = bool(_GRAVEYARD_RECURSION_RE.search(text))
    permanent_recast = 'unearth' in keywords or bool(_PERMANENT_RECAST_RE.search(text))

    if land_only_recursion:
        pass  # land-ramp masquerading as recursion — no points (R3-1 over-count fix).
    elif gy_recursion:
        # Repeatable battlefield recursion (an at-beginning / whenever trigger) -> 2;
        # otherwise a one-shot graveyard spell -> 1.
        if _REPEATABLE_DRAW_RE.search(text) or 'at the beginning' in text or 'whenever' in text:
            recursion_points = 2.0
            parts.append('repeatable recursion +2')
        else:
            recursion_points = 1.0
            parts.append('one-shot gy recursion +1')
    elif otag_recursion:
        # A reanimation / recursion engine (otag-tagged, nonland) — a rebuild engine.
        recursion_points = 1.5
        parts.append('reanimation/recursion engine +1.5')
    elif permanent_recast:
        # Unearth / repeatable permanent-recast (Conduit of Worlds, unearth class) —
        # a rebuild engine that restocks a permanent from the graveyard.
        recursion_points = 1.5
        parts.append('permanent-recast engine +1.5')
    elif 'flicker' in buckets:
        recursion_points = 1.5  # repeatable flicker engine (rubric: 1.5)
        parts.append('flicker engine +1.5')

    # --- Protection points (rubric "Protection quality — board-level"). ---
    protection_points = 0.0
    is_protection = False
    board_level = bool(
        re.search(r'prevent all|phase(s)? out|you and permanents you control|creatures you control gain', text)
    )
    attack_deterrent = bool(_ATTACK_DETERRENT_RE.search(text))
    if 'protection' in buckets or board_level or attack_deterrent:
        is_protection = True
        protection_points = 1.0
        parts.append('protection/attack-deterrent +1')
    # Board-level protection is its own rubric requirement (player/team grant, fog,
    # phasing, mass blink) — rows 6/8/9 count 1/2/3 of these directly.
    if board_level:
        parts.append('board-level protection')

    why = 'resilience: ' + ('; '.join(parts) if parts else 'no combat-resilience signal')
    return is_threat, threat_weight, recursion_points, protection_points, is_protection, board_level, why


def _power(card: dict) -> float:
    """Best-effort power read for the threat base; 0 when absent/variable (``*``)."""
    p = card.get('power')
    if p is None:
        return 0.0
    try:
        return float(p)
    except (TypeError, ValueError):
        return 0.0


# --------------------------------------------------------------------------- #
# Entry point.
# --------------------------------------------------------------------------- #


def classify_card(card: dict, card_otag: dict[str, set[str]]) -> CardTiers:
    """Classify ONE card across the four CRISPI axes (Phase 1: per-card only).

    Args:
        card: A resolved card dict (name/oracle_id/oracle_text/type_line/cmc/
            keywords/produced_mana/mana_cost/power). ``power`` (the resolver carries
            it as a string) seeds the Resilience threat base; absent/``*`` reads 0.
            An unresolved card (empty type_line, None cmc) is handled gracefully as
            inert.
        card_otag: ``str(oracle_id) -> set[slug]`` rolled-up slug closure.

    Returns:
        A frozen :class:`CardTiers`. Lands and fully-unresolved cards return an
        all-inert record (no tiers, not a threat).
    """
    name = card.get('name', '') or ''
    type_line = _type_line(card)

    # Lands never carry CRISPI card tiers (manlands are handled as threats above,
    # but a plain land is inert here — the axes count nonlands).
    if is_land(type_line):
        return CardTiers(name=name)

    slugs = _card_slugs(card, card_otag)

    # A truly unresolved card (no type line, no otags, no oracle text) is inert.
    if not type_line and not slugs and not _text(card):
        return CardTiers(name=name)

    draw, draw_why = _classify_draw(card, slugs)
    tutor, tutor_why, gy_gated = _classify_tutor(card, slugs)
    is_bf_tutor = _is_battlefield_tutor(card, slugs)
    is_interaction, stack_points, interaction_why = _classify_interaction(card, slugs)
    (
        is_threat,
        threat_weight,
        recursion_points,
        protection_points,
        is_protection,
        is_board_protection,
        resilience_why,
    ) = _classify_resilience(card, slugs)

    try:
        copies = max(1, int(card.get('quantity') or 1))
    except (TypeError, ValueError):
        copies = 1

    return CardTiers(
        name=name,
        copies=copies,
        draw=draw,
        draw_why=draw_why,
        tutor=tutor,
        tutor_why=tutor_why,
        is_battlefield_tutor=is_bf_tutor,
        graveyard_gated=gy_gated,
        is_interaction=is_interaction,
        stack_points=stack_points,
        interaction_why=interaction_why,
        is_threat=is_threat,
        threat_weight=threat_weight,
        recursion_points=recursion_points,
        protection_points=protection_points,
        is_protection=is_protection,
        is_board_protection=is_board_protection,
        resilience_why=resilience_why,
        tags=frozenset(buckets_for(slugs)),
        role_slugs=frozenset(slugs & _REDUNDANCY_ROLE_SLUGS),
    )


# =========================================================================== #
# Phase 2 — Consistency + Interaction axis scorers.
#
# Both axes are pure DETERMINISTIC COUNTING over the Phase-1 `CardTiers` records
# (the July-2026 rubric: "Consistency and Interaction are pure counting").
# Each returns a `CrispiAxis(value, rationale, cited_cards)` with `value` snapped
# to the quarter-point grid. The exact numeric ladders/tables below are
# transcribed verbatim from `research/crispi-deep-dive-full.txt`; do NOT retune
# them here (that is Phase-5 Fable-5 calibration work).
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Reusable core: quarter-point snap + anchor interpolation.
# --------------------------------------------------------------------------- #


def snap_quarter(x: float) -> float:
    """Snap a raw score to the nearest 0.25, midpoints rounding UP, clamped 1.0-10.0.

    The rubric: "each scored 1-10 in quarter-point steps ... exact midpoints round
    up". A value exactly halfway between two quarter-points (an eighth, e.g. 6.125)
    rounds to the HIGHER quarter (6.25). Implemented by scaling to quarter-units and
    using floor(n + 0.5) — which rounds .5 up — rather than Python's bankers'
    ``round``. The clamp is applied last so a computed 0.5 or 11.0 lands in range.
    """
    quarters = x * 4.0
    snapped = math.floor(quarters + 0.5) / 4.0
    return max(1.0, min(10.0, snapped))


def interp(total: float, anchors: list[tuple[float, float]]) -> float:
    """Piecewise-linear interpolation of ``total`` over sorted ``(x, score)`` anchors.

    A ``total`` sitting exactly ON an anchor reads that anchor's score; a total
    BETWEEN two anchors interpolates linearly between them. Below the first anchor
    clamps to the first score; at/above the last anchor clamps to the last score
    (the rubric's ``68+ -> 10`` / ``26+ -> 10`` / ``52+ -> 10`` open top rows).
    ``anchors`` must be sorted ascending by x and non-empty.
    """
    if total <= anchors[0][0]:
        return anchors[0][1]
    if total >= anchors[-1][0]:
        return anchors[-1][1]
    for (x0, y0), (x1, y1) in itertools.pairwise(anchors):
        if total <= x1:
            span = x1 - x0
            if span == 0:
                return y1
            frac = (total - x0) / span
            return y0 + frac * (y1 - y0)
    return anchors[-1][1]  # pragma: no cover — unreachable given the clamps above


# --------------------------------------------------------------------------- #
# Rubric tables (transcribed from research/crispi-deep-dive-full.txt).
# --------------------------------------------------------------------------- #

#: Consistency TUTOR ladder (interpolating). Rubric "Tutor ladder":
#: 0->3.5 · 12->4.5 · 20->5.5 · 24->6.25 · 32->7 · 44->8 · 56->9 · 68+->10.
_TUTOR_LADDER: list[tuple[float, float]] = [
    (0, 3.5),
    (12, 4.5),
    (20, 5.5),
    (24, 6.25),
    (32, 7.0),
    (44, 8.0),
    (56, 9.0),
    (68, 10.0),
]

#: Consistency DRAW row table (DISCRETE rows, not a ladder). Rubric draw table maps
#: a draw-point total to a descriptor ROW. Two rows span a range (3-4, 1-2); we read
#: those to their LOWER bound (conservative — the rubric offers no finer signal) and
#: represent each row by a single value. Entries are ``(min_total, row_value)``,
#: descending; the first whose ``min_total <= total`` wins.
_DRAW_ROWS: list[tuple[float, float]] = [
    (60, 10.0),  # Deterministic
    (40, 9.0),  # Highly Consistent
    (36, 8.0),  # Streamlined
    (32, 7.0),  # Focused
    (24, 6.0),  # Synergistic
    (20, 5.0),  # Baseline Casual
    (12, 3.5),  # Inconsistent (3-4 row -> lower-anchored 3.5)
    (0, 1.5),  # Unfocused (<12; 1-2 row -> 1.5)
]

#: Interaction COUNT ladder (interpolating). Rubric "Interaction pieces" row:
#: 0->1 · 3->3 · 6->4.5 · 10->5.5 · 14->6.25 · 18->7 · 22->8.5 · 26+->10.
_INTERACTION_COUNT_LADDER: list[tuple[float, float]] = [
    (0, 1.0),
    (3, 3.0),
    (6, 4.5),
    (10, 5.5),
    (14, 6.25),
    (18, 7.0),
    (22, 8.5),
    (26, 10.0),
]

#: Interaction STACK/TIMING ladder (interpolating). Rubric "Stack pts" row:
#: 0->3.5 · 6->4.75 · 10->5.75 · 14->6.5 · 20->7.5 · 28->8.25 · 38->9 · 45->9.5 · 52+->10.
_STACK_LADDER: list[tuple[float, float]] = [
    (0, 3.5),
    (6, 4.75),
    (10, 5.75),
    (14, 6.5),
    (20, 7.5),
    (28, 8.25),
    (38, 9.0),
    (45, 9.5),
    (52, 10.0),
]


def _draw_row(total: float) -> float:
    """Map a draw-point total to its DISCRETE rubric row value (not interpolated)."""
    for min_total, row in _DRAW_ROWS:
        if total >= min_total:
            return row
    return 1.5  # pragma: no cover — the 0-anchor above always matches.


# --------------------------------------------------------------------------- #
# mana_facts — the minimal structured-mana view the Consistency axis consumes.
#
# Phase 4's caller populates this from the factsheet's structured facts. Kept
# deliberately minimal:
#   land_count   : number of lands in the deck.
#   rock_count   : mana rocks (artifacts that produce mana).
#   dork_count   : mana dorks (creatures that produce mana).
#   avg_cmc      : nonland average mana value (drives the curve-adjusted land target).
#   nonland_count: nonland card count (unused by the penalty today; reserved).
#   pip_pressure : list of ``(color, pip_share, producer_count)`` per color, where
#                  pip_share is that color's fraction of total colored pips [0..1]
#                  and producer_count is how many sources make that color. A color
#                  with pip_share >= 0.20 on producer_count < 10 costs -1.
# All keys OPTIONAL (default to a neutral zero) so a partial dict never raises.
# --------------------------------------------------------------------------- #


def _curve_adjusted_land_target(avg_cmc: float) -> float:
    """Curve-adjusted land target: baseline 36 lands, +/- with the nonland avg CMC.

    APPROXIMATION (flag for calibration): the rubric says "curve-adjusted land
    target" without giving the formula. We anchor on the community baseline of ~36
    lands for an average-CMC (~3.0) Commander deck and slide +/- 2 lands per point
    of avg-CMC deviation from 3.0, clamped to a sane [33, 40] band. Only the
    DISTANCE below this target matters (>=6 -> -1, >=10 -> -2), so the exact anchor
    is second-order; documented for Fable-5 review.
    """
    target = 36.0 + (avg_cmc - 3.0) * 2.0
    return max(33.0, min(40.0, target))


def _mana_penalty(mana_facts: dict) -> tuple[float, list[str]]:
    """Mana-reliability penalty (rubric "Mana Reliability modifier"), capped at -2.

    effective sources = lands + 0.75*(rocks+dorks). 6+ below the curve-adjusted land
    target -> -1; 10+ below -> -2. A color carrying >=20% of pips on <10 producers ->
    -1. Total penalty is capped at -2 (returned as a NEGATIVE number).
    """
    lands = float(mana_facts.get('land_count', 0) or 0)
    rocks = float(mana_facts.get('rock_count', 0) or 0)
    dorks = float(mana_facts.get('dork_count', 0) or 0)
    avg_cmc = float(mana_facts.get('avg_cmc', 3.0) or 3.0)

    effective = lands + 0.75 * (rocks + dorks)
    target = _curve_adjusted_land_target(avg_cmc)
    deficit = target - effective

    penalty = 0.0
    notes: list[str] = []
    if deficit >= 10:
        penalty -= 2.0
        notes.append(f'mana base {deficit:.1f} sources below target ({effective:.1f}/{target:.0f}) -> -2')
    elif deficit >= 6:
        penalty -= 1.0
        notes.append(f'mana base {deficit:.1f} sources below target ({effective:.1f}/{target:.0f}) -> -1')

    for entry in mana_facts.get('pip_pressure', []) or []:
        try:
            color, share, producers = entry
        except (ValueError, TypeError):
            continue
        if float(share) >= 0.20 and float(producers) < 10:
            penalty -= 1.0
            notes.append(f'{color}: {float(share) * 100:.0f}% of pips on {int(producers)} producers (<10) -> -1')

    if penalty < -2.0:
        penalty = -2.0
    return penalty, notes


# --------------------------------------------------------------------------- #
# Consistency axis.
# --------------------------------------------------------------------------- #

#: Draw tiers whose points are "selection / filtering" — capped at 30 in the draw
#: total (rubric: "Selection points count toward the draw total only up to 30").
_SELECTION_DRAW_LABELS = frozenset({'selection'})
#: Trigger-re-fire draw (command-zone engine decks) — its own cap of 18.
_TRIGGER_REFIRE_LABELS = frozenset({'trigger-refire'})
#: The two premium tutor tiers (6-pt): the premium GATE and the tribal-cap waiver
#: both key on "premium-tier tutors" = these labels.
_PREMIUM_TUTOR_LABELS = frozenset({'premium', 'repeatable-engine'})


def _is_draw_engine_commander(commander: CardTiers | None) -> bool:
    """Commander is itself a draw engine (rubric +3 draw bonus / volume-engine lift).

    Heuristic: the commander carries a repeatable/premium draw tier (a real engine),
    NOT merely a one-shot/selection draw. Documented approximation.
    """
    if commander is None or commander.draw is None:
        return False
    label = commander.draw[0]
    return label in {'standard-repeatable', 'premium-asymmetric', 'burst'}


def _is_tutor_commander(commander: CardTiers | None) -> bool:
    """Commander tutors (rubric +5 tutor bonus)."""
    return commander is not None and commander.tutor is not None


def _commander_engine_class(commander: CardTiers | None) -> str | None:
    """Classify a command-zone engine as 'access' or 'volume' (rubric lift), else None.

    APPROXIMATION (flag for calibration) — the rubric distinguishes:
      * ACCESS engine — repeatable battlefield-tutor / selection; lifts the LOWER
        column. Detected from a repeatable-engine / combat-tutor tutor tier, or a
        commander tagged as a tutor/selection engine.
      * VOLUME engine — repeatable draw / card-advantage; lifts the DRAW column
        only. Detected from a repeatable/premium draw tier (``_is_draw_engine_commander``).
    A commander that is BOTH counts as access (finding beats making, and access
    lifts the weaker column which is the tighter constraint). Returns None when the
    commander is not itself an engine.
    """
    if commander is None:
        return None
    # An activated/triggered battlefield-tutor is an access engine by definition
    # (Sisay / Nick Fury class), even without a named tutor tier (R3-2).
    if commander.is_battlefield_tutor:
        return 'access'
    if commander.tutor is not None and commander.tutor[0] in {
        'premium',
        'repeatable-engine',
        'combat-tutor',
        'standard',
    }:
        return 'access'
    if _is_draw_engine_commander(commander):
        return 'volume'
    return None


@dataclass(frozen=True)
class ConsistencyTotals:
    """The Consistency column totals — SHARED with Resilience (Phase 4 wiring).

    The Consistency axis and the Resilience axis both consume the SAME draw / tutor
    point totals (Resilience's combo-assembly multiplier keys on ``tutor_total``;
    its heavy-draw recovery + answer-density path key on ``draw_total``). Computing
    them ONCE here (and having :func:`consistency_axis` reuse this) keeps the two
    axes' view of "how much draw / how many tutors" identical — no drift between the
    Consistency score and the Resilience inputs Phase 4 threads through.

    ``draw_total`` / ``tutor_total`` are the column totals AFTER the rubric caps,
    commander bonuses, combo-commander lift, and tribal bonus (exactly what
    :func:`consistency_axis` snaps to rows). ``premium_tutor_count`` and
    ``cited`` are carried so the axis need not recompute them.
    """

    draw_total: float
    tutor_total: float
    premium_tutor_count: int
    cited: list[str]


def consistency_totals(
    classified: list[CardTiers],
    commander: CardTiers | None,
    *,
    combo_commander: bool = False,
) -> ConsistencyTotals:
    """Compute the Consistency draw / tutor column totals (the SHARED counting).

    Pure counting over the Phase-1 records — the single source of the draw + tutor
    totals both :func:`consistency_axis` (which snaps them to rows) and Phase-4's
    :func:`resilience_axis` call (``tutor_points`` / ``draw_points``) consume. See
    :class:`ConsistencyTotals`.
    """
    cited: list[str] = []

    # --- Draw column total (with rubric caps). -------------------------------
    draw_total = 0.0
    selection_total = 0.0
    refire_total = 0.0
    for c in classified:
        if c.draw is None:
            continue
        label, pts = c.draw
        if label in _SELECTION_DRAW_LABELS:
            selection_total += pts
        elif label in _TRIGGER_REFIRE_LABELS:
            refire_total += pts
        else:
            draw_total += pts
            if pts >= 4:
                cited.append(c.name)
    # Selection caps at 30; trigger-re-fire caps at 18 (rubric).
    draw_total += min(selection_total, 30.0)
    draw_total += min(refire_total, 18.0)
    if _is_draw_engine_commander(commander):
        draw_total += 3.0
        if commander is not None:
            cited.append(commander.name)
    if combo_commander:
        draw_total += 4.0

    # --- Tutor column total (with commander bonus). --------------------------
    tutor_total = 0.0
    premium_tutor_count = 0
    # Signal for graveyard-tutor gating: recursion package (>=3 recursion cards OR a
    # recursion commander).
    recursion_cards = sum(1 for c in classified if c.recursion_points > 0)
    recursion_commander = commander is not None and commander.recursion_points > 0
    has_recursion_package = recursion_cards >= 3 or recursion_commander

    for c in classified:
        if c.tutor is None:
            continue
        label, pts = c.tutor
        if c.graveyard_gated and not has_recursion_package:
            continue  # rubric: dead cardboard, scores 0 without recursion.
        tutor_total += pts
        if label in _PREMIUM_TUTOR_LABELS:
            premium_tutor_count += 1
            cited.append(c.name)
    if _is_tutor_commander(commander):
        tutor_total += 5.0
        if commander is not None:
            cited.append(commander.name)
        if commander is not None and commander.tutor[0] in _PREMIUM_TUTOR_LABELS:
            premium_tutor_count += 1
    if combo_commander:
        tutor_total += 4.0

    # --- Tribal / redundancy bonus (graduated; folds into the tutor column). --
    tribal_bonus, _tribal_notes = _tribal_bonus(classified)
    tutor_total += tribal_bonus

    return ConsistencyTotals(
        draw_total=draw_total,
        tutor_total=tutor_total,
        premium_tutor_count=premium_tutor_count,
        cited=cited,
    )


def consistency_axis(
    classified: list[CardTiers],
    commander: CardTiers | None,
    mana_facts: dict,
    *,
    combo_commander: bool = False,
) -> CrispiAxis:
    """Score the Consistency axis (rubric "Consistency - Full Rubric").

    Joint requirement: ``min(draw_row, tutor_row)`` after bonuses/lifts/penalties.
    ``classified`` is the nonland ``CardTiers`` list; ``commander`` the commander's
    ``CardTiers`` (or None); ``mana_facts`` the structured-mana dict documented above.
    ``combo_commander`` (Phase-4 wired) adds +4 to BOTH columns.

    The draw / tutor column totals come from :func:`consistency_totals` (the shared
    counting Phase 4 also feeds to Resilience), so the Consistency score and the
    Resilience inputs can never disagree on "how much draw / how many tutors".
    """
    totals = consistency_totals(classified, commander, combo_commander=combo_commander)
    draw_total = totals.draw_total
    tutor_total = totals.tutor_total
    premium_tutor_count = totals.premium_tutor_count
    cited: list[str] = list(totals.cited)

    # --- Tribal / redundancy bonus (graduated). ------------------------------
    # APPROXIMATION (flag for calibration): "interchangeable role" is read as a
    # repeated Phase-1 tag/bucket shared by many cards. Interaction / tutor / draw
    # packages never qualify (rubric), so those buckets are excluded. 10+ carriers of
    # one role -> +10; 6-9 -> +5; up to two ADDITIONAL 10+ roles -> +5 each.
    # The bonus is ALREADY folded into ``tutor_total`` by ``consistency_totals``;
    # we re-derive here only for the rationale notes + the redundancy cap gate.
    tribal_bonus, tribal_notes = _tribal_bonus(classified)

    # --- Snap columns to rows. -----------------------------------------------
    draw_row = _draw_row(draw_total)
    tutor_row = interp(tutor_total, _TUTOR_LADDER)

    # --- Premium gate: rows 9-10 need >=2 premium tutors, else cap at 8. ------
    gated = False
    if tutor_row > 8.0 and premium_tutor_count < 2:
        tutor_row = 8.0
        gated = True

    # --- Tribal cap: redundancy caps the tutor column at 7 UNLESS >=2 premium. -
    if tribal_bonus > 0 and premium_tutor_count < 2 and tutor_row > 7.0:
        tutor_row = 7.0

    # --- Command-zone-engine lift (<=+2 rows, <=9), lifts the LOWER column. ----
    lift_note = ''
    engine_class = _commander_engine_class(commander)
    if engine_class == 'volume':
        lifted = min(9.0, draw_row + 2.0)
        if lifted > draw_row:
            lift_note = f'volume command-zone engine lifts draw {draw_row:g}->{lifted:g}'
            draw_row = lifted
    elif engine_class == 'access':
        # Access lifts whichever column reads LOWER.
        if draw_row <= tutor_row:
            lifted = min(9.0, draw_row + 2.0)
            if lifted > draw_row:
                lift_note = f'access command-zone engine lifts draw {draw_row:g}->{lifted:g}'
                draw_row = lifted
        else:
            lifted = min(9.0, tutor_row + 2.0)
            if lifted > tutor_row:
                lift_note = f'access command-zone engine lifts tutor {tutor_row:g}->{lifted:g}'
                tutor_row = lifted

    # --- Joint min. ----------------------------------------------------------
    base = min(draw_row, tutor_row)

    # --- Mana-reliability penalty (-1 / -2). ---------------------------------
    penalty, mana_notes = _mana_penalty(mana_facts)
    value = snap_quarter(base + penalty)

    # --- Rationale. ----------------------------------------------------------
    binding = 'tutor' if tutor_row <= draw_row else 'draw'
    parts = [
        f'draw column {draw_total:.0f} pts -> row {draw_row:g}; '
        f'tutor column {tutor_total:.0f} pts -> row {tutor_row:g}',
    ]
    if binding == 'tutor' and tutor_row <= 5.0:
        parts.append("the deck's tutor column is nearly empty, which caps how reliably it assembles its key pieces")
    parts.append(f'joint min binds on the {binding} column')
    if gated:
        parts.append('premium gate: <2 premium tutors caps the tutor column at 8')
    if lift_note:
        parts.append(lift_note)
    if tribal_notes:
        parts.append('; '.join(tribal_notes))
    if mana_notes:
        parts.append('; '.join(mana_notes))
    rationale = '. '.join(parts) + '.'

    # De-duplicate cited cards preserving order.
    seen: set[str] = set()
    cited_unique = [n for n in cited if not (n in seen or seen.add(n))]
    return CrispiAxis(value=value, rationale=rationale, cited_cards=cited_unique)


#: NARROW interchangeable-role otag slugs. Each names ONE functional effect a stack
#: of cards fills interchangeably (rubric ~127: "redundant copies of one effect are
#: virtual tutors for it"; DeckCheck's World Reclaimer read: "8+ land-ramp spells =
#: massive redundancy role"). We key on the NARROW slug, NOT the broad ``ramp`` /
#: ``tokens`` / ``sac`` bucket: a pile of DIFFERENT mana rocks / token payoffs is
#: variety, but a stack of ``land-ramp`` spells is one role filled many times.
#: Interaction/tutor/draw families are deliberately absent (rubric: "Interaction
#: suites and tutor/draw packages never qualify"), as are the heterogeneous variety
#: umbrellas (combat / wincon / burn — "different win conditions is variety").
#:
#: Only ``land-ramp`` is retained: it is ground-truth-validated (World Reclaimer's
#: land-ramp pile). ``repeatable-token-generator`` / ``repeatable-sacrifice-outlet``
#: were tried but REMOVED — a "token generator" stack is functionally heterogeneous
#: (mana tokens vs bodies vs an anthem-that-upgrades-tokens vs a copy engine), i.e.
#: the rubric's "variety, not redundancy," and they over-credited Consistency. A real
#: single tribe still qualifies via the ``typal`` bucket below.
_REDUNDANCY_ROLE_SLUGS = frozenset(
    {
        'land-ramp',  # World Reclaimer's massive land-ramp redundancy role (validated).
    }
)
#: Buckets that ARE a genuinely interchangeable single-effect role via the bucket
#: itself: a narrow typal/tribal package (10 Goblins fill the same role 10 times).
#: There is no single narrow typal slug, but the ``typal`` bucket is already a narrow
#: real tribe (unlike the broad ramp/token/sac buckets, which mix variety).
_REDUNDANCY_ROLE_BUCKETS = frozenset({'typal'})


def _tribal_bonus(classified: list[CardTiers]) -> tuple[float, list[str]]:
    """Graduated tribal/redundancy bonus (rubric "Tribal/Synergy Bonus").

    Fires ONLY for genuinely INTERCHANGEABLE single-effect redundancy — the rubric
    wants "10+ interchangeable copies of one critical role... copies count (25x
    Persistent Petitioners is one role filled 25 times)... The cards must be
    interchangeable — a pile of different win conditions is variety, not redundancy...
    Interaction suites and tutor/draw packages never qualify." Three qualifying signals:

      * literal copies — repeated identical card NAMES, counted by ``copies`` (a
        quantity-10 basic-effect staple is 10 interchangeable copies),
      * a narrow single typal role (a real tribe), and
      * a single interchangeable FUNCTIONAL role — ramp / tokens / sacrifice
        (:data:`_REDUNDANCY_ROLE_BUCKETS`). DeckCheck's World Reclaimer read credits
        "8+ land-ramp spells = massive redundancy role"; a token or aristocrat stack
        is the same single role filled many times.

    The rubric-barred families (interaction suites, tutor/draw packages) and the
    heterogeneous variety umbrellas (combat / wincon / burn / counters — "different
    win conditions is variety") are EXCLUDED (:data:`_REDUNDANCY_UMBRELLA_BUCKETS`).
    When unsure, do NOT award (conservative). The single largest qualifying role sets
    the base (+10 for 10+, +5 for 6-9); each ADDITIONAL distinct 10+ role adds +5, up
    to two extras.
    """
    counts: dict[str, int] = {}

    # (a) Literal copies — repeated identical card names, counted by copy quantity.
    name_copies: dict[str, int] = {}
    for c in classified:
        if not c.name:
            continue
        name_copies[c.name] = name_copies.get(c.name, 0) + max(1, c.copies)
    for name, n in name_copies.items():
        if n >= 2:  # a real copy pile, not a singleton
            counts[f'copies:{name}'] = n

    # (b) Narrow interchangeable FUNCTIONAL roles — keyed on the NARROW otag slug
    # (land-ramp / repeatable-token-generator / repeatable-sacrifice-outlet), NOT the
    # broad ramp/tokens/sac bucket. A pile of different mana rocks is variety; a stack
    # of land-ramp spells is one role filled many times.
    for role in _REDUNDANCY_ROLE_SLUGS:
        n = sum(c.copies for c in classified if role in c.role_slugs)
        if n > 0:
            counts[role] = n

    # (c) Narrow single typal role (a real tribe) — an interchangeable-role bucket.
    for role in _REDUNDANCY_ROLE_BUCKETS:
        n = sum(c.copies for c in classified if role in c.tags)
        if n > 0:
            counts[role] = n

    if not counts:
        return 0.0, []
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    top_tag, top_n = ranked[0]
    if top_n < 6:
        return 0.0, []

    def _label(tag: str) -> str:
        return tag[len('copies:') :] if tag.startswith('copies:') else tag

    bonus = 0.0
    notes: list[str] = []
    if top_n >= 10:
        bonus += 10.0
        notes.append(f'redundancy: {top_n}x "{_label(top_tag)}" role -> +10 tutor')
    else:
        bonus += 5.0
        notes.append(f'redundancy: {top_n}x "{_label(top_tag)}" role -> +5 tutor')

    extras = 0
    for tag, n in ranked[1:]:
        if extras >= 2:
            break
        if n >= 10:
            bonus += 5.0
            notes.append(f'+5 for additional 10+ "{_label(tag)}" role')
            extras += 1
    return bonus, notes


# --------------------------------------------------------------------------- #
# Interaction axis.
# --------------------------------------------------------------------------- #

#: Answer-scope classes the rubric gates rows 8+ on (creatures / artifacts /
#: enchantments). We read a card's scope from its Phase-1 tags/buckets.
_SCOPE_CREATURE_TAGS = frozenset({'removal', 'counterspells', 'stax', 'protection'})


def interaction_axis(
    classified: list[CardTiers],
    *,
    symmetric_wipe_count: int = 0,
    scope_creature: bool | None = None,
    scope_artifact: bool | None = None,
    scope_enchantment: bool | None = None,
    counterspell_count: int | None = None,
) -> CrispiAxis:
    """Score the Interaction axis (rubric "Interaction - Full Rubric").

    Weakest-binds joint of (count row, stack row, answer-scope). ``classified`` is
    the nonland ``CardTiers`` list.

    Scope flags (creature/artifact/enchantment coverage) and ``counterspell_count``
    may be passed explicitly by the Phase-4 caller (which has the richer otag view);
    when omitted they are DERIVED here from the per-card tags — an APPROXIMATION
    documented below. ``symmetric_wipe_count`` (>=3 caps the score at 7) is likewise
    caller-supplied because symmetry-in-context can't be read from a single card.
    """
    interaction_cards = [c for c in classified if c.is_interaction]
    count = len(interaction_cards)
    stack_total = sum(c.stack_points for c in interaction_cards)

    cited = [c.name for c in interaction_cards if c.stack_points >= 2][:8]

    count_row = interp(count, _INTERACTION_COUNT_LADDER)
    stack_row = interp(stack_total, _STACK_LADDER)

    # --- Answer-scope gate. --------------------------------------------------
    # APPROXIMATION (flag for calibration): from per-card tags we can reliably read
    # CREATURE coverage (removal/counter/stax/protection all answer creatures at
    # least indirectly). Artifact/enchantment coverage is NOT distinguishable from a
    # generic `removal` tag at this layer, so unless the caller passes them
    # explicitly we conservatively assume BOTH are covered when the suite has any
    # removal — leaving the real artifact/enchantment gap to the Phase-4 caller
    # (which sees otags) or an explicit override.
    if counterspell_count is None:
        counterspell_count = sum(1 for c in interaction_cards if c.stack_points >= 2)
    if scope_creature is None:
        scope_creature = any(c.tags & _SCOPE_CREATURE_TAGS for c in interaction_cards) or count > 0
    if scope_artifact is None:
        scope_artifact = any('removal' in c.tags for c in interaction_cards)
    if scope_enchantment is None:
        scope_enchantment = any('removal' in c.tags for c in interaction_cards)

    # All three (or a real 4+ counterspell suite) permits every row; creature + one
    # other class permits up to 8; creature-only permits up to 7.
    scope_cap = 10.0
    scope_note = ''
    covered = sum(bool(x) for x in (scope_creature, scope_artifact, scope_enchantment))
    if counterspell_count >= 4 or covered == 3:
        scope_cap = 10.0
    elif scope_creature and covered == 2:
        scope_cap = 8.0
        scope_note = 'answer-scope covers creatures + one other class -> caps at 8'
    elif scope_creature:
        scope_cap = 7.0
        scope_note = 'creature-only answer scope -> caps at 7'
    else:
        scope_cap = 5.0
        scope_note = 'no clear answer scope -> caps at 5'

    # --- Weakest binds: joint of count row, stack row, scope cap. ------------
    binding_row = min(count_row, stack_row)
    value_raw = min(binding_row, scope_cap)

    # --- Symmetric board-wipe cap (>=3 -> max 7). ----------------------------
    wipe_note = ''
    if symmetric_wipe_count >= 3 and value_raw > 7.0:
        value_raw = 7.0
        wipe_note = f'{symmetric_wipe_count} symmetric board wipes cap the score at 7'

    value = snap_quarter(value_raw)

    binding = 'stack/timing' if stack_row <= count_row else 'count'
    parts = [f'{count} interactive cards -> count row {count_row:g}; {stack_total} stack pts -> row {stack_row:g}']
    if stack_row <= count_row and stack_row <= 5.5:
        parts.append(
            'the suite operates mostly at sorcery speed with little stack presence, '
            'which is the real ceiling on how well it answers combo turns'
        )
    parts.append(f'weakest column binds on {binding}')
    if scope_note:
        parts.append(scope_note)
    if wipe_note:
        parts.append(wipe_note)
    rationale = '. '.join(parts) + '.'

    return CrispiAxis(value=value, rationale=rationale, cited_cards=cited)


# =========================================================================== #
# Phase 3 — Speed + Resilience axis scorers.
#
# These two axes consume the ONLY two judgement inputs the July-2026 engine
# leaves to the AI (rubric "New Engine": "exactly two judgements ... the
# fundamental turn, and how commander-dependent the deck is"). Everything else
# is deterministic counting over the Phase-1 `CardTiers` records plus the
# Phase-4 combo-match list. Tables below are transcribed VERBATIM from
# `research/crispi-deep-dive-full.txt`; do NOT retune here (Phase-5 Fable-5
# calibration owns tuning).
# =========================================================================== #


# --------------------------------------------------------------------------- #
# Speed axis.
# --------------------------------------------------------------------------- #

#: Speed table (rubric "Speed - Full Rubric"): fundamental turn -> score. Fast rows
#: are SINGLE turns worth a full point each (T2->10 · T3->9 · T4->8 · T5->7 · T6->6
#: · T7->5); the tail rows BLEND two turns (T8-9->4 · T10-11->3 · T12-13->2 · T14+
#: ->1) so we anchor each tail band at its MIDPOINT (8.5/10.5/12.5). A fundamental
#: turn straddling two rows interpolates to the .5 between them (rubric: turn 3.5 ->
#: 8.5 "win between turn 3 and 4"; turn 4.5 -> 7.5 "turn 4 or 5"). Turns <=2 clamp to
#: 10; turns >=14 clamp to 1.
_SPEED_LADDER: list[tuple[float, float]] = [
    (2.0, 10.0),
    (3.0, 9.0),
    (4.0, 8.0),
    (5.0, 7.0),
    (6.0, 6.0),
    (7.0, 5.0),
    (8.5, 4.0),
    (10.5, 3.0),
    (12.5, 2.0),
    (14.0, 1.0),
]


def speed_axis(fundamental_turn: float, consistency_value: float) -> CrispiAxis:
    """Score the Speed axis (rubric "Speed - Full Rubric").

    Speed is entirely the fundamental turn — the turn the deck actually completes
    its first elimination in >=50% of goldfish games (the AI-judged input). We map
    that (float) turn onto the Speed table, interpolating for half-steps.

    Args:
        fundamental_turn: The AI-judged fundamental turn (float; half-steps allowed).
            This is one of the two Phase-4/skill-supplied judgement inputs.
        consistency_value: The already-computed Consistency axis value, used ONLY to
            apply the Speed/Consistency coupling cap. Phase 4 passes the Consistency
            axis's ``value`` here (the coupling can only be applied once Consistency
            is known).

    Returns:
        A :class:`CrispiAxis`. ``cited_cards`` is always empty — Speed is turn-based,
        not card-cited.
    """
    raw = interp(float(fundamental_turn), _SPEED_LADDER)
    lookup = snap_quarter(raw)

    # Speed/Consistency coupling (rubric): if Speed is 9 or 10 AND Consistency is 7
    # or lower, cap Speed at 8. A deck that goldfishes a turn-3 win it cannot
    # reliably assemble is not truly a Speed-9 deck.
    capped = False
    if raw >= 9.0 and consistency_value <= 7.0:
        raw = min(raw, 8.0)
        capped = True

    value = snap_quarter(raw)

    parts = [f'fundamental turn {fundamental_turn:g} -> Speed {lookup:g}']
    if capped:
        parts.append(f'Speed/Consistency coupling: Consistency {consistency_value:g} <= 7 caps Speed at 8')
    rationale = '. '.join(parts) + '.'
    return CrispiAxis(value=value, rationale=rationale, cited_cards=[])


# --------------------------------------------------------------------------- #
# Resilience axis.
# --------------------------------------------------------------------------- #


def _combo_layering_row(combos: list, tutor_points: float, combat_row: float = 0.0) -> tuple[float, str]:
    """Resilience via combo layering (rubric "Combo Layering / Inevitability").

    Counts DISTINCT win-producing combo lines from the Phase-4 ``combos`` match list
    (each :class:`combo_detect.Combo` is one registered win/infinite line), then
    assembly-adjusts by tutor access:

      * full credit at 24+ tutor points, x0.75 at 12-23, x0.5 below 12
        (rubric: "Line credit scales with tutor access").

    The adjusted line count maps to a row:

      * 3+ interlocking     -> 10
      * 2+ distinct         -> 9
      * 1.5+ redundant      -> 8
      * 1 primary + backup  -> 7  (2 raw lines that DON'T survive assembly to 2.0, OR
        1 assembled line WITH an independent combat backup reading >=5 — R3-3)
      * lone line, no backup -> caps at 6 (rubric: "A single line ... caps at 6")

    R3-3 (rubric row 7 = "1 primary combo + 1 backup"): a lone assembled combo line is
    normally capped at 6, but when the deck ALSO has a real combat backup channel — its
    combat path independently reads >=5 — the cap is released to 7 ("1 primary + 1
    backup"). ``combat_row`` is the combat-path row the caller already computed. This
    NEVER fires for a comboless deck (n_lines==0 returns 0 early) and NEVER fires on a
    weak/absent combat backup (combat_row < 5).

    Assembly-discounted totals read BELOW those rows (rubric): a 0.75-credit total
    reads 5, a 0.5-credit total reads 3.5.

    APPROXIMATIONS (flag for Fable-5 calibration):
      * We treat every matched ``Combo`` as a full "win-producing" line and do NOT
        apply the per-line efficiency/point-of-failure HALF discounts (>6 mana, X
        cost, 4+ cards, shared single point of failure) — the Phase-1 combo data
        does not carry mana/piece-count metadata. This is OPTIMISTIC; a follow-up
        can thread per-combo cost signals in.
      * Distinctness is by ``variant_id`` (Spellbook variants are already distinct
        interactions); we do not dedupe combos that share a key card into 1.5.
    """
    n_lines = len({c.variant_id for c in combos})
    if n_lines == 0:
        return 0.0, ''

    # Assembly multiplier from tutor access.
    if tutor_points >= 24:
        mult, mult_label = 1.0, 'full assembly credit (24+ tutor pts)'
    elif tutor_points >= 12:
        mult, mult_label = 0.75, 'x0.75 assembly (12-23 tutor pts)'
    else:
        mult, mult_label = 0.5, 'x0.5 assembly (<12 tutor pts)'

    adjusted = n_lines * mult

    # Discounted totals read below the base rows (rubric): 0.75-credit -> 5,
    # 0.5-credit -> 3.5. We apply these when assembly actually discounted the lines.
    if mult == 0.5:
        row = 3.5
    elif mult == 0.75:
        row = 5.0
    elif adjusted >= 3:
        row = 10.0
    elif adjusted >= 2:
        row = 9.0
    elif adjusted >= 1.5:
        row = 8.0
    elif n_lines >= 2:
        row = 7.0  # 1 primary + 1 backup
    elif combat_row >= 5.0:
        # R3-3: a lone assembled combo line WITH an independent combat backup channel
        # (combat path reads >=5) is "1 primary + 1 backup" -> row 7, not the capped 6.
        row = 7.0
    else:
        row = 6.0  # lone line, no backup -> caps at 6

    backup = ' (+combat backup >=5 -> 7)' if row == 7.0 and n_lines < 2 else ''
    note = f'combo layering: {n_lines} distinct line(s) x {mult_label} -> row {row:g}{backup}'
    return row, note


def _combat_row(classified: list[CardTiers], draw_points: float) -> tuple[float, str]:
    """Resilience via the combat rows (rubric "Reading the combat rows").

    Aggregates effective threats, real protection, board-level protection, and
    recursion points + rebuild-engine count from the Phase-1 records, then matches
    the STRUCTURAL requirements per row (both totals AND engine/board counts must
    clear). Heavy draw is recovery: 32+ draw points add +1 recursion point, 40+
    add +2 (rubric).

    Row structural requirements (transcribed from the rubric rows 9/8/6/5/4.5):
      * row 9: 12+ effective threats · 8+ protection · 12+ recursion pts w/ 4+
        rebuild engines · 3+ board-level protection.
      * row 8: 12+ threats · 6+ protection · 10+ recursion pts w/ 3+ engines · 2+
        board-level protection.
      * row 6: 10+ threats · 4+ protection · 8+ recursion pts w/ 2+ engines · 1+
        board-level protection.
      * row 5: 8+ threats · 2+ protection · 4+ recursion pts.
      * row 4.5: 6+ threats · 1+ protection · 2+ recursion pts.
      * row 3.5: 3+ threats, no support structure.
      * else: 2.5 (glass — no real threat base).

    APPROXIMATION (flag for calibration): a "rebuild engine" is a Phase-1 card with
    ``recursion_points >= 1.5`` (the rubric's repeatable/mass/exile/token/flicker
    pieces all carry >=1.5; one-shot graveyard spells carry 1.0 and are NOT
    engines). We do not add the per-3-sticky-creature or battlefield-tutor-commander
    engine bonuses here (that needs richer signals); documented for Fable-5.
    """
    threats = sum(c.threat_weight for c in classified if c.is_threat)
    protection = sum(1 for c in classified if c.is_protection)
    board_protection = sum(1 for c in classified if c.is_board_protection)
    recursion_pts = sum(c.recursion_points for c in classified)
    rebuild_engines = sum(1 for c in classified if c.recursion_points >= 1.5)

    # Heavy draw is recovery (rubric): 32+ draw pts -> +1 recursion pt, 40+ -> +2.
    if draw_points >= 40:
        recursion_pts += 2.0
    elif draw_points >= 32:
        recursion_pts += 1.0

    row = 2.5  # glass default
    if threats >= 12 and protection >= 8 and recursion_pts >= 12 and rebuild_engines >= 4 and board_protection >= 3:
        row = 9.0
    elif threats >= 12 and protection >= 6 and recursion_pts >= 10 and rebuild_engines >= 3 and board_protection >= 2:
        row = 8.0
    elif threats >= 10 and protection >= 4 and recursion_pts >= 8 and rebuild_engines >= 2 and board_protection >= 1:
        row = 6.0
    elif threats >= 8 and protection >= 2 and recursion_pts >= 4:
        row = 5.0
    elif threats >= 6 and protection >= 1 and recursion_pts >= 2:
        row = 4.5
    elif threats >= 3:
        row = 3.5

    note = (
        f'combat: {threats:g} eff. threats, {protection} protection '
        f'({board_protection} board-level), {recursion_pts:g} recursion pts '
        f'w/ {rebuild_engines} rebuild engines -> row {row:g}'
    )
    return row, note


def _stax_row(classified: list[CardTiers]) -> tuple[float, str]:
    """Resilience via the stax/prison path (rubric "The stax/prison path").

    4 stax pieces -> 4 · 6 -> 5 · 8+ -> 6 (capped at 6). Stax membership is read
    from the Phase-1 ``stax`` tag.
    """
    stax = sum(1 for c in classified if 'stax' in c.tags)
    if stax >= 8:
        return 6.0, f'stax path: {stax} stax pieces -> row 6 (cap)'
    if stax >= 6:
        return 5.0, f'stax path: {stax} stax pieces -> row 5'
    if stax >= 4:
        return 4.0, f'stax path: {stax} stax pieces -> row 4'
    return 0.0, ''


def _answer_density_row(
    classified: list[CardTiers], draw_points: float, counterspell_count: int | None
) -> tuple[float, str]:
    """Resilience via the answer-density (draw-go control) path (rubric, cap 7).

    13+ interaction pieces w/ 5+ counterspells & 32+ draw pts -> 6; 16+/8+/40+ -> 7.
    Caps at 7. ``counterspell_count`` may be caller-supplied (Phase 4 has the richer
    otag view); when omitted it is derived from per-card stack points (a stack-point
    piece >=2 is a counter/free/turn-protection presence — an APPROXIMATION).
    """
    interaction = sum(1 for c in classified if c.is_interaction)
    if counterspell_count is None:
        counterspell_count = sum(1 for c in classified if c.is_interaction and c.stack_points >= 2)

    if interaction >= 16 and counterspell_count >= 8 and draw_points >= 40:
        return (
            7.0,
            f'answer-density: {interaction} interaction, {counterspell_count} counters, {draw_points:g} draw -> row 7',
        )
    if interaction >= 13 and counterspell_count >= 5 and draw_points >= 32:
        return (
            6.0,
            f'answer-density: {interaction} interaction, {counterspell_count} counters, {draw_points:g} draw -> row 6',
        )
    return 0.0, ''


#: `commander_dependence` string -> penalty (rubric "Commander Dependency Penalty":
#: None=0 runs fine commander-less/80%+; Moderate=-1 format default/50-80%; High=-2
#: <50% capacity).
_COMMANDER_DEPENDENCE_PENALTY: dict[str, float] = {
    'low': 0.0,  # None
    'med': -1.0,  # Moderate (format default)
    'high': -2.0,  # High
}


def resilience_axis(
    classified: list[CardTiers],
    *,
    tutor_points: float,
    combos: list,
    commander_dependence: str,
    draw_points: float = 0.0,
    counterspell_count: int | None = None,
    engine_exposure: int | None = None,
    archetype_caps: set[str] | None = None,
) -> CrispiAxis:
    """Score the Resilience axis (rubric "Resilience - Full Rubric").

    Best-supported row across the paths (combo layering · combat rows · stax ·
    answer-density), then archetype caps, then the engine-exposure and
    commander-dependence penalties (LAST).

    The Phase-4 wiring contract — every caller-supplied signal:
      * ``classified: list[CardTiers]`` — the nonland Phase-1 records (threats,
        recursion, protection, interaction, stax tags).
      * ``tutor_points: float`` — the Consistency axis's TOTAL tutor points (drives
        the combo assembly multiplier x1.0/0.75/0.5). Phase 4 passes the same tutor
        total it computed for Consistency.
      * ``combos: list[Combo]`` — the ``combo_detect.combos_in_deck(...)`` match list
        (each matched combo is one distinct win line). Phase 4 runs the detector.
      * ``commander_dependence: "low"|"med"|"high"`` — the AI-judged dependence
        (None/Moderate/High -> 0/-1/-2 penalty). One of the two judgement inputs.
      * ``draw_points: float`` — the Consistency draw-column total (32+ -> +1 / 40+
        -> +2 recursion points; also gates the answer-density path). Phase 4 passes
        its computed draw total.
      * ``counterspell_count: int | None`` — the deck's counterspell count for the
        answer-density path; Phase 4 supplies it from the otag view, else derived.
      * ``engine_exposure: int | None`` — a caller-supplied structural-exposure
        penalty (-1 or -2) computed by Phase 4 from otag concentration (45%+ one
        hoser class + <2 answers -> -2; answerable or 30%+ unanswerable -> -1). When
        None we do NOT self-derive (needs the otag concentration Phase 4 owns) — a
        conservative 0.
      * ``archetype_caps: set[str] | None`` — caller hint of archetype caps to apply
        ("stax" -> 6, "voltron" -> 7). Phase 4 detects the archetype.

    Returns a :class:`CrispiAxis` (value snapped to the quarter grid, clamped 1-10).
    """
    # Combat row is computed first so the combo channel can read it as a potential
    # backup channel (R3-3: a lone combo line + a combat path >=5 is "1 primary + 1
    # backup" -> row 7).
    combat_row, combat_note = _combat_row(classified, draw_points)
    combo_row, combo_note = _combo_layering_row(combos, tutor_points, combat_row)
    stax_row, stax_note = _stax_row(classified)
    density_row, density_note = _answer_density_row(classified, draw_points, counterspell_count)

    # Best-supported row across every path.
    paths = [
        (combo_row, combo_note),
        (combat_row, combat_note),
        (stax_row, stax_note),
        (density_row, density_note),
    ]
    base, base_note = max(paths, key=lambda p: p[0])
    if base <= 0.0:
        base, base_note = 2.5, 'no supported resilience path (glass)'

    parts = [n for _, n in paths if n]

    # --- Archetype caps (stax -> 6, Voltron -> 7). ---------------------------
    caps = {c.lower() for c in (archetype_caps or set())}
    if 'stax' in caps and base > 6.0:
        base = 6.0
        parts.append('stax archetype caps Resilience at 6')
    if 'voltron' in caps and base > 7.0:
        base = 7.0
        parts.append('Voltron archetype caps Resilience at 7')

    # --- Engine-exposure penalty (caller-supplied; -1 / -2). -----------------
    exposure = 0.0
    if engine_exposure is not None:
        exposure = max(-2.0, min(0.0, float(engine_exposure)))
        if exposure < 0:
            parts.append(f'engine-exposure penalty {exposure:g} (concentrated behind one hoser class)')

    # --- Commander-dependence penalty (rubric: applied LAST). ----------------
    dep_penalty = _COMMANDER_DEPENDENCE_PENALTY.get(commander_dependence.lower(), -1.0)
    if dep_penalty < 0:
        label = {'med': 'Moderate', 'high': 'High'}.get(commander_dependence.lower(), commander_dependence)
        parts.append(f'commander-dependence {label} -> {dep_penalty:g}')

    value = snap_quarter(base + exposure + dep_penalty)

    # Cite the cards on the binding path (combo win-cards, else threats/protection).
    cited: list[str] = []
    if base_note.startswith('combo') and combos:
        for c in combos:
            cited.extend(c.card_names)
    else:
        cited = [c.name for c in classified if c.is_threat or c.recursion_points > 0][:8]
    seen: set[str] = set()
    cited_unique = [n for n in cited if n and not (n in seen or seen.add(n))][:8]

    rationale = '. '.join(parts) + '.'
    return CrispiAxis(value=value, rationale=rationale, cited_cards=cited_unique)


# =========================================================================== #
# Phase 4 — the top-level scorer.
#
# `crispi_score` orchestrates the four axes + the Performance Index. It is a PURE
# function of its inputs — NO clock, NO randomness inside (``computed_at`` is a
# caller-stamped string, defaulted to '' so scoring stays deterministic; the verb
# stamps the real ISO timestamp). This is what makes "same deck + same inputs ->
# byte-identical output" a real property (the determinism test).
#
# Everything the four axes need beyond the Phase-1 `CardTiers` records is DERIVED
# here from otag slugs (scope coverage, counterspell count, symmetric-wipe count,
# engine exposure, archetype caps, combo-commander) — each documented inline as an
# audit surface for Fable-5 calibration.
# =========================================================================== #


#: otag slugs that answer ARTIFACTS (an artifact-scoped removal / bounce / exile).
#: A suite with any of these covers the artifact answer-scope class.
_ARTIFACT_ANSWER_SLUGS = frozenset(
    {'artifact-removal', 'destroy-artifact', 'exile-artifact', 'artifact-hate', 'destroy-artifact-or-enchantment'}
)
#: otag slugs that answer ENCHANTMENTS.
_ENCHANTMENT_ANSWER_SLUGS = frozenset(
    {
        'enchantment-removal',
        'destroy-enchantment',
        'exile-enchantment',
        'enchantment-hate',
        'destroy-artifact-or-enchantment',
    }
)
#: Text tells for a SYMMETRIC board wipe (hits every creature / all players).
_SYMMETRIC_WIPE_RE = re.compile(r'\ball creatures\b|each creature|destroy all|each player sacrifices')


def _scope_coverage(cards: list[dict], card_otag: dict[str, set[str]]) -> tuple[bool, bool, bool]:
    """Derive (creature, artifact, enchantment) answer-scope coverage from otags.

    APPROXIMATION (Fable-5 audit): CREATURE coverage = any removal/counterspell/
    stax/protection bucket present (those answer creatures at least indirectly).
    ARTIFACT / ENCHANTMENT coverage = a card whose slug closure carries an
    artifact-/enchantment-answer slug (:data:`_ARTIFACT_ANSWER_SLUGS` /
    :data:`_ENCHANTMENT_ANSWER_SLUGS`). Conservative: a generic ``removal`` tag with
    no artifact/enchantment slug does NOT claim those classes (unlike the axis's
    own optimistic fallback), so a deck with only creature removal correctly caps.
    """
    creature = artifact = enchantment = False
    for c in cards:
        slugs = _card_slugs(c, card_otag)
        buckets = buckets_for(slugs)
        if buckets & _SCOPE_CREATURE_TAGS:
            creature = True
        if slugs & _ARTIFACT_ANSWER_SLUGS:
            artifact = True
        if slugs & _ENCHANTMENT_ANSWER_SLUGS:
            enchantment = True
    return creature, artifact, enchantment


def _counterspell_count(cards: list[dict], card_otag: dict[str, set[str]]) -> int:
    """Count counterspells from the otag ``counterspells`` bucket (audit surface)."""
    return sum(1 for c in cards if 'counterspells' in buckets_for(_card_slugs(c, card_otag)))


def _symmetric_wipe_count(cards: list[dict], card_otag: dict[str, set[str]]) -> int:
    """Best-effort symmetric board-wipe count (Fable-5 audit surface).

    A card is a symmetric wipe iff it is a removal/board-wipe otag AND its oracle
    text reads as hitting EVERYTHING (``all creatures`` / ``destroy all`` / ``each
    player sacrifices``). Symmetry-in-context isn't fully readable from one card, so
    this is the conservative text tell; >=3 caps Interaction at 7 (rubric).
    """
    count = 0
    for c in cards:
        buckets = buckets_for(_card_slugs(c, card_otag))
        if 'removal' not in buckets:
            continue
        if _SYMMETRIC_WIPE_RE.search((c.get('oracle_text') or '').lower()):
            count += 1
    return count


#: Graveyard-dependence otag tells: a card whose plan reads OUT of the graveyard
#: (an engine that folds to Rest in Peace). Conservative — the affinity/matters
#: leaves, not incidental single-card recursion.
_GRAVEYARD_ENGINE_SLUGS = frozenset(
    {
        'affinity-for-graveyard',
        'cards-in-graveyard-matter',
        'card-types-in-graveyard-matter',
        'castable-from-graveyard',
        'activate-from-graveyard',
        'continuous-effect-from-graveyard',
    }
)


def _engine_exposure(
    cards: list[dict],
    card_otag: dict[str, set[str]],
    nonland_count: int,
    counterspell_count: int,
) -> int | None:
    """Derive the Resilience engine-exposure penalty from HOSER-class concentration.

    Rubric ~164 (Fable-5 audit surface): "a deck that concentrates its nonland engine
    behind one hoser class folds to a single resolved hate piece — graveyard
    dependence to Rest in Peace, artifact piles to Vandalblast, enchantment piles to
    mass enchantment removal. 45%+ of nonland cards in one class with fewer than two
    ways to answer ... = -2; answerable, or 30%+ and unanswerable = -1. WORST CLASS
    ONLY; creature concentration is exempt — and cards that fight as creature threats
    (creatures, crewed Vehicles, your commander) never count toward the artifact or
    enchantment share ... the penalty measures engine dependence, not what your threats
    are made of."

    So the concentration NUMERATOR counts only NONCREATURE hoser-answerable cards:
    noncreature Artifacts (Vandalblast), noncreature Enchantments (mass enchantment
    removal), and graveyard-engine cards (Rest in Peace). Creature/burn/typal/token/
    combat threat concentration is EXEMPT and never counts. "Answers" = the deck's
    counterspell count (a proxy for reactive coverage of the class).
    """
    if nonland_count <= 0:
        return None

    artifact = enchantment = graveyard = 0
    for c in cards:
        type_line = _type_line(c).lower()
        if 'land' in type_line and is_land(_type_line(c)):
            continue
        # A card that fights as a creature threat never counts toward the hoser share.
        is_creature_threat = 'creature' in type_line
        slugs = _card_slugs(c, card_otag)
        if 'artifact' in type_line and not is_creature_threat:
            artifact += 1
        if 'enchantment' in type_line and not is_creature_threat:
            enchantment += 1
        if slugs & _GRAVEYARD_ENGINE_SLUGS:
            graveyard += 1

    # Worst class only: the single largest NONCREATURE hoser-answerable concentration.
    top_n = max(artifact, enchantment, graveyard)
    if top_n <= 0:
        return None
    share = top_n / nonland_count
    answerable = counterspell_count >= 2
    if share >= 0.45 and not answerable:
        return -2
    if share >= 0.45 or share >= 0.30:
        return -1
    return None


def _archetype_caps(classified: list[CardTiers], commander: CardTiers | None) -> set[str]:
    """Best-effort archetype detection for the Resilience caps (Fable-5 audit).

    STAX -> a stax-heavy shell (>=6 cards carrying the ``stax`` tag). VOLTRON -> the
    plan concentrates on a single threat: a protection-heavy, low-threat-count board
    (>=4 protection pieces AND <=6 real threats) reads as a suit-up-the-commander
    build. Both are conservative structural proxies; caps only ever LOWER a score.
    """
    caps: set[str] = set()
    stax = sum(1 for c in classified if 'stax' in c.tags)
    if stax >= 6:
        caps.add('stax')
    protection = sum(1 for c in classified if c.is_protection)
    threats = sum(1 for c in classified if c.is_threat)
    if protection >= 4 and threats <= 6:
        caps.add('voltron')
    return caps


# =========================================================================== #
# Phase 6 — Commander Bracket (1-5) classifier.
#
# The official WotC Commander Brackets (Exhibition / Core / Upgraded / Optimized
# / cEDH) are a RULE ladder: each low bracket bars certain cards/plans (Game
# Changers, mass land denial, extra turns, early 2-card combos). The CRISPI
# deep-dive layers "guardrail floors" on top: two numbers (Speed and the overall
# CRISPI score, plus one Consistency+Interaction pairing) that can only BUMP a
# deck UP into its proper bracket, never down. The final bracket is
# `max(rule-ceiling, highest CRISPI floor tripped)` — a deck is at least as high
# as its cards force it, and at least as high as its speed/power betray it.
#
# `bracket(...)` is a PURE function of the already-scored `CrispiResult` plus the
# five rule signals the caller counts from the deck + tiers. It names every
# signal that fired in `triggers` for full transparency (rubric ~77: "if your
# deck gets bumped you can see exactly why").
# =========================================================================== #


def _rule_ceiling(
    *,
    game_changers: int,
    two_card_combos: int,
    mass_land_denial: int,
    extra_turns: int,
    fundamental_turn: float,
) -> tuple[int, list[str]]:
    """The official WotC rule ceiling — the LOWEST bracket the deck's cards permit.

    Rules (from ``research/commander-brackets.txt``), read as "which bracket's
    restrictions does the deck still satisfy" pushed UP by every violation:

      * Bracket 4 (Optimized) has NO card restrictions, so any of the following
        forces the ceiling to at least 4:
          - 4+ Game Changers (B3 allows 0-3),
          - any mass land denial (barred B1-B3),
          - a 2-card combo that can land BEFORE turn ~6 (B3 permits 2-card combos
            only from ~turn 6 on; ``fundamental_turn < 6`` => early).
      * Otherwise, any of the following bars B1/B2 and forces the ceiling to B3:
          - 1-3 Game Changers (allowed at B3, barred at B1/B2),
          - any extra-turn card (B1 bars all; B2 bars CHAINING — we conservatively
            treat any extra-turn card as a B3 signal, since we can't prove a deck
            never chains),
          - a 2-card combo landing at/after turn ~6 (permitted at B3, barred B1/B2).
      * A fully clean deck has a rule ceiling of B1 (the CRISPI floors then decide
        how far up it really sits).

    Returns ``(ceiling, triggers)`` — the numeric ceiling and the named rule signals.
    """
    triggers: list[str] = []
    early_combo = two_card_combos > 0 and float(fundamental_turn) < 6.0
    late_combo = two_card_combos > 0 and float(fundamental_turn) >= 6.0

    ceiling = 1

    # --- B4 forcers (Optimized: no restrictions). ----------------------------
    if game_changers >= 4:
        ceiling = max(ceiling, 4)
        triggers.append(f'rule: {game_changers} Game Changers (4+ -> B4)')
    if mass_land_denial > 0:
        ceiling = max(ceiling, 4)
        triggers.append(f'rule: {mass_land_denial} mass land denial (barred B1-B3 -> B4)')
    if early_combo:
        ceiling = max(ceiling, 4)
        triggers.append(f'rule: 2-card combo lands before turn 6 (fundamental turn {float(fundamental_turn):g} -> B4)')

    # --- B3 forcers (Upgraded: 0-3 GC, no MLD, combos only from ~turn 6). -----
    if 1 <= game_changers <= 3:
        ceiling = max(ceiling, 3)
        triggers.append(f'rule: {game_changers} Game Changer(s) (1-3 -> B3)')
    if extra_turns > 0:
        ceiling = max(ceiling, 3)
        triggers.append(f'rule: {extra_turns} extra turn card(s) (barred B1/B2 -> B3)')
    if late_combo:
        ceiling = max(ceiling, 3)
        triggers.append(f'rule: 2-card combo (fundamental turn {float(fundamental_turn):g} >= 6 -> B3)')

    return ceiling, triggers


def _crispi_floor(result: CrispiResult) -> tuple[int, list[str]]:
    """The highest CRISPI guardrail floor a deck's numbers trip (bump-UP only).

    From ``research/crispi-deep-dive-full.txt`` ("CRISPI floors"):

      * B5 floor: Speed 9+ OR CRISPI 8.5+.
      * B4 floor: Speed 8+ OR CRISPI 7.0+ OR (Consistency 7.5+ AND Interaction 7.5+).
      * B3 floor: Speed 6+ OR CRISPI 5.0+.
      * B2 floor: Speed 5+ OR CRISPI 3.5+.

    Half-step Speed "gets the benefit of the doubt": a rating like 8.5 counts as a
    turn-4 win (the SLOWER of turns 3/4), so an 8.5 clears the B4 Speed floor but
    NOT B5's (which wants a true Speed 9 = turn-3 win). We implement that by using
    ``floor(speed)`` for the Speed-floor comparison — 8.5 -> 8, 7.5 -> 7 — exactly
    the rubric's "count the slower turn" rule.

    Returns ``(floor, triggers)`` — the highest floor bracket (1 if none trips) and
    the named floor signals that fired.
    """
    speed = result.speed.value
    pi = result.performance_index
    cons = result.consistency.value
    interaction = result.interaction.value
    speed_floor = math.floor(speed)  # half-step benefit-of-the-doubt (8.5 -> 8).

    floor = 1
    triggers: list[str] = []

    # B5 floor.
    if speed_floor >= 9:
        floor = max(floor, 5)
        triggers.append(f'B5 floor: Speed {speed:g} (>=9)')
    if pi >= 8.5:
        floor = max(floor, 5)
        triggers.append(f'B5 floor: CRISPI {pi:g} (>=8.5)')

    # B4 floor.
    if speed_floor >= 8:
        floor = max(floor, 4)
        triggers.append(f'B4 floor: Speed {speed:g} (>=8)')
    if pi >= 7.0:
        floor = max(floor, 4)
        triggers.append(f'B4 floor: CRISPI {pi:g} (>=7.0)')
    if cons >= 7.5 and interaction >= 7.5:
        floor = max(floor, 4)
        triggers.append(f'B4 floor: Consistency {cons:g} + Interaction {interaction:g} (both >=7.5)')

    # B3 floor.
    if speed_floor >= 6:
        floor = max(floor, 3)
        triggers.append(f'B3 floor: Speed {speed:g} (>=6)')
    if pi >= 5.0:
        floor = max(floor, 3)
        triggers.append(f'B3 floor: CRISPI {pi:g} (>=5.0)')

    # B2 floor.
    if speed_floor >= 5:
        floor = max(floor, 2)
        triggers.append(f'B2 floor: Speed {speed:g} (>=5)')
    if pi >= 3.5:
        floor = max(floor, 2)
        triggers.append(f'B2 floor: CRISPI {pi:g} (>=3.5)')

    return floor, triggers


def _name_list_count(cards: list[dict], names: frozenset[str]) -> int:
    """Count deck cards whose (normalized) name is in a bracket rule name-list.

    Counts by deck ROW (a single copy of a Game Changer / MLD / extra-turn card is
    a violation regardless of quantity). Names are normalized with the same
    :func:`normalize_card_name` the name-lists were built with, so punctuation/case
    don't matter and a DFC front-face name matches its list entry.
    """
    return sum(1 for c in cards if normalize_card_name(c.get('name') or '') in names)


def _two_card_combo_count(combos: list) -> int:
    """Count DISTINCT 2-card combo lines in the deck (the bracket rule signal).

    A bracket "2-card combo" is a registered combo whose concrete card set is
    exactly two cards. Distinctness is by ``variant_id`` (matching the Resilience
    combo-layering count). Larger combos (3+ cards) are NOT bracket 2-card combos —
    the official rules restrict specifically the two-card variety.
    """
    seen: set[str] = set()
    count = 0
    for c in combos:
        names = getattr(c, 'card_names', ()) or ()
        vid = getattr(c, 'variant_id', None)
        if len(names) == 2 and vid not in seen:
            seen.add(vid)
            count += 1
    return count


def bracket(
    result: CrispiResult,
    *,
    game_changers: int,
    two_card_combos: int,
    mass_land_denial: int,
    extra_turns: int,
    tutor_count: int,
) -> CrispiBracket:
    """Classify a scored deck into the official WotC Commander Bracket (1-5).

    Floor-only-upward: the final bracket is ``max(rule-ceiling, highest CRISPI
    floor)``. The rule ceiling is the lowest bracket the deck's CARDS permit
    (Game Changers, mass land denial, extra turns, early 2-card combos); the
    CRISPI floors are the two-number guardrails that bump a compliant-but-strong
    (or -fast) deck up into its true bracket. Both only ever push UP — a weak deck
    with four Game Changers is still B4 (rubric ~77), and a strong deck is never
    dragged below what its cards allow.

    Args:
        result: The scored :class:`CrispiResult` (its axis values + PI + the
            ``inputs.fundamental_turn`` drive the floors and the B3 combo nuance).
        game_changers: Count of deck cards on the official WotC Game Changers list.
        two_card_combos: Count of detected 2-card combo lines in the deck (the
            fundamental turn decides whether they land "before turn 6" -> B4, or
            at/after -> B3).
        mass_land_denial: Count of mass-land-denial cards (any -> B4 ceiling).
        extra_turns: Count of extra-turn cards (any -> B3 ceiling).
        tutor_count: The deck's total tutor count (reserved / informational — the
            official ceilings key on Game Changers, not raw tutors; carried so the
            signal is available to the caller and future rubric revisions).

    Returns:
        A :class:`CrispiBracket` with the 1-5 bracket and the named ``triggers``
        (every rule + floor signal that fired, so a bump is always explainable).
    """
    fundamental_turn = float(result.inputs.get('fundamental_turn', 9.0))

    ceiling, rule_triggers = _rule_ceiling(
        game_changers=game_changers,
        two_card_combos=two_card_combos,
        mass_land_denial=mass_land_denial,
        extra_turns=extra_turns,
        fundamental_turn=fundamental_turn,
    )
    floor, floor_triggers = _crispi_floor(result)

    value = max(ceiling, floor, 1)

    # Name the deciding signal(s): the rule triggers at/above the final bracket and
    # the floor triggers at/above it (the ones that actually SET the bracket).
    triggers = list(rule_triggers) + list(floor_triggers)
    if not triggers:
        triggers = [f'no bump: clean deck, CRISPI {result.performance_index:g} / Speed {result.speed.value:g} -> B1']

    return CrispiBracket(bracket=value, triggers=triggers)


def crispi_score(
    cards: list[dict],
    card_otag: dict[str, set[str]],
    *,
    fundamental_turn: float,
    commander_dependence: str,
    commander_card: dict | None = None,
    mana_facts: dict | None = None,
    combos: list | None = None,
    computed_at: str = '',
) -> CrispiResult:
    """Score a deck across all four CRISPI axes + the Performance Index (PURE).

    The top-level orchestrator: classify every nonland card, identify the
    commander's tiers, then run Consistency / Interaction / Speed / Resilience with
    the Phase-2/3 wiring contracts satisfied from the derived otag view. The PI is
    the mean of the four axis values snapped to the quarter grid.

    Determinism: this function contains NO clock and NO randomness. ``computed_at``
    is a caller-stamped string (default ``''``) — the verb stamps the real ISO
    timestamp — so "same inputs -> identical output" holds (excluding that stamp).

    Args:
        cards: The deck's card dicts (name/oracle_id/oracle_text/type_line/cmc/
            keywords/produced_mana/mana_cost/power/is_commander). Lands may be
            present — the classifier returns them inert and the axes count nonlands.
        card_otag: ``str(oracle_id) -> set[slug]`` closure. May be empty ({}); the
            axes then compute from structured facts only (graceful degradation).
        fundamental_turn: The AI-judged fundamental turn (float; drives Speed).
        commander_dependence: ``"low"|"med"|"high"`` (drives the Resilience penalty).
        commander_card: The commander's card dict (else the ``is_commander`` flag on
            a card in ``cards`` identifies it). Used to classify the commander's tiers.
        mana_facts: The structured-mana dict Consistency consumes (land/rock/dork
            counts, avg_cmc, pip_pressure). Defaults to an empty neutral dict.
        combos: The ``combos_in_deck`` match list (each a distinct win line) for
            Resilience. Defaults to [] (sparse combo lake -> combat path still scores).
        computed_at: Freshness stamp string. Default '' (deterministic sentinel).

    Returns:
        A :class:`CrispiResult` with the four axes, the snapped PI, ``bracket=None``
        (Phase 6), the two ``inputs``, and ``computed_at``.
    """
    mana_facts = mana_facts or {}
    combos = combos or []

    # --- Classify the nonland cards; find the commander. ---------------------
    # A card is nonland iff its front face is not a land (lands never carry CRISPI
    # tiers — the classifier returns them inert and the axes count nonlands).
    nonland_cards = [c for c in cards if not is_land(_type_line(c))]
    classified = [classify_card(c, card_otag) for c in nonland_cards]

    commander_dict = commander_card
    if commander_dict is None:
        commander_dict = next((c for c in cards if c.get('is_commander')), None)
    commander = classify_card(commander_dict, card_otag) if commander_dict is not None else None

    # A combo the commander participates in (combo-commander lift on both columns).
    combo_commander = False
    if commander_dict is not None and combos:
        cmd_name = (commander_dict.get('name') or '').strip().casefold()
        cmd_oid = str(commander_dict.get('oracle_id') or '')
        for combo in combos:
            names = {(n or '').strip().casefold() for n in getattr(combo, 'card_names', ())}
            oids = {str(o) for o in getattr(combo, 'card_oracle_ids', ()) if o}
            if (cmd_name and cmd_name in names) or (cmd_oid and cmd_oid in oids):
                combo_commander = True
                break

    # --- Shared draw / tutor totals (Consistency <-> Resilience). ------------
    totals = consistency_totals(classified, commander, combo_commander=combo_commander)

    # --- Consistency. --------------------------------------------------------
    consistency = consistency_axis(classified, commander, mana_facts, combo_commander=combo_commander)

    # --- Interaction (derive scope / counters / symmetric wipes from otags). --
    scope_creature, scope_artifact, scope_enchantment = _scope_coverage(nonland_cards, card_otag)
    counterspell_count = _counterspell_count(nonland_cards, card_otag)
    symmetric_wipe_count = _symmetric_wipe_count(nonland_cards, card_otag)
    interaction = interaction_axis(
        classified,
        symmetric_wipe_count=symmetric_wipe_count,
        scope_creature=scope_creature,
        scope_artifact=scope_artifact,
        scope_enchantment=scope_enchantment,
        counterspell_count=counterspell_count,
    )

    # --- Resilience (shares tutor / draw totals; derives exposure + caps). ---
    engine_exposure = _engine_exposure(nonland_cards, card_otag, len(nonland_cards), counterspell_count)
    archetype_caps = _archetype_caps(classified, commander)
    resilience = resilience_axis(
        classified,
        tutor_points=totals.tutor_total,
        combos=combos,
        commander_dependence=commander_dependence,
        draw_points=totals.draw_total,
        counterspell_count=counterspell_count,
        engine_exposure=engine_exposure,
        archetype_caps=archetype_caps,
    )

    # --- Speed (consumes the already-computed Consistency value for the cap). -
    speed = speed_axis(fundamental_turn, consistency_value=consistency.value)

    # --- Performance Index: mean of the four axes, snapped to the quarter grid. -
    pi_raw = (consistency.value + interaction.value + speed.value + resilience.value) / 4.0
    performance_index = round(snap_quarter(pi_raw), 2)

    result = CrispiResult(
        consistency=consistency,
        interaction=interaction,
        speed=speed,
        resilience=resilience,
        performance_index=performance_index,
        bracket=None,
        inputs={'fundamental_turn': float(fundamental_turn), 'commander_dependence': commander_dependence},
        computed_at=computed_at,
    )

    # --- Commander Bracket (Phase 6): rule ceiling vs CRISPI floors. ----------
    # Rule signals are counted over ALL deck cards (Game Changers / MLD / extra
    # turns can be lands or nonlands); the tutor count reuses the Consistency
    # tutor total already computed above (informational to the bracketer).
    game_changers = _name_list_count(cards, GAME_CHANGERS)
    mass_land_denial = _name_list_count(cards, MASS_LAND_DENIAL)
    extra_turns = _name_list_count(cards, EXTRA_TURNS)
    two_card_combos = _two_card_combo_count(combos)
    result = result.model_copy(
        update={
            'bracket': bracket(
                result,
                game_changers=game_changers,
                two_card_combos=two_card_combos,
                mass_land_denial=mass_land_denial,
                extra_turns=extra_turns,
                tutor_count=int(totals.tutor_total),
            )
        }
    )
    return result


__all__ = (
    'CardTiers',
    'ConsistencyTotals',
    'bracket',
    'classify_card',
    'consistency_axis',
    'consistency_totals',
    'crispi_score',
    'interaction_axis',
    'interp',
    'resilience_axis',
    'snap_quarter',
    'speed_axis',
)
