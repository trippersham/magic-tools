"""Byte-determinism: identical output across repeated runs and across hash seeds.

The engine iterates sets internally; a non-determinism bug surfaces as a differing
JSON serialization. We assert the full record (``sort_keys=True``) is byte-stable
when resolved twice in-process, and — the stronger check — identical across a
subprocess run under a different ``PYTHONHASHSEED``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from pipeline.transforms.strategy import resolve
from pipeline.transforms.strategy.tests.conftest import GOLDEN_SLUGS

FIX = Path(__file__).resolve().parent / 'fixtures'


def _blob(slug: str) -> str:
    combos = json.loads((FIX / 'combos.json').read_text())
    deck = json.loads((FIX / 'decks' / f'{slug}.json').read_text())
    return json.dumps(resolve(deck, combos), sort_keys=True, default=list)


@pytest.mark.parametrize('slug', sorted(GOLDEN_SLUGS))
def test_same_output_twice_in_process(slug):
    assert _blob(slug) == _blob(slug)


_SUBPROC = (
    'import json,sys;'
    'from pathlib import Path;'
    'from pipeline.transforms.strategy import resolve;'
    'fix=Path(sys.argv[1]);slug=sys.argv[2];'
    "combos=json.loads((fix/'combos.json').read_text());"
    "deck=json.loads((fix/'decks'/(slug+'.json')).read_text());"
    'print(json.dumps(resolve(deck,combos),sort_keys=True,default=list))'
)


@pytest.mark.parametrize('slug', sorted(GOLDEN_SLUGS))
def test_stable_across_hashseeds(slug):
    def run(seed: str) -> str:
        env = {**os.environ, 'PYTHONHASHSEED': seed}
        out = subprocess.run(
            [sys.executable, '-c', _SUBPROC, str(FIX), slug],
            capture_output=True, text=True, env=env, check=True,
        )
        return out.stdout

    a = run('0')
    b = run('1')
    c = run('12345')
    assert a == b == c
    assert a.strip()  # non-empty
