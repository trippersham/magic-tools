"""The committed slug -> functional-bucket crosswalk (human-auditable).

Per the otag-adoption research (§"How to use them" + §"Caveats"), a card's
functional role is the set of ANCESTOR tag slugs it rolls up to (roots carry ~0
direct taggings, so you MUST roll leaves up the DAG first — see
``otag_rollup``). This module maps those slugs to our deck-balance buckets
(``removal / ramp / draw / tokens / counters / burn / tutor / sac /
counterspells / flicker / typal / anthem / combat / protection`` plus the three
flagged gap buckets ``stax / extra_combat / wincon``). It is deliberately a
plain, committed Python dict so the curation is reviewable in a diff and slug
renames surface loudly.

Some buckets key on a broad ROOT slug (``removal``, ``ramp``, ``card-advantage``,
``tutor``, ``counterspell``); others fold in a LEAF-slug family where the tags do
NOT all roll up to a single root (``typal-*``, ``gives-pp-counters`` &
``repeatable-pp-counters`` for counters, ``repeatable-token-generator`` for
tokens, the combat/anthem/protection grant-families). This "broaden the bucket
set" curation is exactly the research's recommended way to close the residual
(§"The residual"): on PTTD it lifts otag coverage from ~44% (roots only) to
~92%.

Design decisions carried from the research:

    - **Multi-label is a feature.** ``buckets_for`` returns a SET — Cultivate is
      ``ramp`` AND ``tutor``; a removal spell that draws is ``removal`` AND
      ``draw``. Deck-balance counting wants the overlap, not a single label.
    - **damage != life-loss.** The ``burn`` bucket folds in ``burn`` (direct
      damage) PLUS ``opponent-loses-life`` PLUS ``drain-life`` (life loss), so
      Torment of Hailfire / Exsanguinate land in ``burn`` even though they deal
      no damage. Miss this and you miss the whole aristocrat/X-drain plan.
    - **Key on slug for the bucket map** (human-auditable), on UUID for the DAG
      traversal (durable). Slugs can be community-renamed; a drifted slug simply
      stops matching (fails safe, visibly) rather than mis-buckets.

FLAGGED GAPS (the research's "no clean root" set — curated heuristics, not roots):

    - ``stax``       : no ``stax`` root exists. Heuristic = the ``tax`` slug plus
      the ``hate-*`` / ``can't`` family. We seed ``tax`` and a small set of
      resource-denial slugs; this is INTENTIONALLY conservative and flagged for
      later curation (``GAP_BUCKETS``).
    - ``extra_combat``: only the lone leaf ``extra-combat-phase`` exists (no
      root, no ancestors). We map that single slug directly.
    - ``wincon``     : there is no general ``wincon`` root (only
      ``alternate-win-condition`` + a few cycle tags). We map
      ``alternate-win-condition``; a *general* wincon detector is out of scope
      and left to the reasoning layer.

These three buckets are listed in ``GAP_BUCKETS`` so downstream code (and this
report) can flag that their coverage is heuristic, not root-backed.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# The crosswalk: bucket name -> the set of ancestor/leaf SLUGS that trigger it.
#
# A card lands in a bucket iff the closure of its tag slugs (leaf + all DAG
# ancestors) intersects that bucket's slug set. Slugs here are verified to exist
# in the committed oracle-tags DAG snapshot (see tests/test_transforms.py).
# --------------------------------------------------------------------------- #

BUCKET_ROOTS: dict[str, frozenset[str]] = {
    # Removal — spot + sweepers roll up to the `removal` root.
    "removal": frozenset({"removal"}),
    # Ramp — mana acceleration / land-into-play (land-ramp/mana-rock roll up to `ramp`).
    "ramp": frozenset({"ramp"}),
    # Card advantage / draw.
    "draw": frozenset({"card-advantage"}),
    # Token generation. `token-increaser` is the root, but the repeatable-token
    # LEAF family does NOT roll up to it, so fold those leaves in directly
    # (research §"broaden the bucket set").
    "tokens": frozenset(
        {"token-increaser", "repeatable-token-generator", "repeatable-creature-tokens"}
    ),
    # +1/+1 (and similar) counter strategies. Beyond the `counter-increaser` root,
    # the gives-/gains-/repeatable-pp-counters LEAF family carries most real
    # counter cards (they don't all roll up to the root) — fold them in.
    "counters": frozenset(
        {
            "counter-increaser",
            "pp-counters-matter",
            "counters-matter",
            "gives-pp-counters",
            "gives-pp-counters-to-all",
            "gains-pp-counters",
            "repeatable-pp-counters",
        }
    ),
    # Burn / reach — CRITICAL: damage OR life-loss (see module docstring).
    "burn": frozenset({"burn", "opponent-loses-life", "drain-life"}),
    # Tutors (all tutor-* leaves roll up to `tutor`).
    "tutor": frozenset({"tutor"}),
    # Sacrifice outlets / aristocrat engines.
    "sac": frozenset(
        {"sacrifice-outlet", "free-sacrifice-outlet", "repeatable-sacrifice-outlet"}
    ),
    # Hard counters (a distinct interaction axis from removal).
    "counterspells": frozenset({"counterspell"}),
    # Flicker / blink value engines (ETB reuse).
    "flicker": frozenset({"flicker"}),
    # Typal / tribal payoff — a core deck plan (PTTD is a changeling/typal deck).
    # There is no single `typal` root that all typal cards roll up to, so we key
    # on the typal LEAF family (research: fold in `typal-share`).
    "typal": frozenset(
        {
            "typal",
            "typal-creature",
            "typal-hero",
            "typal-share",
            "noncreature-typal",
            "changeling",
        }
    ),
    # Anthem / team buff — static or repeatable board-wide power/toughness pump.
    "anthem": frozenset({"power-boost-to-all", "toughness-boost-to-all", "anthem"}),
    # Combat / aggro enablers — extra combat damage, evasion granting, attack payoffs.
    "combat": frozenset(
        {
            "attacking-matters",
            "attacking-matters-self",
            "attack-trigger",
            "gives-double-strike",
            "gives-evasion",
            "gives-flying",
            "gives-trample",
            "gives-menace",
        }
    ),
    # Protection — grant hexproof/indestructible/ward or protect-target effects.
    # (Distinct from `removal`; this is the resilience axis susceptibility keys on.)
    "protection": frozenset(
        {
            "protection",
            "protects-creature",
            "protects-all",
            "gives-indestructible",
            "gives-hexproof",
        }
    ),
    # --- FLAGGED GAP BUCKETS (heuristic, not root-backed; see docstring) ------ #
    # stax / resource denial — no clean root; `tax` slug + denial family.
    "stax": frozenset({"tax", "cast-tax", "tax-attack", "tax-block"}),
    # extra combat — lone leaf, no ancestors.
    "extra_combat": frozenset({"extra-combat-phase"}),
    # wincon — only the alternate-win family has a tag; general wincon is reasoning-layer.
    "wincon": frozenset({"alternate-win-condition"}),
}

#: Buckets whose coverage is a curated heuristic, NOT a root rollup. Downstream
#: reporting flags these as lower-confidence (research: "no clean roots for
#: stax / extra-combat / wincon — need heuristics").
GAP_BUCKETS: frozenset[str] = frozenset({"stax", "extra_combat", "wincon"})

#: The canonical bucket vocabulary this crosswalk emits, in a stable order.
BUCKETS: tuple[str, ...] = tuple(BUCKET_ROOTS.keys())


def buckets_for(tags: set[str]) -> set[str]:
    """Return every bucket whose trigger slugs intersect ``tags`` (multi-label).

    Args:
        tags: The FULL slug closure for one card — its leaf tag slugs PLUS all
            their DAG ancestors (produced by ``otag_rollup``). Passing only the
            leaf slugs will under-match, because the bucket keys are the broad
            ancestor slugs (roots carry ~0 direct taggings).

    Returns:
        The set of bucket names the card counts in. Empty if none match — an
        honest "uncategorized" signal, not a guess.
    """
    return {bucket for bucket, slugs in BUCKET_ROOTS.items() if tags & slugs}
