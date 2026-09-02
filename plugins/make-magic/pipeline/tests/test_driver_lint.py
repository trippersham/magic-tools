"""Layer-1 bytecode denylist scan for driver classes (no-terminal-API enforcement).

A make-magic quad driver may only enqueue LEGAL game actions. It must NEVER call a
game/player terminal or state-fabrication API to ASSERT a win it never played (the
``MACRO_FIRE_REAL → gameOver=true`` on turn 1 pathology). :mod:`pipeline.sim.driver_lint`
parses the constant pool of every compiled ``.class`` in a driver's ``classes_dir`` (pure
Python — no ``javap``) and flags forbidden ``Methodref`` owners+methods:

  * FAIL — terminal calls: ``mage.players.Player.{lost,won,leave,quit,setLosses,setWins}``
    and ``mage.game.Game.{end,setWinner}``; ``concede`` (any owner); and zone-fabrication
    (``moveCard*`` on any ``mage/`` owner — ``moveCards`` / ``moveCardToExile*`` / …). A driver
    referencing any of these is rejected: it mutates a zone without paying costs or passing
    priority (the ``moveCards``-exile-library + drop-Thassa's-Oracle fabrication).
  * A driver acts ONLY through casts / activations / choices; every terminal state and every
    zone change comes from the rules engine, never from a direct owner-API call.

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


def test_scan_clean_class_has_no_findings() -> None:
    """A class touching only benign reads (``getName`` / ``checkIfGameIsOver``) raises nothing."""
    findings = driver_lint.lint_class_bytes(_class_bytes('clean', 'CleanDriver'), name='CleanDriver')
    assert findings == (), findings


def test_zone_move_is_fail() -> None:
    """``moveCards`` / ``moveCardTo*`` on a ``mage/`` owner is now a FAIL (zone-fabrication): a
    driver moving cards between zones without casting/paying/priority manufactures a terminal
    (the exile-library + drop-Thassa's-Oracle deck-out). It is NOT a WARN — the sanctioned line
    is casts + engine resolution, never a direct ``moveCards``."""
    findings = driver_lint.lint_class_bytes(_class_bytes('zonemove', 'ZoneMoveDriver'), name='ZoneMoveDriver')
    fails = [f for f in findings if f.severity == 'FAIL']
    assert any('moveCards' in f.detail for f in fails), findings
    assert any('moveCardToExile' in f.detail for f in fails), findings
    # No lingering WARN severity for zone moves — the split for these is gone.
    assert not any(f.severity == 'WARN' and 'moveCard' in f.detail for f in findings), findings


def test_concede_is_fail() -> None:
    """``concede`` is a FAIL — a driver may not concede at all (forcing the OPPONENT to concede
    fabricates a credited decisive win; self-concession is only a theoretical nicety). Safety wins."""
    findings = driver_lint.lint_class_bytes(_class_bytes('concede', 'ConcedeDriver'), name='ConcedeDriver')
    fails = [f for f in findings if f.severity == 'FAIL']
    assert any('concede' in f.detail for f in fails), findings
    assert not any(f.severity == 'WARN' and 'concede' in f.detail for f in findings), findings


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
    """A clean classes_dir (benign reads only) returns ok=True with no findings at all."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / 'CleanDriver.class').write_bytes(_class_bytes('clean', 'CleanDriver'))
    result = driver_lint.lint_driver_classes(tmp_path)
    assert result.ok
    assert result.findings == ()


def test_lint_driver_dir_zonemove_fails(tmp_path: Path) -> None:
    """A classes_dir whose driver zone-fabricates via ``moveCards`` is REJECTED (ok=False)."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / 'ZoneMoveDriver.class').write_bytes(_class_bytes('zonemove', 'ZoneMoveDriver'))
    result = driver_lint.lint_driver_classes(tmp_path)
    assert not result.ok
    assert result.fail_findings
    assert 'moveCards' in result.summary


# --- Sol HIGH 2: subclass owners, reflection, fail-closed ------------------ #


def test_subclass_owner_terminal_call_fails() -> None:
    """A terminal method called on a CONCRETE mage subclass whose name does NOT contain the
    ``Game``/``Player`` substring (e.g. ``mage/game/CommanderFreeForAll.end``) still FAILs — owner
    substring denylisting alone missed these; the ``mage/`` namespace rule catches them."""
    findings = driver_lint.lint_class_bytes(_class_bytes('subclass', 'SubclassDriver'), name='SubclassDriver')
    fails = [f.detail for f in findings if f.severity == 'FAIL']
    assert any('CommanderFreeForAll.end' in m for m in fails), fails
    assert any('HumanControlled.lost' in m for m in fails), fails


def test_reflection_escape_hatch_fails() -> None:
    """Any driver reaching for reflective invocation (``Method.invoke`` /
    ``Class.getDeclaredMethod`` / method handles) FAILs — drivers have no legitimate reflection."""
    findings = driver_lint.lint_class_bytes(_class_bytes('reflect', 'ReflectDriver'), name='ReflectDriver')
    fails = [f.detail for f in findings if f.severity == 'FAIL']
    assert any('invoke' in m or 'getDeclaredMethod' in m for m in fails), fails


def test_compiler_bootstrap_refs_are_not_reflection() -> None:
    """Lambda / string-concat invokedynamic bootstraps (``LambdaMetafactory.metafactory``,
    ``StringConcatFactory.makeConcatWithConstants``) are compiler-generated and must NOT be flagged
    as reflection — the ``lambda`` fixture uses both and stays ok."""
    findings = driver_lint.lint_class_bytes(_class_bytes('lambda', 'LambdaDriver'), name='LambdaDriver')
    assert [f for f in findings if f.severity == 'FAIL'] == [], findings


def test_truncated_constant_pool_fails_closed(tmp_path: Path) -> None:
    """A malformed / truncated ``.class`` cannot be proven clean → it FAILs (fail-closed), never a
    silent skip that would let an unscannable driver load."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    (dest / 'TruncatedDriver.class').write_bytes(_class_bytes('truncated', 'TruncatedDriver'))
    result = driver_lint.lint_driver_classes(tmp_path)
    assert not result.ok
    assert result.fail_findings
    assert any('scan' in f.detail.lower() for f in result.fail_findings), result.fail_findings


def test_unreadable_class_fails_closed(tmp_path: Path) -> None:
    """An unreadable ``.class`` file is a scan error → FAIL, not a skip."""
    dest = tmp_path / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True)
    bad = dest / 'NoRead.class'
    bad.write_bytes(_class_bytes('clean', 'CleanDriver'))
    bad.chmod(0o000)
    try:
        result = driver_lint.lint_driver_classes(tmp_path)
    finally:
        bad.chmod(0o644)
    assert not result.ok, 'an unreadable class must fail closed, not pass'


_REFERENCE_DRIVERS = sorted(
    (Path(__file__).parents[1] / 'pipeline' / 'sim' / 'reference_drivers').glob('*.java')
)


def test_checked_in_reference_drivers_ship_no_forbidden_api() -> None:
    """Every checked-in reference/example driver must ship a CLEAN example — no direct terminal,
    concede, reflection, or zone-move call. The repo cannot ship a forbidden pattern that an
    author would copy (the old Jeleva reference used ``me.moveCards(...)`` + ``o.lost(game)``).
    A textual guard (the sources are not compiled in CI) over the call-site tokens the lint FAILs."""
    assert _REFERENCE_DRIVERS, 'expected at least one reference driver to guard'
    forbidden = ('.moveCards(', '.moveCardTo', '.lost(', '.won(', '.setWinner(', '.concede(', '.end(')
    offenders: dict[str, list[str]] = {}
    for src in _REFERENCE_DRIVERS:
        text = src.read_text(encoding='utf-8')
        hits = [tok for tok in forbidden if tok in text]
        if hits:
            offenders[src.name] = hits
    assert not offenders, f'reference driver(s) ship a forbidden call: {offenders}'


def _write_one(base: Path, variant: str, name: str) -> Path:
    dest = base / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True, exist_ok=True)
    (dest / f'{name}.class').write_bytes(_class_bytes(variant, name))
    return base
