"""Layer-1 bytecode denylist scan for driver classes (no-terminal-API enforcement).

A make-magic quad driver may only enqueue LEGAL game actions. It must NEVER call a
game/player terminal or state-fabrication API to ASSERT a win it never played (the
``MACRO_FIRE_REAL → gameOver=true`` on turn 1 pathology). :mod:`pipeline.sim.driver_lint`
parses the constant pool of every compiled ``.class`` in a driver's ``classes_dir`` (pure
Python — no ``javap``) and flags forbidden ``Methodref`` owners+methods:

  * FAIL — terminal calls: ``mage.players.Player.{lost,won,leave,quit,setLosses,setWins}``
    and ``mage.game.Game.{end,setWinner}``. A driver referencing any of these is rejected.
  * WARN — zone-fabrication (``moveCards*`` on Game/Player) and ``concede`` on the driver's
    own seat: rules-legal / part of the sanctioned bounded-resolution pattern, but recorded.

The fixtures are tiny ``.class`` files compiled against stub ``mage.*`` types, so the owner
names in the constant pool are the real forbidden owners without needing the XMage jar.
"""

from __future__ import annotations

from pathlib import Path

from pipeline.sim import driver_lint

_FIX = Path(__file__).parent / 'fixtures' / 'driver_lint'


def _class_bytes(variant: str, name: str) -> bytes:
    return (_FIX / variant / 'org' / 'makemagic' / 'driver' / f'{name}.class').read_bytes()


def test_scan_class_bytes_catches_terminal_calls() -> None:
    """A class calling ``Player.lost`` / ``Game.setWinner`` yields FAIL findings naming them."""
    scan = driver_lint.scan_class_bytes(_class_bytes('bad', 'BadDriver'))
    refs = {(owner, method) for owner, method in scan.method_refs}
    assert ('mage/players/Player', 'lost') in refs
    assert ('mage/game/Game', 'setWinner') in refs

    findings = driver_lint.lint_class_bytes(_class_bytes('bad', 'BadDriver'), name='BadDriver')
    fail_msgs = [f.detail for f in findings if f.severity == 'FAIL']
    assert any('Player.lost' in m for m in fail_msgs), fail_msgs
    assert any('Game.setWinner' in m for m in fail_msgs), fail_msgs


def test_scan_clean_class_has_no_fail() -> None:
    """A class touching only benign reads (plus a WARN-level moveCards) raises no FAIL."""
    findings = driver_lint.lint_class_bytes(_class_bytes('clean', 'CleanDriver'), name='CleanDriver')
    assert [f for f in findings if f.severity == 'FAIL'] == []
    # moveCards is the sanctioned bounded-resolution move → WARN, not FAIL.
    assert any(f.severity == 'WARN' and 'moveCards' in f.detail for f in findings), findings


def test_concede_is_warn_not_fail() -> None:
    """``concede`` on the own seat is rules-legal but delta-biasing → WARN, never FAIL."""
    findings = driver_lint.lint_class_bytes(_class_bytes('concede', 'ConcedeDriver'), name='ConcedeDriver')
    assert [f for f in findings if f.severity == 'FAIL'] == []
    assert any(f.severity == 'WARN' and 'concede' in f.detail for f in findings), findings


def test_lint_driver_dir_bad_fails(tmp_path: Path) -> None:
    """Scanning a classes_dir containing the bad fixture returns a failing LintResult."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / 'BadDriver.class').write_bytes(_class_bytes('bad', 'BadDriver'))
    result = driver_lint.lint_driver_classes(tmp_path)
    assert not result.ok
    assert result.fail_findings
    assert 'Player.lost' in result.summary or 'Game.setWinner' in result.summary


def test_lint_driver_dir_clean_ok(tmp_path: Path) -> None:
    """A clean classes_dir returns ok=True with the WARN recorded but not blocking."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / 'CleanDriver.class').write_bytes(_class_bytes('clean', 'CleanDriver'))
    result = driver_lint.lint_driver_classes(tmp_path)
    assert result.ok
    assert result.warn_findings  # moveCards recorded as WARN
