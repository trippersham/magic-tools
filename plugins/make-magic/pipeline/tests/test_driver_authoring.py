"""Phase 2 authoring tests — the thin-driver template + the Mikaeus fill.

All pure string generation: NO javac, NO JVM. Asserts the template bakes the
load-bearing spike discipline (seam ctor, copy ctor, NO copy() override, the
DRIVER_LINE_FIRED marker) and that the canonical Mikaeus fill is thin (owns the line,
force-attacks nothing).
"""

from __future__ import annotations

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_authoring as da


def _deck(name: str = 'Test Deck') -> Deck:
    return Deck(name=name, cards=[DeckCard(name='Forest', quantity=1)])


def test_fqcn_and_package_are_legal_java_identifiers() -> None:
    """A 32-hex uuid can begin with a digit — the package leaf must be prefixed so the
    FQCN is a legal Java name the seam can Class.forName()."""
    deck = _deck()
    pkg = da.driver_package(deck)
    fqcn = da.driver_fqcn(deck)
    assert pkg == f'{da.DRIVER_PACKAGE_ROOT}.d_{deck.uuid}'
    assert fqcn == f'{pkg}.Driver'
    # every dotted segment is a legal Java identifier (starts with a letter/underscore)
    for seg in fqcn.split('.'):
        assert seg[0].isalpha() or seg[0] == '_'
        assert all(c.isalnum() or c == '_' for c in seg)


def test_render_bakes_seam_ctor_copy_ctor_and_marker() -> None:
    """The template supplies the seam's (String,RangeOfInfluence,int) ctor, a copy ctor,
    and the idempotent DRIVER_LINE_FIRED marker — regardless of the LineSpec."""
    deck = _deck()
    src = da.render_driver(deck, da.MIKAEUS_LINE_SPEC)
    assert f'package {da.driver_package(deck)};' in src
    assert 'extends ComputerPlayer7' in src
    assert 'public Driver(String name, RangeOfInfluence range, int skill)' in src
    assert 'public Driver(final Driver d)' in src  # copy ctor for engine cloning
    assert 'protected void markLineFired()' in src
    assert f'System.err.println("{da.DRIVER_LINE_FIRED_MARKER} name=" + getName());' in src


def test_template_does_not_override_copy() -> None:
    """No copy() override: CP7's minimax simulations must run as pure CP7 so the owned
    overrides fire only on the real player's decisions (spike design)."""
    src = da.render_driver(_deck(), da.MIKAEUS_LINE_SPEC)
    assert 'copy()' not in src  # neither `public X copy()` nor a call to super.copy()


def test_mikaeus_fill_is_thin_and_owns_the_line() -> None:
    """The Mikaeus fill owns exactly the three thin responsibilities and calls the
    marker when the line executes — and force-attacks NOTHING (no selectAttackers)."""
    src = da.render_driver(_deck(), da.MIKAEUS_LINE_SPEC)
    # owns: guarded priority, scoped chooseTarget, light chooseMulligan
    assert 'public boolean priority(mage.game.Game game)' in src
    assert 'public boolean chooseTarget(' in src
    assert 'public boolean chooseMulligan(mage.game.Game game)' in src
    # executes the line -> emits the marker
    assert 'markLineFired();' in src
    # HARD RULE: never force-attack (no selectAttackers OVERRIDE in a thin driver)
    assert 'void selectAttackers' not in src
    # defers outside the owned line
    assert 'super.priority(game)' in src


def test_render_splices_arbitrary_members() -> None:
    """render_driver is generic: a LineSpec's members + imports land verbatim in the
    class body (Phase 4's LLM-authored lines flow through the same surface)."""
    spec = da.LineSpec(
        name='probe',
        imports=('import java.util.List;',),
        members='    // SENTINEL_MEMBER_BODY',
    )
    src = da.render_driver(_deck(), spec)
    assert 'import java.util.List;' in src
    assert '// SENTINEL_MEMBER_BODY' in src
