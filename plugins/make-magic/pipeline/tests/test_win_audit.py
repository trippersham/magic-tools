"""Honest-win-rate audit — pure-function tests (no store, no JVM).

Uses the shared transcript fixtures: ``driven_combo_win`` is a macro-fire spurious win
(fake), ``driverless_loss`` is a real lethal (loser at 0). The audit must exclude fakes
from both numerator and denominator so the honest win-rate is not inflated.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.sim.win_audit import DeckAudit, audit_rows

_FIX = Path(__file__).parent / 'fixtures' / 'transcripts'
FAKE_WIN = (_FIX / 'driven_combo_win.log').read_text()  # macro fires, opp alive at 20 -> fake
REAL_KILL = (_FIX / 'driverless_loss.log').read_text()  # winner=b, loser (A) at 0 -> real


def test_audit_excludes_fake_macro_wins() -> None:
    # deck X: 2 fake macro wins (A credited), 1 real A-kill, 1 real opp win, 1 draw (ignored).
    rows = [
        ('deckX', 'a', FAKE_WIN),
        ('deckX', 'a', FAKE_WIN),
        (
            'deckX',
            'a',
            REAL_KILL.replace('winner=PlayerB', 'winner=PlayerA').replace('lifeA=0 lifeB=14', 'lifeA=14 lifeB=0'),
        ),
        ('deckX', 'b', REAL_KILL),
        ('deckX', 'draw', FAKE_WIN),  # non-credited winner -> not decided, ignored
    ]
    audits = audit_rows(rows)
    a = audits['deckX']
    assert a.decided == 4  # the draw row is excluded
    assert a.fake == 2
    assert a.real_wins == 1
    assert a.opp_wins == 1
    assert a.honest_decided == 2
    # naive counts the 2 fakes as A wins: (1 real + 2 fake) / 4 = 75%.
    assert a.naive_winrate == 0.75
    # honest excludes the fakes entirely: 1 / (4 - 2) = 50%.
    assert a.honest_winrate == 0.5


def test_audit_all_fake_has_zero_honest_but_defined() -> None:
    audits = audit_rows([('d', 'a', FAKE_WIN), ('d', 'a', FAKE_WIN)])
    a = audits['d']
    assert (a.decided, a.fake, a.honest_decided) == (2, 2, 0)
    assert a.naive_winrate == 1.0
    assert a.honest_winrate is None  # no honest decided games -> undefined, not a divide-by-zero


def test_audit_empty() -> None:
    assert audit_rows([]) == {}
    assert isinstance(DeckAudit('d', 0, 0, 0, 0).naive_winrate, type(None))
