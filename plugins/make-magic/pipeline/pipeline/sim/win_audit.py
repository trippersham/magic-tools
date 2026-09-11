"""Honest-win-rate audit — exclude macro-fire spurious ("cheating driver") wins.

The driven combo macro fires on a mere ``applicable()`` precondition and the game is
credited a win with the opponent still ALIVE and the combo unassembled (see
``transcript_parser.is_fake_macro_win``). Those wins inflate every combo deck's reported
win-rate. This module recomputes an HONEST win-rate over the durable ``sim_game_logs``
by excluding fake macro wins (they are non-decisive — topped up, not credited), leaving
real lethal kills and genuine opponent concedes.

Read-only. The live enforcement (the harness emitting ``macro_game_over`` → ``INVALID``,
the goldfish requiring a real lethal) is separate; this audits data already on disk.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass

from pipeline.sim import transcript_parser as tp

__all__ = ('DeckAudit', 'audit_rows')


@dataclass(frozen=True)
class DeckAudit:
    """One deck's decided-game tally, split into real vs fabricated wins.

    ``decided`` counts games with a credited winner. ``fake`` are macro-attributed
    non-lethal wins (excluded from W/L). ``real_wins`` credits the subject seat (A) on a
    real terminal; ``opp_wins`` credits the opponent. ``naive_winrate`` counts the fakes
    as A-wins (today's inflated number); ``honest_winrate`` excludes them from numerator
    AND denominator (the topped-up, corrected number).
    """

    deck: str
    decided: int
    fake: int
    real_wins: int
    opp_wins: int

    @property
    def honest_decided(self) -> int:
        return self.decided - self.fake

    @property
    def naive_winrate(self) -> float | None:
        return (self.real_wins + self.fake) / self.decided if self.decided else None

    @property
    def honest_winrate(self) -> float | None:
        return self.real_wins / self.honest_decided if self.honest_decided else None


def audit_rows(rows: Iterable[tuple[str, str, str]]) -> dict[str, DeckAudit]:
    """Tally ``(deck_hash, winner, raw_log)`` rows into a per-deck :class:`DeckAudit`.

    ``winner`` is ``'a'`` / ``'b'`` / ``'draw'`` / ``'unknown'``; only credited (``a``/``b``)
    games count as decided. A row whose transcript is a fake macro win (``is_fake_macro_win``)
    is tallied as ``fake`` and credited to neither seat.
    """
    acc: dict[str, list[int]] = {}
    for deck, winner, raw_log in rows:
        if winner not in ('a', 'b'):
            continue
        t = acc.setdefault(deck, [0, 0, 0, 0])  # decided, fake, real_wins, opp_wins
        t[0] += 1
        if tp.is_fake_macro_win(raw_log):
            t[1] += 1
        elif winner == 'a':
            t[2] += 1
        else:
            t[3] += 1
    return {deck: DeckAudit(deck, d, f, rw, ow) for deck, (d, f, rw, ow) in acc.items()}


def _default_db() -> str:
    override = os.environ.get('MAKE_MAGIC_DATA_DIR')
    root = override or os.path.expanduser('~/.local/share/make-magic')
    return os.path.join(root, 'make_magic.duckdb')


def main() -> None:  # pragma: no cover - thin store runner
    """Print the honest-vs-naive win-rate per driven deck + a corpus total from the store."""
    import duckdb

    con = duckdb.connect(_default_db(), read_only=True)
    rows = con.execute(
        'select m.deck_a_hash, f.winner, l.raw_log '
        'from sim_game_features f '
        'join sim_game_logs l using(matchup_key, game_index) '
        'join sim_matchups m using(matchup_key)'
    ).fetchall()
    audits = audit_rows(rows)
    tot_dec = sum(a.decided for a in audits.values())
    tot_fake = sum(a.fake for a in audits.values())
    print(f'{"deck":<12}{"decided":>9}{"fake":>7}{"naive%":>9}{"honest%":>9}')
    for a in sorted(audits.values(), key=lambda x: -x.fake):
        if a.fake == 0:
            continue
        nw = f'{a.naive_winrate:.1%}' if a.naive_winrate is not None else '-'
        hw = f'{a.honest_winrate:.1%}' if a.honest_winrate is not None else '-'
        print(f'{a.deck[:11]:<12}{a.decided:>9}{a.fake:>7}{nw:>9}{hw:>9}')
    pct = f'{tot_fake / tot_dec:.1%}' if tot_dec else '-'
    print(f'\nCORPUS: decided={tot_dec} fake_macro_wins={tot_fake} ({pct}) honest_denominator={tot_dec - tot_fake}')


if __name__ == '__main__':  # pragma: no cover
    main()
