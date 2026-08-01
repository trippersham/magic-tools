"""OFFLINE test that ``run_matchup`` stages decks under filesystem-safe stems (0.3).

Forge is never launched: ``subprocess.run`` is mocked to return a one-game log,
and ``decks_dir`` is redirected to a tmp dir. The assertion is that a deck named
with ``/`` and ``:`` writes real files (not a spurious sub-directory) and still
parses its winner (slot-keyed, so the spaced/slashed name is fine).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.sim import runner as runner_mod
from pipeline.sim.forge_runtime import ForgeInstall
from pipeline.sim.runner import run_matchup


class _Proc:
    returncode = 0
    stdout = 'Simulation mode\nGame Result: Game 1 ended in 5 ms. Ai(1)-U/R Izzet has won!\n'
    stderr = ''


def test_run_matchup_stages_under_sanitized_stem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    decks_root = tmp_path / 'decks'
    monkeypatch.setattr(ForgeInstall, 'decks_dir', property(lambda self: decks_root))
    monkeypatch.setattr(runner_mod.subprocess, 'run', lambda *a, **k: _Proc())

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    result = run_matchup(
        install,
        ('U/R Izzet', '[Main]\n1 Lightning Bolt\n'),
        ('Foe: Two', '[Main]\n1 Grizzly Bears\n'),
        n=1,
        seed=1,
    )

    cdir = decks_root / 'constructed'
    assert (cdir / 'U_R Izzet.dck').is_file()  # '/' sanitized, real file
    assert (cdir / 'Foe_ Two.dck').is_file()  # ':' sanitized
    assert not (cdir / 'U').exists()  # the '/' did NOT create a subdir (the bug)
    assert result.wins_a == 1  # slot-1 winner parsed despite the slashed/spaced name


def test_colliding_stems_are_disambiguated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    decks_root = tmp_path / 'decks'
    monkeypatch.setattr(ForgeInstall, 'decks_dir', property(lambda self: decks_root))
    monkeypatch.setattr(runner_mod.subprocess, 'run', lambda *a, **k: _Proc())

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    # 'A/B' and 'A:B' both sanitize to 'A_B' — distinct texts must not clobber.
    run_matchup(install, ('A/B', '[Main]\n1 Lightning Bolt\n'), ('A:B', '[Main]\n1 Grizzly Bears\n'), n=1, seed=1)

    cdir = decks_root / 'constructed'
    staged = sorted(p.name for p in cdir.glob('*.dck'))
    assert len(staged) == 2  # two distinct files, no overwrite
