"""OFFLINE test that ``run_matchup`` stages decks in ISOLATION (R3-2).

Forge is never launched: ``subprocess.Popen`` is mocked to return a one-game log,
and ``runner.staging_root`` is redirected to a tmp dir. The assertions:

  * the ``-d`` paths are ABSOLUTE, ISOLATED (under the staged root, NOT the real
    Forge profile decks dir), and CONTENT-ADDRESSED;
  * two decks whose names sanitize to the SAME stem but carry DIFFERENT text do
    NOT collide (distinct staged paths);
  * the staged files are cleaned up after the run.
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


def _isolate_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the runner's staging root to a tmp dir; return it."""
    staging_root = tmp_path / 'staging'
    monkeypatch.setattr(runner_mod, 'staging_root', lambda: staging_root)
    return staging_root


def _capture_cmd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fmt: str = 'constructed',
) -> tuple[list[str], Path]:
    """Run a mocked matchup and return ``(argv, staging_root)``."""
    staging_root = _isolate_staging(tmp_path, monkeypatch)
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
    return _CapturingProc.last_cmd, staging_root


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


def test_runner_passes_isolated_absolute_deck_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd, staging_root = _capture_cmd(tmp_path, monkeypatch)

    # The harness reads `-d` as a filesystem path, so ABSOLUTE staged paths are
    # passed — pointing at the two files it wrote, UNDER the isolated staging root
    # (never the real Forge profile decks dir).
    d = cmd.index('-d')
    path_a, path_b = Path(cmd[d + 1]), Path(cmd[d + 2])
    assert path_a.is_absolute() and path_b.is_absolute(), cmd
    assert staging_root in path_a.parents and staging_root in path_b.parents, cmd
    # Content-addressed filenames (a 16-hex sha256 prefix + .dck), NOT the deck name.
    assert path_a.suffix == '.dck' and len(path_a.stem) == 16
    assert 'A.dck' not in cmd and 'B.dck' not in cmd  # human name never becomes the filename
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


def test_run_matchup_stages_under_isolated_root_and_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging_root = _isolate_staging(tmp_path, monkeypatch)
    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _Proc)

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    result = run_matchup(
        install,
        ('U/R Izzet', '[Main]\n1 Lightning Bolt\n'),
        ('Foe: Two', '[Main]\n1 Grizzly Bears\n'),
        n=1,
        seed=1,
    )

    assert result.wins_a == 1  # slot-1 winner parsed despite the slashed/spaced name
    # Nothing was written under the real Forge profile (we staged under the tmp root).
    assert not (Path.home() / 'Library' / 'Application Support' / 'Forge' / 'decks' / 'U').exists()
    # Best-effort cleanup removed the per-run dir (no staged .dck lingers).
    assert list(staging_root.rglob('*.dck')) == []


def test_colliding_stems_do_not_collide(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    staging_root = _isolate_staging(tmp_path, monkeypatch)

    staged_paths: list[str] = []

    class _RecordingProc(_Proc):
        def __init__(self, cmd: list[str], *args: object, **kwargs: object) -> None:
            super().__init__(cmd, *args, **kwargs)
            d = cmd.index('-d')
            staged_paths.extend([cmd[d + 1], cmd[d + 2]])

    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _RecordingProc)
    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    # 'A/B' and 'A:B' both sanitize to 'A_B' — distinct texts must map to distinct,
    # content-addressed staged paths (never the same file).
    run_matchup(install, ('A/B', '[Main]\n1 Lightning Bolt\n'), ('A:B', '[Main]\n1 Grizzly Bears\n'), n=1, seed=1)

    assert len(staged_paths) == 2
    assert staged_paths[0] != staged_paths[1], f'distinct decks collided: {staged_paths}'
    for p in staged_paths:
        assert staging_root in Path(p).parents


def test_identical_decks_share_one_staged_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A mirror match (identical text both sides) content-addresses to the SAME
    file — harmless (same bytes), and Forge reads the same path for both slots."""
    _isolate_staging(tmp_path, monkeypatch)

    staged_paths: list[str] = []

    class _RecordingProc(_Proc):
        def __init__(self, cmd: list[str], *args: object, **kwargs: object) -> None:
            super().__init__(cmd, *args, **kwargs)
            d = cmd.index('-d')
            staged_paths.extend([cmd[d + 1], cmd[d + 2]])

    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _RecordingProc)
    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    same = '[Main]\n1 Lightning Bolt\n'
    run_matchup(install, ('Mirror A', same), ('Mirror B', same), n=1, seed=1)

    assert staged_paths[0] == staged_paths[1]  # same content -> same content-addressed path


# --------------------------------------------------------------------------- #
# existence guard (Task 1.6c / adversary M1): a broken install (wheel that
# dropped the harness jar) must fail LOUDLY with an actionable error BEFORE the
# JVM is launched — never into the silent 0-0-0 table.
# --------------------------------------------------------------------------- #


def test_missing_harness_jar_fails_loudly_without_spawning_jvm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.sim.runner import ForgeError

    # Point _HARNESS_JAR at a path that does not exist (a jar-less install).
    missing = tmp_path / 'nope' / 'make-magic-forge-simai.jar'
    assert not missing.exists()
    monkeypatch.setattr(runner_mod, '_HARNESS_JAR', missing)
    _isolate_staging(tmp_path, monkeypatch)

    # If the guard fails to fire, this raises AssertionError instead of ForgeError.
    def _no_spawn(*args: object, **kwargs: object) -> object:
        raise AssertionError('JVM must NOT be spawned when the harness jar is missing')

    monkeypatch.setattr(runner_mod.subprocess, 'Popen', _no_spawn)

    install = ForgeInstall(forge_dir=tmp_path, jar=tmp_path / 'forge.jar', java=tmp_path / 'java')
    with pytest.raises(ForgeError) as excinfo:
        run_matchup(install, ('A', '[Main]\n1 Lightning Bolt\n'), ('B', '[Main]\n1 Grizzly Bears\n'), n=1, seed=1)

    msg = str(excinfo.value)
    assert 'make-magic-forge-simai.jar' in msg  # names the missing jar
    assert 'build.sh' in msg  # tells the user how to rebuild it


# --------------------------------------------------------------------------- #
# Stale-staging reaper: sweep orphans a SIGKILL bypassed (the finally cleanup)
# --------------------------------------------------------------------------- #


def test_reap_stale_staging_sweeps_only_old_run_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Orphaned ``run-*`` / ``xmage-*`` dirs past the age cutoff are reaped; fresh
    dirs and unrelated entries are left untouched (so a concurrent sim is safe)."""
    import os
    import time

    from pipeline.sim.runner import reap_stale_staging

    root = tmp_path / 'staging'
    root.mkdir()
    monkeypatch.setattr(runner_mod, 'staging_root', lambda: root)

    old_run = root / 'run-oldcrash'
    old_xmage = root / 'xmage-oldcrash'
    fresh_run = root / 'run-active'
    unrelated = root / 'keepme'
    for d in (old_run, old_xmage, fresh_run, unrelated):
        d.mkdir()
        (d / 'marker').write_text('x')
    # Age the two orphans well past the cutoff; leave fresh + unrelated new.
    old_time = time.time() - 7200  # 2h ago
    for d in (old_run, old_xmage):
        os.utime(d, (old_time, old_time))

    reaped = reap_stale_staging(max_age_s=3600)

    assert reaped == 2
    assert not old_run.exists() and not old_xmage.exists()  # orphans swept.
    assert fresh_run.exists()  # a concurrent run's fresh dir is untouched.
    assert unrelated.exists()  # non-staging entries are never touched.


def test_reap_stale_staging_never_raises_on_missing_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A missing staging root is a no-op (0 reaped), never an error — reaping must
    not be the thing that breaks a run."""
    from pipeline.sim.runner import reap_stale_staging

    monkeypatch.setattr(runner_mod, 'staging_root', lambda: tmp_path / 'does-not-exist')
    assert reap_stale_staging() == 0
