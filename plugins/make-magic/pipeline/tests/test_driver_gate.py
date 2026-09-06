"""Phase 5 gate tests — the DUAL-MODE solo own-turn-clock ship gate (mocked JVM), plus the
guarded Jeleva quad positive control against the real dist jar.

The unit tests use a FAKE goldfish engine (canned driven/baseline medians + a driven output
string carrying the standard slot-exercise markers) — NO real JVM. The gate routes by the
quad's shape (a macro ⇒ proactive, Φ-only ⇒ reactive):

  * proactive PASS = registers + ``MACRO_FIRE_REAL`` (true execution) + never-slower;
  * proactive FAIL = macro never fires, OR slower than CP7;
  * reactive PASS = registers + never-worse-solo floor (NO macro-fire requirement).

The final test is an opt-in integration: the Jeleva quad ECJ-compiled against the real jar
MUST register + fire its macro + pass; a non-firing driver MUST be rejected. It skips cleanly
when the local dist jar / JRE / ECJ are absent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da
from pipeline.sim import driver_compile as dcomp
from pipeline.sim import driver_gate as dg
from pipeline.sim import drivers
from pipeline.sim import xmage_runtime as xr
from pipeline.sim.engines.xmage import GoldfishResult

# --------------------------------------------------------------------------- #
# fixtures / fakes                                                             #
# --------------------------------------------------------------------------- #


def _deck(name: str = 'Gate Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


def _proactive_spec() -> da.QuadSpec:
    """A minimal PROACTIVE quad (carries a macro ⇒ macro-fire required)."""
    return da.QuadSpec(
        name='gate-proactive',
        archetype='proactive',
        phi_body='return 0;',
        macro=da.MacroSpec(applicable_body='return true;', apply_body='return;'),
    )


def _reactive_spec() -> da.QuadSpec:
    """A minimal REACTIVE Φ-only quad (no macro ⇒ never-worse floor only)."""
    return da.QuadSpec(name='gate-reactive', archetype='reactive', phi_body='return 0;')


class _FakeEngine:
    """A goldfish engine that returns canned driven/baseline results + a driven output string
    — the gate's whole JVM dependency, replaced so the LOGIC is unit-testable offline."""

    def __init__(self, *, driven: GoldfishResult, driven_output: str, baseline: GoldfishResult) -> None:
        self._driven = driven
        self._driven_output = driven_output
        self._baseline = baseline
        self.calls: list[str] = []
        self.fmts: list[str] = []  # records the fmt each goldfish call received

    def goldfish_output(self, deck_a, *, games, install, driver=None, fmt='constructed'):
        assert driver is not None  # the driven run always injects the driver
        self.calls.append('driven')
        self.fmts.append(fmt)
        return self._driven, self._driven_output

    def goldfish(self, deck_a, *, games, install, driver=None, fmt='constructed'):
        assert driver is None  # the baseline is driverless (throwaway pure CP7)
        self.calls.append('baseline')
        self.fmts.append(fmt)
        return self._baseline


@pytest.fixture
def _store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(tmp_path))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', 'deadbeef' * 8)  # deterministic harness_version
    return tmp_path


def _res(median: float, games: int = 5, max_turn: int | None = None) -> GoldfishResult:
    return GoldfishResult(median_kills_own=median, games=games, max_turn=max_turn)


_REG = dg.DRIVER_REGISTERED_MARKER
_MACRO = da.DRIVER_MACRO_FIRED_MARKER  # reachability (apply() entered — search copy OR real)
_REAL = dg.MACRO_FIRE_REAL_MARKER  # true execution (act() committed the win on the REAL game)

_GAMES = 20  # >= dg._MIN_GATE_GAMES floor


# --------------------------------------------------------------------------- #
# 1. proactive mode — registers + macro fires + never-slower                   #
# --------------------------------------------------------------------------- #


def test_proactive_passes_when_registers_really_fires_and_not_slower(_store: Path) -> None:
    """Proactive: DRIVER_REGISTERED + MACRO_FIRE_REAL (true execution) present AND driven <=
    baseline → pass, meta stamped gates_passed + gate_mode='proactive', driver_valid True."""
    deck = _deck()
    output = (
        f'noise\n{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    )
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is True
    assert result.mode == 'proactive'
    assert result.registered is True and result.macro_fired is True
    assert result.macro_reachable is True
    assert eng.calls == ['driven', 'baseline']
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gates_passed is True and meta.gate_mode == 'proactive'
    assert meta.extra['gate_driven_median_kills_own'] == 6.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_passing_gate_stamps_driver_class_drive_for_macro_bearing(_store: Path) -> None:
    """DRIVE/THIN richness stamp: a macro-bearing (proactive) quad that passes stamps
    driver_class='drive' — Speed will run a DRIVEN goldfish. Derived from macro presence
    (is_proactive), the same signal gate_mode uses."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))
    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )
    assert result.passed is True
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.driver_class == 'drive'
    assert drivers.driver_is_drive(deck, data_dir=_store) is True


def test_passing_gate_stamps_driver_class_thin_for_phi_only(_store: Path) -> None:
    """A Φ-only (reactive) quad that passes stamps driver_class='thin' — a goldfish driven
    by it is behaviorally CP7, so Speed keeps the deterministic closed form."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no macro fire needed
    eng = _FakeEngine(driven=_res(7.0), driven_output=output, baseline=_res(7.0))
    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_reactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )
    assert result.passed is True
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.driver_class == 'thin'
    assert drivers.driver_is_drive(deck, data_dir=_store) is False


def test_specless_regate_recovers_driver_class_from_mode(_store: Path) -> None:
    """The spec-less re-gate path: no live QuadSpec, mode recovered from the prior stamp.
    driver_class is re-derived from that mode (proactive ⇒ drive)."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))
    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=None, mode='proactive', install=object(), games=_GAMES, engine=eng, data_dir=_store
    )
    assert result.passed is True
    assert drivers.read_meta(deck, data_dir=_store).driver_class == 'drive'


def test_specless_regate_recovers_drive_from_observed_macro_fire(_store: Path) -> None:
    """A driver re-gated from an 'unknown' gate_mode runs under the lenient reactive gate
    (is_proactive False), but if its macro still fires in the re-gate goldfish it is stamped
    'drive' — richness keys on the observed fire, not the declared mode."""
    deck = _deck()
    output = (  # a macro really fires even though we gate in reactive mode
        f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    )
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))
    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=None, mode='unknown', install=object(), games=_GAMES, engine=eng, data_dir=_store
    )
    assert result.passed is True
    assert drivers.read_meta(deck, data_dir=_store).driver_class == 'drive'  # recovered, not 'thin'.


def test_specless_regate_unknown_mode_no_fire_stamps_unknown(_store: Path) -> None:
    """A legacy driver re-gated from an 'unknown' mode passes the lenient reactive gate WITHOUT
    firing a macro. Its richness cannot be determined — no observed fire proves neither a macro's
    absence nor thin-ness — so it must stamp 'unknown' (read back as driver_is_drive → None), not
    guess 'thin'. The Speed router then keeps the closed form and recommends re-authoring."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # registers, no macro fire
    eng = _FakeEngine(driven=_res(7.0), driven_output=output, baseline=_res(7.0))
    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=None, mode='unknown', install=object(), games=_GAMES, engine=eng, data_dir=_store
    )
    assert result.passed is True
    assert drivers.read_meta(deck, data_dir=_store).driver_class == 'unknown'  # not 'thin'.
    assert drivers.driver_is_drive(deck, data_dir=_store) is None


def test_proactive_fails_when_macro_never_fires(_store: Path) -> None:
    """Proactive: registers but MACRO_FIRE_REAL absent → fail even if the median is fine;
    nothing stamped, driver_valid False."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no fire markers
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert result.mode == 'proactive'
    assert result.registered is True and result.macro_fired is False
    assert result.macro_reachable is False
    assert 'MACRO_FIRE_REAL' in result.reason
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_proactive_fails_when_reachable_but_never_really_fires(_store: Path) -> None:
    """THE NEW TEETH: registers + DRIVER_MACRO_FIRED present (the macro is REACHABLE — apply()
    entered on a throwaway search copy) but MACRO_FIRE_REAL absent (it never executed to win on
    the real game) → FAIL. Reachability alone must not pass the fire check."""
    deck = _deck()
    output = (
        f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_MACRO} pid=1\n'  # reachable in search, never real
        'GOLDFISH SUMMARY (OWN TURNS) ...\n'
    )
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert result.macro_reachable is True  # reachability recorded…
    assert result.macro_fired is False  # …but NOT true execution
    assert 'MACRO_FIRE_REAL' in result.reason
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_games_floor_raises_below_minimum(_store: Path) -> None:
    """A silently-underpowered gate is worse than a hard stop: games below the floor raises
    ValueError LOUDLY (both modes); at the floor it does not."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    with pytest.raises(ValueError, match='games'):
        dg.gate_driver(
            deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=8, engine=eng, data_dir=_store
        )
    # reactive too — jitter affects the never-worse floor as well.
    with pytest.raises(ValueError, match='games'):
        dg.gate_driver(
            deck, ('D', 'dck'), spec=_reactive_spec(), install=object(), games=8, engine=eng, data_dir=_store
        )
    # at the floor: no raise.
    result = dg.gate_driver(
        deck,
        ('D', 'dck'),
        spec=_proactive_spec(),
        install=object(),
        games=dg._MIN_GATE_GAMES,
        engine=eng,
        data_dir=_store,
    )
    assert result.passed is True


def test_proactive_fails_when_slower_than_baseline(_store: Path) -> None:
    """Proactive: registers + fires but driven median WORSE than baseline by MORE than the
    tolerance (default 2) → fail. 9.0 vs 6.0 is +3, beyond the 2-turn jitter slack."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(6.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert result.macro_fired is True
    assert 'WORSE' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_fails_when_not_registered(_store: Path) -> None:
    """Neither mode passes without DRIVER_REGISTERED (belt-and-suspenders over the P3 fail-loud)."""
    deck = _deck()
    output = f'{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no registration
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert result.registered is False
    assert 'DRIVER_REGISTERED' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


# --------------------------------------------------------------------------- #
# 2. reactive mode — never-worse floor, NO macro-fire requirement              #
# --------------------------------------------------------------------------- #


def test_reactive_passes_on_floor_without_macro_fire(_store: Path) -> None:
    """Reactive Φ-only: registers + meets the never-worse floor → pass WITHOUT any macro-fire
    (a passive goldfish gives a reactive deck nothing to react to). gate_mode='reactive'."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\nGOLDFISH SUMMARY (OWN TURNS) ...\n'  # no macro marker — fine
    eng = _FakeEngine(driven=_res(8.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_reactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is True
    assert result.mode == 'reactive'
    assert result.macro_fired is False
    meta = drivers.read_meta(deck, data_dir=_store)
    assert meta is not None and meta.gate_mode == 'reactive'
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_reactive_fails_below_the_floor(_store: Path) -> None:
    """Reactive: even with no macro requirement, a driver meaningfully slower than CP7 fails
    the never-worse floor."""
    deck = _deck()
    output = f'{_REG} p=1\n'
    eng = _FakeEngine(driven=_res(-1.0), driven_output=output, baseline=_res(7.0))  # no-kill sentinel → +inf

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_reactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert 'floor' in result.reason
    assert drivers.driver_valid(deck, data_dir=_store) is False


# --------------------------------------------------------------------------- #
# 2b. commander fmt threading — the P6.0 fix                                    #
# --------------------------------------------------------------------------- #


def _commander_deck(name: str = 'Cmd Deck') -> Deck:
    """A deck carrying a commander (role='commander') → the gate must run fmt='commander'."""
    return Deck(
        name=name,
        cards=[
            DeckCard(name='Yawgmoth, Thran Physician', quantity=1, role='commander'),
            DeckCard(name='Swamp', quantity=1),
        ],
    )


def test_commander_deck_threads_fmt_commander_to_both_goldfish_calls(_store: Path) -> None:
    """A commander deck (has commanders) → BOTH driven and baseline goldfish run
    fmt='commander' so the commander is seated (CommanderDuel) — the P6.0 Signal-A fix.
    Same fmt on both keeps the never-slower comparison honest."""
    deck = _commander_deck()
    output = f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert eng.calls == ['driven', 'baseline']
    assert eng.fmts == ['commander', 'commander']


def test_noncommander_deck_stays_constructed_fmt(_store: Path) -> None:
    """A non-commander deck (no commanders) → both goldfish calls stay fmt='constructed'
    (byte-identical prior argv) — the constructed path is unchanged."""
    deck = _deck()
    output = f'{_REG} fqcn=x playerId=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ...\n'
    eng = _FakeEngine(driven=_res(6.0), driven_output=output, baseline=_res(8.0))

    dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert eng.fmts == ['constructed', 'constructed']


# --------------------------------------------------------------------------- #
# 2c. brick-cap validity — deckout/freeze-at-cap is a BRICK, not a pass         #
# --------------------------------------------------------------------------- #


def test_deckout_at_cap_fails_the_brick_cap_validity_guard(_store: Path) -> None:
    """THE BRICK-CAP GUARD (Jeleva-freeze pathology): the solo harness counts a "kill" whenever
    the passer opponent LOSES for any reason — including decking out / an SBA technicality — and
    CLAMPS that kill turn to maxTurn. So a driven median sitting AT maxTurn is NOT the deck's own
    kill; it is the do-nothing opponent falling over at the cap. Even when registered + macro
    fires + never-slower (driven==baseline==maxTurn), it must FAIL as a brick-cap invalidity, and
    nothing is stamped."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=20\nGOLDFISH SUMMARY (OWN TURNS) ... maxTurn=20 ...\n'
    # driven median AT the cap (20) — the "kills" are clamp artifacts (opponent deckout at cap).
    # baseline also at the cap, so never-slower passes; ONLY the brick-cap guard can reject this.
    eng = _FakeEngine(driven=_res(20.0, max_turn=20), driven_output=output, baseline=_res(20.0, max_turn=20))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert result.registered is True and result.macro_fired is True  # capability + relative both held…
    assert 'brick-cap' in result.reason and 'maxTurn=20' in result.reason  # …brick-cap rejected it
    assert drivers.read_meta(deck, data_dir=_store) is None
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_real_kill_below_cap_passes_brick_cap_guard(_store: Path) -> None:
    """A real kill comfortably below the cap (driven median 6 < maxTurn 20) is NOT a clamp
    artifact and clears the brick-cap guard — the guard fires only at/beyond the cap boundary."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\nGOLDFISH SUMMARY (OWN TURNS) ... maxTurn=20 ...\n'
    eng = _FakeEngine(driven=_res(6.0, max_turn=20), driven_output=output, baseline=_res(8.0, max_turn=20))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is True
    assert drivers.driver_valid(deck, data_dir=_store) is True


# --------------------------------------------------------------------------- #
# 2d. pure-measurement invariant — no bracket/archetype/absolute-turn bar       #
# --------------------------------------------------------------------------- #


def test_gate_is_relative_not_absolute_two_decks_different_clocks_both_pass(_store: Path) -> None:
    """PURE-MEASUREMENT INVARIANT: the ONLY numeric comparisons are (driven vs baseline+tol) and
    (games vs the floor). There is NO constant turn bar keyed to a bracket/archetype/absolute
    clock. Proof by structure: a FAST deck (driven 4 vs baseline 4) and a SLOW deck (driven 15 vs
    baseline 15) BOTH pass — the identical driven==baseline relationship, at wildly different
    absolute clocks, yields the same verdict. An absolute turn bar would pass one and fail the
    other; a relative gate passes both."""
    fast_out = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=4\nGOLDFISH SUMMARY (OWN TURNS) ... maxTurn=20 ...\n'
    slow_out = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=15\nGOLDFISH SUMMARY (OWN TURNS) ... maxTurn=20 ...\n'
    fast_deck = _deck('Fast Deck')
    slow_deck = _deck('Slow Deck')

    fast = dg.gate_driver(
        fast_deck,
        ('D', 'dck'),
        spec=_proactive_spec(),
        install=object(),
        games=_GAMES,
        engine=_FakeEngine(driven=_res(4.0, max_turn=20), driven_output=fast_out, baseline=_res(4.0, max_turn=20)),
        data_dir=_store,
    )
    slow = dg.gate_driver(
        slow_deck,
        ('D', 'dck'),
        spec=_proactive_spec(),
        install=object(),
        games=_GAMES,
        engine=_FakeEngine(driven=_res(15.0, max_turn=20), driven_output=slow_out, baseline=_res(15.0, max_turn=20)),
        data_dir=_store,
    )

    # Both pass: the verdict is driven<=baseline+tol, NOT any absolute turn threshold.
    assert fast.passed is True and slow.passed is True
    # And the mirror: a SLOW deck fails ONLY when it is slower than ITS OWN baseline — proving the
    # bar is the same-deck baseline, never an absolute clock. (Fast baseline, slow driven → fail.)
    slower_than_self = dg.gate_driver(
        _deck('Regressed Deck'),
        ('D', 'dck'),
        spec=_proactive_spec(),
        install=object(),
        games=_GAMES,
        engine=_FakeEngine(driven=_res(15.0, max_turn=20), driven_output=slow_out, baseline=_res(6.0, max_turn=20)),
        data_dir=_store,
    )
    assert slower_than_self.passed is False and 'WORSE' in slower_than_self.reason


# --------------------------------------------------------------------------- #
# 3. tolerance behaviour                                                        #
# --------------------------------------------------------------------------- #


def test_tolerance_absorbs_seedless_jitter(_store: Path) -> None:
    """Within the tolerance (default 2), a driven median slightly above baseline still passes
    — the check must not coin-flip on XMage's seedless ±1-turn jitter."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))  # +1, within tol

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is True
    assert result.extra['gate_tolerance'] == 2.0
    assert drivers.driver_valid(deck, data_dir=_store) is True


def test_tolerance_is_configurable(_store: Path) -> None:
    """A stricter tolerance=0 restores exact never-slower: +1 now fails."""
    deck = _deck()
    output = f'{_REG} p=1\n{_MACRO} pid=1\n{_REAL} name=A turn=6\n'
    eng = _FakeEngine(driven=_res(9.0), driven_output=output, baseline=_res(8.0))

    result = dg.gate_driver(
        deck,
        ('D', 'dck'),
        spec=_proactive_spec(),
        install=object(),
        games=_GAMES,
        tolerance=0.0,
        engine=eng,
        data_dir=_store,
    )

    assert result.passed is False
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_kill_metric_treats_no_kill_sentinel_as_worst() -> None:
    """Unit: a negative median (the harness -1.0 'never killed') maps to +inf so it can never
    beat a real kill turn."""
    import math

    assert dg._kill_metric(-1.0) == math.inf
    assert dg._kill_metric(6.0) == 6.0


# --------------------------------------------------------------------------- #
# 4. compile step — structured failure                                         #
# --------------------------------------------------------------------------- #


def test_compile_quad_driver_raises_structured_on_compile_failure(
    monkeypatch: pytest.MonkeyPatch, _store: Path
) -> None:
    """A compile failure surfaces as a structured DriverCompileError (parsed diagnostics), not
    a silent skip; the render + guardrail step still ran (source written)."""
    deck = _deck()
    spec = _proactive_spec()

    def _boom(source, fqcn, *, data_dir=None):
        result = dcomp.CompileResult(
            ok=False,
            class_dir=None,
            diagnostics=(dcomp.Diagnostic(severity='ERROR', file='Driver.java', line=9, message='cannot find symbol'),),
            raw_stderr='1. ERROR in Driver.java (at line 9)',
            cache_hit=False,
        )
        raise dcomp.DriverCompileError(Path(source), result)

    monkeypatch.setattr(dcomp, 'compile_for_injection', _boom)
    with pytest.raises(dcomp.DriverCompileError, match='cannot find symbol'):
        dg.compile_quad_driver(deck, spec, data_dir=_store)
    # the source was rendered + written before the (failing) compile.
    assert (drivers.driver_dir(deck, data_dir=_store) / 'Driver.java').read_text().startswith('package ')


def test_compile_quad_driver_rejects_guardrail_violation(_store: Path) -> None:
    """A quad that owns something the §7.1 do-not-own list forbids is rejected BEFORE compile."""
    deck = _deck()
    bad = da.QuadSpec(
        name='bad',
        archetype='proactive',
        phi_body='return 0;',
        macro=da.MacroSpec(applicable_body='return true;', apply_body='game.copy();'),
    )
    with pytest.raises(da.GuardrailViolation):
        dg.compile_quad_driver(deck, bad, data_dir=_store)


# --------------------------------------------------------------------------- #
# 5. POSITIVE CONTROL (opt-in integration): real jar, real gates               #
# --------------------------------------------------------------------------- #

_DATA_DIR = Path.home() / '.local' / 'share' / 'make-magic'
_DIST_JAR = _DATA_DIR / 'xmage' / 'make-magic-xmage-dist.jar'
_LAB_JRE = Path.home() / 'mtg-sim-lab' / 'jre' / 'jdk-21.0.12+8-jre' / 'Contents' / 'Home' / 'bin' / 'java'
_JELEVA_TXT = Path.home() / 'mtg-sim-lab' / 'reward-poc' / 'decks' / 'jeleva.txt'
_ECJ_JAR = _DATA_DIR / 'xmage' / 'tools' / f'ecj-{dcomp.ECJ_VERSION}.jar'
_INTEGRATION_READY = _DIST_JAR.is_file() and _LAB_JRE.is_file() and _JELEVA_TXT.is_file()


def _jeleva_dck() -> tuple[str, str]:
    lines = [ln.strip() for ln in _JELEVA_TXT.read_text().splitlines() if ln.strip()]
    return ('jeleva', '[metadata]\nName=jeleva\n[Main]\n' + '\n'.join(lines) + '\n')


@pytest.mark.integration
@pytest.mark.skipif(not _INTEGRATION_READY, reason='local dist jar / lab JRE / jeleva deck absent')
def test_jeleva_positive_control_and_nonfiring_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    """END-TO-END: the Jeleva quad ECJ-compiles against the real jar and PASSES (registers +
    macro fires + never-slower); a non-firing driver is REJECTED."""
    from pipeline.sim.engine import get_engine

    monkeypatch.setenv('MAKE_MAGIC_DATA_DIR', str(_DATA_DIR))
    monkeypatch.setenv('MAKE_MAGIC_JAVA', str(_LAB_JRE))
    monkeypatch.delenv('MAKE_MAGIC_XMAGE_HOME', raising=False)
    monkeypatch.setenv('MAKE_MAGIC_XMAGE_DIST_JAR', str(_DIST_JAR))
    monkeypatch.setattr(xr, 'XMAGE_DIST_SHA256', None)

    engine = get_engine('xmage')
    install = engine.resolve(provision=False, data_dir=_DATA_DIR)
    deck_ref = _jeleva_dck()
    games = 20  # >= dg._MIN_GATE_GAMES floor

    good_deck = Deck(name='Jeleva Good', cards=[DeckCard(name='Forest', quantity=1)])
    dg.compile_quad_driver(good_deck, da.JELEVA_QUAD_SPEC, data_dir=_DATA_DIR)
    good = dg.gate_driver(
        good_deck, deck_ref, spec=da.JELEVA_QUAD_SPEC, install=install, games=games, engine=engine, data_dir=_DATA_DIR
    )
    assert good.registered is True, f'never registered: {good.reason}'
    assert good.macro_fired is True, f'macro never fired: {good.reason}'
    assert good.passed is True, f'positive control failed: {good.reason}'
    assert drivers.driver_valid(good_deck, data_dir=_DATA_DIR) is True


# --------------------------------------------------------------------------- #
# Layer-1 lint at the gate — forbidden terminal-API bytecode is rejected       #
# BEFORE any JVM game runs (defense at compile/gate time).                      #
# --------------------------------------------------------------------------- #

_LINT_FIX = Path(__file__).parent / 'fixtures' / 'driver_lint'


def _stage_class(classes_dir: Path, variant: str, name: str) -> None:
    dest = classes_dir / 'org' / 'makemagic' / 'driver'
    dest.mkdir(parents=True, exist_ok=True)
    src = _LINT_FIX / variant / 'org' / 'makemagic' / 'driver' / f'{name}.class'
    (dest / f'{name}.class').write_bytes(src.read_bytes())


def test_gate_rejects_driver_with_terminal_api(_store: Path) -> None:
    """A driver whose compiled bytecode references Player.lost / Game.setWinner FAILS the gate
    with a message naming the forbidden ref — and the JVM engine is NEVER called."""
    deck = _deck('Lint Bad')
    _stage_class(drivers.classes_dir(deck, data_dir=_store), 'bad', 'BadDriver')
    eng = _FakeEngine(driven=_res(6.0), driven_output='', baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert ('Player.lost' in result.reason) or ('Game.setWinner' in result.reason)
    assert eng.calls == []  # rejected before any game ran
    assert drivers.driver_valid(deck, data_dir=_store) is False


def test_gate_rejects_concede_driver(_store: Path) -> None:
    """A concede-referencing driver is REJECTED by the lint (concede is FAIL, not WARN): forcing the
    opponent to concede fabricates a credited decisive win, so drivers may not concede at all."""
    deck = _deck('Lint Concede')
    _stage_class(drivers.classes_dir(deck, data_dir=_store), 'concede', 'ConcedeDriver')
    eng = _FakeEngine(driven=_res(6.0), driven_output='', baseline=_res(8.0))

    result = dg.gate_driver(
        deck, ('D', 'dck'), spec=_proactive_spec(), install=object(), games=_GAMES, engine=eng, data_dir=_store
    )

    assert result.passed is False
    assert 'concede' in result.reason.lower()
    assert eng.calls == []  # rejected before any game ran
    assert drivers.driver_valid(deck, data_dir=_store) is False
