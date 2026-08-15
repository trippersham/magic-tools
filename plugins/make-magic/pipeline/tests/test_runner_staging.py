"""OFFLINE test that ``run_matchup`` stages decks under filesystem-safe stems (0.3).

Forge is never launched: ``subprocess.Popen`` is mocked to return a one-game log,
and ``decks_dir`` is redirected to a tmp dir. The assertion is that a deck named
with ``/`` and ``:`` writes real files (not a spurious sub-directory) and still
parses its winner (slot-keyed, so the spaced/slashed name is fine).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

import pytest

from pipeline.sim import runner as runner_mod
from pipeline.sim.forge_runtime import ForgeInstall
from pipeline.sim.runner import run_matchup


class _Proc:
    """A fake ``Popen``: one staged game's log, exits 0, never spawns anything."""

    returncode = 0
    pid = 4242

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return (
            'Simulation mode\nGame Result: Game 1 ended in 5 ms. Ai(1)-U/R Izzet has won!\n',
            '',
        )


class _CapturingProc(_Proc):
    """A fake ``Popen`` that records the argv it was launched with (class attr)."""

    last_cmd: ClassVar[list[str]] = []

    def __init__(self, cmd: list[str], *args: object, **kwargs: object) -> None:
        super().__init__(cmd, *args, **kwargs)
        type(self).last_cmd = list(cmd)


def _capture_cmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fmt: str = 'constructed',
) -> tuple[list[str], Path]:
    """Run a mocked matchup and return ``(argv, decks_root)``."""
    decks_root = tmp_path / 'decks'
    monkeypatch.setattr(ForgeInstall, 'decks_dir', property(lambda self: decks_root))
    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _CapturingProc)
    _CapturingProc.last_cmd = []
    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    # n=1 so the single-game fake log satisfies the runner's games==n check; the
    # captured argv still carries -n 1 (the value is asserted separately below).
    run_matchup(
        install,
        ('A', '[Main]\n1 Lightning Bolt\n'),
        ('B', '[Main]\n1 Grizzly Bears\n'),
        n=1,
        seed=42,
        fmt=fmt,
        timeout_s=25,
    )
    return _CapturingProc.last_cmd, decks_root


# --------------------------------------------------------------------------- #
# command shape — launches the sim-AI HARNESS (-cp … SimAIMatch -sim 1),
# NOT the stock `-jar … sim` verb; harness jar first; no -s seed.
# --------------------------------------------------------------------------- #


def test_runner_launches_harness_via_classpath(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, _ = _capture_cmd(tmp_path, monkeypatch)

    # Real sim-AI harness, not the stock heuristic `sim` verb.
    assert '-cp' in cmd, cmd
    assert 'sim' not in cmd, f'must not use the stock `sim` verb: {cmd}'
    assert '-jar' not in cmd, f'must launch via -cp (so classpath ordering holds), not -jar: {cmd}'
    assert runner_mod._HARNESS_MAIN_CLASS in cmd

    # Classpath: harness jar FIRST so its StaticAbilityContinuous shadow wins.
    classpath = cmd[cmd.index('-cp') + 1]
    parts = classpath.split(os.pathsep)
    assert parts[0] == str(runner_mod._HARNESS_JAR), f'harness jar must be first: {classpath}'
    assert parts[1].endswith('forge.jar'), f'Forge jar must follow the harness: {classpath}'
    # Main-Class comes immediately after the classpath.
    assert cmd[cmd.index('-cp') + 2] == runner_mod._HARNESS_MAIN_CLASS


def test_runner_passes_decks_n_clock_and_sim_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, decks_root = _capture_cmd(tmp_path, monkeypatch)

    # The harness reads `-d` as a filesystem path, so ABSOLUTE staged paths (not
    # bare profile stems) are passed — pointing at the two files it wrote.
    d = cmd.index('-d')
    cdir = decks_root / 'constructed'
    assert cmd[d + 1] == str(cdir / 'A.dck') and cmd[d + 2] == str(cdir / 'B.dck'), cmd
    assert Path(cmd[d + 1]).is_absolute() and Path(cmd[d + 1]).is_file()
    assert cmd[cmd.index('-n') + 1] == '1'  # n threaded through
    assert cmd[cmd.index('-c') + 1] == '25'  # timeout_s becomes Forge's -c draw clock
    assert cmd[cmd.index('-sim') + 1] == '1'  # sim AI ON


def test_runner_drops_seed_arg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The harness takes no seed; -s must NOT be passed (seed lives in matchup_key)."""
    cmd, _ = _capture_cmd(tmp_path, monkeypatch)
    assert '-s' not in cmd, f'harness has no seed arg — -s must be dropped: {cmd}'
    assert '42' not in cmd, f'the seed value must not leak into argv: {cmd}'


def test_runner_constructed_omits_format_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, _ = _capture_cmd(tmp_path, monkeypatch, fmt='constructed')
    assert '-f' not in cmd, f'constructed is the harness default; -f must be omitted: {cmd}'


def test_runner_commander_passes_format_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, _ = _capture_cmd(tmp_path, monkeypatch, fmt='commander')
    f = cmd.index('-f')
    assert cmd[f + 1] == 'commander', cmd


def test_run_matchup_stages_under_sanitized_stem(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    decks_root = tmp_path / 'decks'
    monkeypatch.setattr(ForgeInstall, 'decks_dir', property(lambda self: decks_root))
    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _Proc)

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
    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _Proc)

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    # 'A/B' and 'A:B' both sanitize to 'A_B' — distinct texts must not clobber.
    run_matchup(install, ('A/B', '[Main]\n1 Lightning Bolt\n'), ('A:B', '[Main]\n1 Grizzly Bears\n'), n=1, seed=1)

    cdir = decks_root / 'constructed'
    staged = sorted(p.name for p in cdir.glob('*.dck'))
    assert len(staged) == 2  # two distinct files, no overwrite
