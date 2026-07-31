"""OFFLINE tests for the deck format -> target-size mapping helper.

The mapping is TOLERANT by design: the live ``Format`` field's exact string
values are not yet known, so ``target_for_format`` normalizes (strip + casefold)
and matches on substring/equality rather than an exact enum. Empty / unknown
formats yield ``None`` (untargeted) so the audit layer treats them as WIP, not a
violation.
"""

from __future__ import annotations

import pytest

from pipeline.contracts.targets import target_for_format


@pytest.mark.parametrize(
    ('fmt', 'expected'),
    [
        ('Commander', 100),
        ('commander', 100),
        ('  Commander  ', 100),
        ('EDH', 100),
        ('edh', 100),
        ('Duel Commander', 100),
        ('Standard', 60),
        ('standard', 60),
        ('Modern', 60),
        ('Pioneer', 60),
        ('Brawl', 60),
        ('Historic', 60),
        ('Pauper', 60),
        ('', None),
        ('   ', None),
        (None, None),
        ('Weird', None),
        ('Limited', None),
    ],
)
def test_target_for_format(fmt: str | None, expected: int | None) -> None:
    assert target_for_format(fmt) == expected
