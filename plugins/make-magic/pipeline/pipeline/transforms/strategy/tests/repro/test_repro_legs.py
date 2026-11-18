"""Pytest entries for the standing reproducibility gate (``-m repro``).

Both legs are DESELECTED from the default suite (``pyproject.toml`` addopts adds
``not repro``). Run them deliberately::

    uv run --extra dev pytest -m repro -q                         # Leg B only
    MAKE_MAGIC_REPRO_ALLOW_LIVE=1 uv run --extra dev pytest -m repro -q   # + Leg A (ONE live fetch)

* **Leg B** (``@pytest.mark.repro``) stands up isolated lakes from the *committed*
  pinned fixture and needs **no** primary lake (``~/.local/share/make-magic``) — it
  runs wherever the fixture is checked out, including offline CI from the repo.
* **Leg A** (``@pytest.mark.repro`` + ``@pytest.mark.live``) performs ONE genuine
  live Scryfall ``oracle_tags.sync`` and so is additionally gated at runtime: it
  SKIPS with an explicit notice unless ``MAKE_MAGIC_REPRO_ALLOW_LIVE=1`` is set.
  The orchestrator / CI opts in explicitly.
"""

from __future__ import annotations

import os

import pytest

from pipeline.transforms.strategy.tests.repro import harness

REPRO = pytest.mark.repro
LIVE = pytest.mark.live


@REPRO
def test_leg_b_determinism(tmp_path) -> None:
    """Two fresh runs against the pinned lake produce identical SHAs + zero drift."""
    # Self-contained: the committed fixture is the only input (no primary lake).
    harness.verify_pinned_fixture()
    result = harness.leg_b(tmp_path / 'legb')
    assert result['verdict'] == 'PASS', '\n'.join(result['reasons']) or result
    # The three composites equal the committed pinned values.
    assert result['composites_runA'] == result['composites_pinned']
    assert result['composites_runB'] == result['composites_pinned']
    assert result['primary_lake_integrity'] == 'ok'


@REPRO
@LIVE
def test_leg_a_drift_impact(tmp_path) -> None:
    """Live tags into a frozen-bulk lake: card-universe empty, drift adjudicated."""
    if os.environ.get('MAKE_MAGIC_REPRO_ALLOW_LIVE') != '1':
        pytest.skip(
            'Leg A performs ONE genuine live Scryfall oracle_tags fetch; '
            'set MAKE_MAGIC_REPRO_ALLOW_LIVE=1 to opt in (deselected + skipped otherwise).'
        )
    fetch_log = os.environ.get('MAKE_MAGIC_REPRO_FETCH_LOG')
    result = harness.leg_a(
        tmp_path / 'lega',
        fetch_log=(None if not fetch_log else __import__('pathlib').Path(fetch_log)),
    )
    # INVALID (isolation breach / fail-open) is a hard failure; SUBSTANTIVE-REVIEW
    # means real drift moved an output and a human must adjudicate (also non-PASS).
    assert result['verdict'] != 'INVALID', '\n'.join(result['reasons']) or result
    assert result['fetch']['genuine'], result['fetch']
    assert result['diff']['n_card_universe'] == 0, 'isolation breach: card-universe bucket non-empty'
    assert result['primary_lake_integrity'] == 'ok'
    assert result['verdict'] == 'PASS', 'substantive drift needs adjudication: ' + '\n'.join(result['reasons'])
