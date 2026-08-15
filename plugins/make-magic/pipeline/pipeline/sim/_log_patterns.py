"""Shared compiled regexes for parsing a Forge verbose game log.

These three patterns describe the game-terminator + winner grammar of a Forge
``sim`` log and are consumed by BOTH the tally (:mod:`pipeline.sim.runner`) and
the telemetry segmentation (:mod:`pipeline.sim.telemetry`). They lived as
byte-identical duplicates in each module; a future edit to one silently desynced
tally vs telemetry segmentation (the R2-1 class of bug). Consolidating them here
— ONE definition both modules import — makes that drift structurally impossible.

  * :data:`RESULT_RE`      — the normal terminator: ``Game Result: Game N ended
    in <ms> ms. <tail>`` (group 1 = elapsed ms, group 2 = tail carrying the
    winner). The ONLY line the tally counts as a decided game.
  * :data:`DRAW_RESULT_RE` — the GENUINE-draw terminator
    (``SimAIMatch.java:219``): ``Game Result: Game N ended in a Draw! Took <ms>
    ms.`` (group 1 = elapsed ms). Distinct wording, so it does NOT match
    :data:`RESULT_RE`; it is ALSO a game boundary and must be recognised or a
    draw game's lines merge into the next segment.
  * :data:`WINNER_RE`      — the winner tail within a terminator:
    ``Ai(<slot>)-<name> has won!`` (group 1 = slot; ``1`` = deck_a, ``2`` =
    deck_b). The name is matched non-greedily (``.+?``) so spaced/paren deck
    names parse — only the SLOT drives attribution.
"""

from __future__ import annotations

import re

__all__ = (
    'DRAW_RESULT_RE',
    'RESULT_RE',
    'WINNER_RE',
)

#: The ONLY line the tally counts: ``Game Result: Game N ended in <ms> ms. <tail>``.
RESULT_RE = re.compile(r'^Game Result: Game \d+ ended in (\d+) ms\. (.+)$')
#: A GENUINE draw terminator (``SimAIMatch.java:219``):
#: ``Game Result: Game N ended in a Draw! Took <ms> ms.`` — a real (non-clockout)
#: draw. It does NOT match :data:`RESULT_RE`, yet it IS a game terminator.
DRAW_RESULT_RE = re.compile(r'^Game Result: Game \d+ ended in a Draw! Took (\d+) ms\.$')
#: Winner tail: ``Ai(<slot>)-<name> has won!`` — slot 1 = deck_a, 2 = deck_b.
#: The name is matched non-greedily (``.+?``, NOT ``\S+``) so deck names with
#: spaces/parens parse — only the SLOT drives attribution.
WINNER_RE = re.compile(r'Ai\((\d)\)-.+? has won!')
