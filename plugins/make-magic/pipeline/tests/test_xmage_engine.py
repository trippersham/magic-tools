"""Offline tests for the XMage :class:`~pipeline.sim.engines.xmage.XMageEngine`.

No JVM / no XMage reactor: exercises the deck translation, the declared
capabilities, the registry wiring, and the never-crash resolve contract. The
real headless CP7 run is validated end-to-end via the pipeline (see the task-2.3
behavioral evidence) + the telemetry drop-in in ``test_telemetry_xmage.py``.
"""

from __future__ import annotations

import pytest

from pipeline.sim.engine import EngineUnavailableError, SimEngine, get_engine
from pipeline.sim.engines.xmage import XMageEngine, _forge_dck_to_xmage_txt


def test_registered_and_is_sim_engine() -> None:
    engine = get_engine('xmage')
    assert isinstance(engine, XMageEngine)
    assert isinstance(engine, SimEngine)
    assert engine.name == 'xmage'


def test_capabilities_reflect_xmage_cp7() -> None:
    caps = XMageEngine().capabilities()
    assert caps.has_hand_visibility is True
    assert caps.has_counter_metrics is True  # CP7 casts counters — the differentiator.
    # XMage names a combat kill generically — downstream must not read a named source.
    assert caps.kill_attribution == 'combat_generic'
    assert caps.expected_nondecisive_rate < 0.1  # XMage plays to a decisive result.


def test_forge_dck_translates_to_xmage_txt() -> None:
    # A Forge .dck's [Main] lines are already 'N Cardname' — keep ONLY those, drop
    # the [metadata] and [Sideboard] sections (XMage DeckImporter reads plain .txt).
    dck = (
        '[metadata]\n'
        'Name=Test\n'
        'Deck Type=Constructed\n'
        '[Main]\n'
        '4 Lightning Bolt\n'
        '20 Mountain\n'
        '[Sideboard]\n'
        '2 Smash to Smithereens\n'
    )
    out = _forge_dck_to_xmage_txt(dck)
    assert out == '4 Lightning Bolt\n20 Mountain\n'
    assert 'Name=Test' not in out and 'Smash to Smithereens' not in out


def test_resolve_unavailable_raises_engine_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    # No MAKE_MAGIC_XMAGE_HOME -> a clean EngineUnavailableError (never a traceback),
    # re-raised from XMageUnavailableError so the CLI/doctor stay actionable.
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    with pytest.raises(EngineUnavailableError) as exc:
        XMageEngine().resolve(provision=False)
    assert 'MAKE_MAGIC_XMAGE_HOME' in str(exc.value)


def test_commander_is_rejected_constructed_only() -> None:
    # XMage engine is constructed-only for now — commander must fail clearly, not
    # silently mis-run. (No install needed: the format check precedes resolve use.)
    from pipeline.sim.engine import EngineInstall

    class _Stub:
        pass

    with pytest.raises(EngineUnavailableError, match='constructed only'):
        XMageEngine().run_matchup(
            ('A', ''),
            ('B', ''),
            n=1,
            seed=1,
            fmt='commander',
            install=EngineInstall(version='x', handle=_Stub()),
        )
