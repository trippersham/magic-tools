"""Presence/packaging guard for the committed Forge sim-AI harness jar.

Task 1.5 ports the harness (``org.makemagic.simai.SimAIMatch``) plus the
NPE-guard shadow class (``forge.game.staticability.StaticAbilityContinuous``)
into ``pipeline/sim/java/forge-simai`` and commits the built jar so the wheel and
downstream tests are deterministic without a JDK at install time. These tests
fail loudly if a build/packaging regression drops a class or the ``Main-Class``.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pipeline.sim

_JAR = Path(pipeline.sim.__file__).parent / 'java' / 'forge-simai' / 'make-magic-forge-simai.jar'

_HARNESS_CLASS = 'org/makemagic/simai/SimAIMatch.class'
_SHADOW_CLASS = 'forge/game/staticability/StaticAbilityContinuous.class'
_MAIN_CLASS = 'org.makemagic.simai.SimAIMatch'


def test_harness_jar_exists_and_nonempty() -> None:
    assert _JAR.is_file(), f'harness jar missing: {_JAR}'
    assert _JAR.stat().st_size > 0, f'harness jar is empty: {_JAR}'


def test_harness_jar_contains_both_classes() -> None:
    with zipfile.ZipFile(_JAR) as zf:
        names = set(zf.namelist())
    assert _HARNESS_CLASS in names, f'{_HARNESS_CLASS} not in jar'
    assert _SHADOW_CLASS in names, f'{_SHADOW_CLASS} not in jar'


def test_harness_jar_declares_main_class() -> None:
    with zipfile.ZipFile(_JAR) as zf:
        manifest = zf.read('META-INF/MANIFEST.MF').decode('utf-8')
    # Manifest lines can wrap at 72 bytes; our short value does not, but be
    # tolerant by collapsing any continuation-line folding just in case.
    unfolded = manifest.replace('\r\n', '\n').replace('\n ', '')
    assert f'Main-Class: {_MAIN_CLASS}' in unfolded, f'Main-Class {_MAIN_CLASS!r} not declared in manifest:\n{manifest}'
