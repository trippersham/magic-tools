"""Sim dispatcher: ``python -m pipeline.sim.run <verb> [args...]``.

Mirrors ``collection/run.py`` exactly: a plain ``sys.argv`` dispatcher routing
``argv[0]`` to a per-verb ``argparse`` handler (``_verb(argv[1:])``), each
building its own ``ArgumentParser(prog='simulate <verb>')`` — no Typer, no new
dep. This is the human-facing surface over the Phase-6 sim core
(:mod:`pipeline.sim.core`), gauntlet (:mod:`pipeline.sim.gauntlet`), runner
(:mod:`pipeline.sim.runner`), Forge runtime (:mod:`pipeline.sim.forge_runtime`),
and governor (:mod:`pipeline.sim.governor`).

Verbs:
  * ``match <A> <B>`` — one head-to-head via ``run_matchup`` (win tally).
  * ``deck <name>`` — ``simulate`` a candidate over a gauntlet (win-rate ± CI,
    per-opponent breakdown, telemetry profile).
  * ``ab <A> <B>`` — ``compare`` two variants over the SAME gauntlet.
  * ``gauntlet show [--source <curated|bundle>]`` — list a packaged gauntlet's
    decks (offline, no Forge); defaults to ``curated``.
  * ``log <A> <B> [--game N]`` — retrieve a stored per-game verbose Forge log for
    a past matchup (offline, no Forge; forensic replay from DuckDB).
  * ``doctor [--provision]`` — Forge/Java resolution + version + derived pool
    size + a free-RAM/disk snapshot; graceful whether or not Forge is present.
    ``--provision`` downloads + caches Forge on a miss (one-time ~350MB).

``match`` / ``deck`` / ``ab`` AUTO-PROVISION Forge on first use (fetch-at-runtime,
one-time notice), so a fresh box needs no manual install; ``doctor`` stays
read-only unless ``--provision`` is passed.

Deck references (``<A>`` / ``<B>`` / ``<name>``) resolve as a ``.dck`` file path
(arg ends in ``.dck`` or is an existing file) OR an Airtable deck name (via
``get_store().get_deck`` -> ``ForgeDckExporter``). Domain errors
(``ForgeUnavailableError``, ``ForgeError``, ``CollectionError``, bad gauntlet
source) surface as a clean one-line ``error:`` + non-zero exit — never a raw
traceback (mirrors ``collection/run.py``'s handling).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pipeline.collection import CollectionError, get_store
from pipeline.contracts import Deck, DeckCard
from pipeline.destinations.deck_export import DeckExportError, get_exporter
from pipeline.sim.core import (
    Comparison,
    SimResult,
    compare,
    simulate,
)
from pipeline.sim.engine import (
    EngineInstall,
    EngineUnavailableError,
    SimEngine,
    available_engines,
    get_engine,
)
from pipeline.sim.forge_runtime import (
    ENV_FORGE_HOME,
    ENV_JAVA,
    ForgeInstall,
    ForgeUnavailableError,
)
from pipeline.sim.gauntlet import gauntlet_sources, resolve_gauntlet
from pipeline.sim.governor import derive_pool_size, free_disk_gib, free_ram_gib
from pipeline.sim.runner import ForgeError, MatchResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.sim.store import CachedMatchup, MatchupRow
    from pipeline.sim.telemetry import PilotingProfile

__all__ = ('main',)

#: Default per-opponent / per-matchup games when the caller doesn't pass one.
_DEFAULT_GAMES = 4
#: Default RNG seed (Forge's ``-s`` is not reliably reproducible; see core).
_DEFAULT_SEED = 42
#: Format choices exposed on the CLI.
_FORMAT_CHOICES = ('constructed', 'commander')
#: Minimum total card count (sum of quantities, basics included) a deck must have
#: to be a VALID sim in each format. A below-floor deck (e.g. an empty ``[Main]``
#: from a decklist that failed to render) is a STRUCTURAL error the availability
#: guard rejects before the JVM — Forge would otherwise happily play a 0-card deck
#: to a plausible-looking 0-10-0 (R3-1). ``constructed`` uses the 40-card
#: minimum-deck rule; ``commander`` the 100-card singleton rule.
_FORMAT_SIZE_FLOOR = {'constructed': 40, 'commander': 100}
#: Gauntlet sources exposed on the CLI: the core sources + every named bundle
#: shipped for any format (union), so ``--gauntlet <bundle>`` is accepted
#: regardless of ``--format`` arg order. The (source, format) pairing is validated
#: EARLY by :func:`_validate_gauntlet` (before any Forge download) — a bundle only
#: shipped for constructed is rejected under ``--format commander`` up front.
_GAUNTLET_CHOICES = tuple(dict.fromkeys(src for fmt in _FORMAT_CHOICES for src in gauntlet_sources(fmt)))
#: ``--gauntlet`` help — names the core sources and the shipped bundles so
#: ``guilds`` (the flagship 30-deck field) is discoverable from ``-h``.
_GAUNTLET_HELP = (
    "opponent field: 'curated' (small default), 'mine'/'both' (your own decks, "
    "needs a collection backend), or a shipped bundle — 'guilds' (30 decks: 10 "
    "two-color guilds x weak/mid/strong, constructed only). Default 'curated'."
)
#: ``--format`` help, shared across verbs.
_FORMAT_HELP = "deck format (default 'constructed'). 'commander' runs 1v1 EDH."
#: ``--yes`` help, shared across the game verbs (first-run download consent).
_YES_HELP = 'Auto-confirm the one-time ~350MB Forge download on first use (skip the TTY prompt).'


def _validate_gauntlet(source: str, fmt: str) -> None:
    """Reject a (gauntlet, format) mismatch UP FRONT, before Forge is provisioned.

    ``_GAUNTLET_CHOICES`` is the union across formats, so argparse alone would let
    e.g. ``--gauntlet guilds --format commander`` through and only fail deep inside
    :func:`~pipeline.sim.core.simulate` — after a possible ~350 MB Forge download.
    Validate here so the error is immediate and actionable (mirrors
    :func:`~pipeline.sim.gauntlet.resolve_gauntlet`'s own check)."""
    valid = gauntlet_sources(fmt)
    if source not in valid:
        raise CollectionError(
            f'gauntlet {source!r} is not available for --format {fmt}; choose from {", ".join(valid)}'
        )


# --------------------------------------------------------------------------- #
# Deck-arg resolution.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _ResolvedDeck:
    """A resolved deck reference: its name, rendered ``.dck`` text, and — when the
    source is the collection store — the fully-hydrated :class:`Deck`.

    ``deck`` is the source ``Deck`` (with per-card ``oracle_id``s) for an Airtable
    name; it is ``None`` for a raw ``.dck`` path (no Scryfall resolution info).
    Availability validation routes through this ``deck`` when present so it shares
    the destination's ``validate`` path; ``UNRESOLVED`` (name-only) warnings are
    meaningful only when it is present (a raw ``.dck`` legitimately has no
    ``oracle_id``s, so we don't warn on those).
    """

    name: str
    text: str
    deck: Deck | None

    @property
    def ref(self) -> tuple[str, str]:
        """The ``(name, dck_text)`` pair the sim core (``run_matchup``/``simulate``) consumes."""
        return (self.name, self.text)


def _resolve_deck_arg(arg: str) -> _ResolvedDeck:
    """Resolve a deck reference to a :class:`_ResolvedDeck`.

    A ``.dck`` path (arg ends in ``.dck`` or is an existing file) is read straight
    off disk — its stem is the deck name, ``deck`` is ``None``. Anything else is an
    Airtable deck NAME: resolved via ``get_store().get_deck`` and rendered with the
    Forge ``.dck`` exporter, keeping the hydrated ``Deck`` for validation. The
    single resolver both the deck-ref verbs (``match`` / ``deck`` / ``ab``) and the
    ``log`` verb route through.
    """
    from pathlib import Path

    path = Path(arg)
    if arg.endswith('.dck') or path.is_file():
        if not path.is_file():
            raise CollectionError(f'deck file not found: {arg}')
        return _ResolvedDeck(name=path.stem, text=path.read_text(), deck=None)

    deck = get_store().get_deck(arg)
    return _ResolvedDeck(name=deck.name, text=get_exporter('forge_dck').export(deck), deck=deck)


# --------------------------------------------------------------------------- #
# Forge provisioning.
# --------------------------------------------------------------------------- #


def _confirm_engine_download(engine_name: str, *, assume_yes: bool) -> None:
    """Gate a first-run engine download on consent when stdin is a TTY.

    A stranger typing ``simulate deck …`` to explore should not silently pull a large
    artifact (Forge ~350 MB, XMage ~76 MB) on a possibly-metered connection. So on the
    fetch path:

      * ``--yes`` (``assume_yes``) or a NON-interactive stdin (agent / CI / pipe)
        proceeds without a prompt — the auto-provision promise is kept for automation.
      * an INTERACTIVE stdin (a TTY) is asked to confirm; a non-``y`` answer aborts
        with a clean :class:`EngineUnavailableError` naming the escape hatches. The
        engine's own ``resolve(provision=True)`` then surfaces the size-specific
        "downloading…" notice.
    """
    if assume_yes or not sys.stdin.isatty():
        return
    print(
        f'The {engine_name} sim engine is not installed. Download it now (one-time, cached for reuse)?', file=sys.stderr
    )
    answer = input('  proceed? [y/N] ').strip().lower()
    if answer not in ('y', 'yes'):
        raise EngineUnavailableError(
            f'{engine_name} download declined. Re-run with --yes to auto-provision, or '
            '`simulate doctor --provision` to install it explicitly.'
        )


#: Default engine when ``--engine`` is omitted (Forge is the only backend this
#: phase; the choices are the registry contents so a future engine auto-appears).
_DEFAULT_ENGINE = 'forge'
#: The ``deck`` verb's pseudo-engine that runs EVERY registered engine and prints a
#: side-by-side comparison (task 3.1). Not a registered backend — a CLI-level fan-out.
_BOTH_ENGINE = 'both'
_ENGINE_HELP = 'Sim engine to use (default forge). Registry-driven — additional engines appear as they register.'
_ENGINE_HELP_BOTH = (
    "Sim engine to use (default forge), or 'both' to run every registered engine and print a "
    'side-by-side win-rate + piloting comparison. Registry-driven; one engine unavailable is reported + skipped.'
)


def _add_engine_arg(parser: argparse.ArgumentParser, *, include_both: bool = False) -> None:
    """Add the ``--engine`` selector, its choices drawn from the live registry.

    Choices are :func:`~pipeline.sim.engine.available_engines` at parse time so a
    newly-registered engine (or a test's ``FakeEngine``) is selectable without
    touching this code, and an unknown name is rejected by argparse with a clean
    ``error:`` + exit 2 (no traceback). ``include_both`` adds the ``both``
    pseudo-engine (the two-engine compare fan-out) — offered only on the ``deck``
    evaluation verb, where a per-engine win-rate + piloting divergence is meaningful
    (``match``/``ab``/``log`` stay single-engine).
    """
    choices = [*available_engines(), _BOTH_ENGINE] if include_both else available_engines()
    parser.add_argument(
        '--engine',
        choices=choices,
        default=_DEFAULT_ENGINE,
        help=_ENGINE_HELP_BOTH if include_both else _ENGINE_HELP,
    )


def _ensure_engine(engine: SimEngine, *, assume_yes: bool = False) -> EngineInstall:
    """Resolve ``engine`` for a game verb, AUTO-PROVISIONING on first use.

    Game verbs (``match`` / ``deck`` / ``ab``) call this so a fresh box provisions
    the backend itself on the first run (the fetch-at-runtime promise): the engine's
    read-only :meth:`~pipeline.sim.engine.SimEngine.resolve` is tried first, and on
    an :class:`~pipeline.sim.engine.EngineUnavailableError` the ~350 MB pull is
    gated on confirmation (:func:`_confirm_forge_download`) when stdin is a TTY
    (unless ``--yes``; agent/CI proceed silently) before a provisioning resolve,
    which surfaces the one-time download notice. An impossible fetch (offline)
    still raises ``EngineUnavailableError`` → the ``main`` handler prints a clean
    error. Returns the :class:`~pipeline.sim.engine.EngineInstall` the engine's
    ``run_matchup`` / ``simulate`` consume.

    Both backends now auto-provision (2.3b): Forge fetches its ~350 MB release + JRE,
    XMage the ~76 MB shaded distributable jar. The consent
    (:func:`_confirm_engine_download`) is generic; the engine's own
    ``resolve(provision=True)`` surfaces the size-specific "downloading…" notice. An
    impossible fetch still raises ``EngineUnavailableError`` → ``main`` prints a clean
    error (and ``--engine both`` treats it as a clean SKIP).
    """
    try:
        return engine.resolve(provision=False)
    except EngineUnavailableError:
        pass
    _confirm_engine_download(engine.name, assume_yes=assume_yes)
    return engine.resolve(provision=True)


# --------------------------------------------------------------------------- #
# Rendering helpers (stable, human-facing text).
# --------------------------------------------------------------------------- #


def _pct(x: float) -> str:
    """Format a rate in [0, 1] as a percent."""
    return f'{x * 100:.1f}%'


def _print_sim_result(result: SimResult) -> None:
    """Print a :class:`SimResult`: overall win-rate ± CI, per-opponent, telemetry."""
    lo, hi = result.win_rate_ci
    print(f'candidate: {result.candidate}   gauntlet: {result.gauntlet_source} ({result.fmt})')
    print(
        f'overall win-rate: {_pct(result.win_rate)}  '
        f'[95% CI {_pct(lo)}-{_pct(hi)}]  '
        f'({result.wins}-{result.losses}-{result.draws} over {result.total_games} games)'
    )
    print(f'matchups: {len(result.per_opponent)}  (cached {result.cached_matchups}, fresh {result.fresh_matchups})')
    print('per-opponent:')
    for opp in result.per_opponent:
        olo, ohi = opp.win_rate_ci
        cached = ' [cached]' if opp.cached else ''
        print(
            f'  {opp.opponent:<24} {_pct(opp.win_rate)}  '
            f'[{_pct(olo)}-{_pct(ohi)}]  '
            f'({opp.wins}-{opp.losses}-{opp.draws}){cached}'
        )
    _print_profile(result.profile)
    _print_piloting(result.piloting)
    _print_failures(result)


def _print_failures(result: SimResult) -> None:
    """Surface any matchup FAILURES + a partial-run notice to stderr (B2).

    A failed matchup (deck-load / timeout / crash) produced NO usable games — it is
    visibly DISTINCT from a real 0-0-0 "lost every game" row, which the
    per-opponent table shows. Printing to stderr keeps stdout the clean result
    table while making a silent all-zeros run impossible.
    """
    if result.aborted:
        print(
            'WARNING: the run was ABORTED before every matchup ran (persistent RAM/disk '
            'starvation) — results are PARTIAL.',
            file=sys.stderr,
        )
    if result.failures:
        print(
            f'WARNING: {len(result.failures)} matchup(s) FAILED (no games produced — NOT a 0-0-0 loss):',
            file=sys.stderr,
        )
        for opponent, error in result.failures:
            print(f'  vs {opponent}: {error}', file=sys.stderr)


#: A run is UNTRUSTWORTHY when MORE THAN this fraction of its matchups FAILED
#: (produced no usable games) — even if a surviving matchup kept ``total_games > 0``.
#: Half is the line: a few intermittent failures (e.g. an XMage cold-start race on
#: 3/30) still leave a usable field read, but a MAJORITY-failed run must fail the
#: exit code so a cron/agent can't read success off a mostly-dead run (R2-3).
_MAX_MATCHUP_FAILURE_FRACTION = 0.5


def _exit_nonzero_on_unusable_run(*results: SimResult) -> None:
    """Exit non-zero when a run is UNUSABLE: no games, aborted, or MOSTLY failed (R2-3).

    ``simulate deck`` / ``ab`` print failures to stderr but otherwise exit 0 — so a
    run that is dead or mostly-dead would read as SUCCESS to a cron/agent consumer.
    Raise :class:`SystemExit(1)` when any given result is unusable, on three grounds:

    * ``total_games == 0`` — EVERY matchup failed (a garbage deck, a jar-less install)
      so no game was ever played; or
    * ``aborted`` — the governor stopped admitting work (persistent RAM/disk
      starvation), so the field is only partially run; or
    * MORE THAN :data:`_MAX_MATCHUP_FAILURE_FRACTION` of the matchups FAILED — a
      surviving matchup kept ``total_games > 0``, but a majority of the field never
      produced a game, so the aggregate is not a trustworthy read (the exit code
      must scale with the failure rate, not just flip at all-or-nothing).

    A legitimate low/zero win-rate with real games played (lost every game), or a
    MINORITY of intermittent matchup failures (the field read still stands), still
    exits 0. ``_print_failures`` has already surfaced the WHY on stderr in every case.
    """
    for result in results:
        if result.aborted or result.total_games == 0:
            raise SystemExit(1)
        # Denominator is the TOTAL field attempted = successes + failures. `per_opponent`
        # holds ONLY the matchups that produced games; failures are tracked separately, so
        # dividing by len(per_opponent) alone trips the guard at a ~1/3 failure rate rather
        # than the documented majority (a 30-deck field, 12 failed, would wrongly exit 1).
        total_matchups = len(result.per_opponent) + len(result.failures)
        if total_matchups and len(result.failures) > _MAX_MATCHUP_FAILURE_FRACTION * total_matchups:
            raise SystemExit(1)


def _print_profile(profile: object) -> None:
    """Print the telemetry :class:`~pipeline.sim.core.TelemetryProfile` block."""
    from pipeline.sim.core import TelemetryProfile

    assert isinstance(profile, TelemetryProfile)
    print('telemetry:')
    print(f'  games:            {profile.games}')
    print(f'  avg kill turn:    {_fmt_opt(profile.avg_kill_turn)}')
    print(f'  median kill turn: {_fmt_opt(profile.median_kill_turn)}')
    print(f'  avg win margin:   {_fmt_opt(profile.avg_win_margin_life)} life')
    print(f'  wincon mix:       {profile.wincon_mix or "-"}')
    if profile.mean_ramp_curve:
        curve = ', '.join(f'{v:.1f}' for v in profile.mean_ramp_curve)
        print(f'  mean ramp curve:  [{curve}]')


def _fmt_opt(value: float | None) -> str:
    """Format an optional float metric ('-' when None)."""
    return '-' if value is None else f'{value:.2f}'


def _print_piloting(piloting: object) -> None:
    """Print the PILOTING block — "is a low win-rate the DECK or the AI piloting it?".

    Gated on the engine's hand visibility: ``piloting`` is ``None`` for a backend
    that can't see hidden zones (nothing printed). When present but UNAVAILABLE (the
    otag lake could not classify the deck's interaction), print ONE honest line
    naming the reason — NEVER a fabricated 0/0. When available, a compact block:
    opportunity-conditioned counter fire-rate (+ Wilson CI), removal fire-rate, and
    interaction stranded/game, under a one-line frame.
    """
    if piloting is None:
        return  # engine lacks hand visibility — no piloting signal to show.
    from pipeline.sim.core import PilotingProfile

    assert isinstance(piloting, PilotingProfile)
    print('piloting:')
    if not piloting.available:
        # Honest single line — the deck's interaction could not be classified.
        print(f'  unavailable ({piloting.reason})')
        return
    print('  (of moments the deck HELD the card + a legal target + the mana, how often the AI cast it)')
    print(
        f'  counter fire-rate: {_fmt_fire(piloting.counter_fire, piloting.counter_ci)}  '
        f'({piloting.counter_casts}/{piloting.counter_opps} opps)'
    )
    print(
        f'  removal fire-rate: {_fmt_fire(piloting.removal_fire, piloting.removal_ci)}  '
        f'({piloting.removal_casts}/{piloting.removal_opps} opps)'
    )
    print(f'  interaction stranded/game: {piloting.interaction_stranded_per_game:.2f}  (over {piloting.games} game(s))')
    _print_coverage(piloting)


def _print_coverage(piloting: object) -> None:
    """Print the classification COVERAGE line under the piloting block.

    How much of the deck the metric could actually "see": of the distinct non-land
    cards, how many carried otags. Low coverage means the fire-rates rest on a
    partial view (a card with no otags is invisible to the opportunity model). When
    a FEW cards are uncategorized they are named (so a silently-dropped interaction
    card is spottable); a larger set is summarized. Skipped when coverage wasn't
    populated (older cached results / non-Forge paths → ``cards_total == 0``).
    """
    from pipeline.sim.core import PilotingProfile

    assert isinstance(piloting, PilotingProfile)
    if piloting.cards_total <= 0:
        return
    line = f'  classification coverage: {piloting.cards_classified}/{piloting.cards_total} non-land cards carry otags'
    unc = piloting.uncategorized
    if unc:
        shown = ', '.join(unc[:_COVERAGE_NAME_CAP])
        more = f', +{len(unc) - _COVERAGE_NAME_CAP} more' if len(unc) > _COVERAGE_NAME_CAP else ''
        line += f'  (uncategorized: {shown}{more})'
    print(line)


#: How many uncategorized card names to list inline before summarizing the rest.
_COVERAGE_NAME_CAP = 8


def _fmt_fire(fire: float | None, ci: tuple[float, float] | None) -> str:
    """Format a fire-rate + its Wilson CI ('-' when there were no opportunities)."""
    if fire is None:
        return '-  (no opportunities)'
    if ci is None:
        return _pct(fire)
    lo, hi = ci
    return f'{_pct(fire)}  [95% CI {_pct(lo)}-{_pct(hi)}]'


# --------------------------------------------------------------------------- #
# Verbs.
# --------------------------------------------------------------------------- #


def _dck_card_names(dck_text: str) -> list[str]:
    """The card names referenced by a rendered ``.dck`` ([Main] + [Commander] + [Sideboard]).

    Parses ``<qty> <name>`` lines under the card sections, skipping headers and
    metadata — so the availability guard can check exactly the names Forge will
    try to load. The sideboard IS included: a Forge-unloadable sideboard card
    would otherwise reach Forge unvalidated (the silent-drop class the guard
    exists to kill) via the ``.dck`` path, which the Airtable-hydrated path
    already validates. Accepts REAL Forge ``.dck`` files, not just our exporter's
    output: section headers are matched case-insensitively (Forge writes
    ``[main]``) and a pinned printing (``<name>|SET`` or ``<name>|SET|art``) is
    stripped to the bare name (the index knows names, not printings).
    """
    names: list[str] = []
    in_cards = False
    for line in dck_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('['):
            in_cards = stripped.lower() in ('[main]', '[commander]', '[sideboard]')
            continue
        if not in_cards:
            continue
        qty, _, name = stripped.partition(' ')
        name = name.split('|', 1)[0].strip()
        if qty.isdigit() and name:
            names.append(name)
    return names


def _dck_total_cards(dck_text: str) -> int:
    """Sum the quantities of every card in a rendered ``.dck`` ([Main]+[Commander]+[Sideboard]).

    Mirrors :func:`_dck_card_names`'s section walk but SUMS the ``<qty>`` prefixes
    (a name-only reconstruction would collapse ``40 Mountain`` to one card and
    defeat the size floor). Case-insensitive headers + pinned-printing tolerant,
    for the same reasons documented on :func:`_dck_card_names`.
    """
    total = 0
    in_cards = False
    for line in dck_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('['):
            in_cards = stripped.lower() in ('[main]', '[commander]', '[sideboard]')
            continue
        if not in_cards:
            continue
        qty, _, name = stripped.partition(' ')
        name = name.split('|', 1)[0].strip()
        if qty.isdigit() and name:
            total += int(qty)
    return total


def _total_cards(resolved: _ResolvedDeck) -> int:
    """Total cards (summed quantities) in a resolved deck, for the size floor.

    A store-hydrated deck sums its :attr:`DeckCard.quantity` values; a ``.dck``
    path is counted from its rendered text (:func:`_dck_total_cards`), since the
    name-only reconstruction loses per-card quantities.
    """
    if resolved.deck is not None:
        return sum(card.quantity for card in resolved.deck.cards)
    return _dck_total_cards(resolved.text)


def _deck_from_dck(name: str, dck_text: str) -> Deck:
    """Reconstruct a minimal :class:`Deck` from rendered ``.dck`` text (for a path arg).

    Only the card NAMES are recoverable from a raw ``.dck`` (no ``oracle_id``s), so
    every card is name-only — that is exactly why the guard suppresses the
    resulting ``UNRESOLVED`` warnings for path decks and acts only on the
    (real) ``ABSENT_FROM_TARGET`` findings.
    """
    cards = [DeckCard(name=cn) for cn in _dck_card_names(dck_text)]
    return Deck(name=name, cards=cards)


def _guard_deck_size(decks: list[_ResolvedDeck], fmt: str) -> None:
    """Reject a hollow / below-minimum deck BEFORE the JVM (R3-1, structural).

    An empty ``[Main]`` (a decklist that failed to render) references no absent
    cards, so the availability guard passes it — and Forge then plays it to a
    plausible-looking 0-10-0. That is never a valid sim, so a deck whose total card
    count (summed quantities, basics included) is below the format floor
    (:data:`_FORMAT_SIZE_FLOOR`: constructed 40, commander 100) is a BLOCKING
    :class:`DeckExportError` naming the deficiency. This is a STRUCTURAL error: it
    is NOT downgraded by ``--allow-missing`` (that flag only softens card
    AVAILABILITY) and needs no card DB, so it is checked independently of whether
    the availability index can be built.
    """
    floor = _FORMAT_SIZE_FLOOR.get(fmt, _FORMAT_SIZE_FLOOR['constructed'])
    for resolved in decks:
        total = _total_cards(resolved)
        if total < floor:
            # A plain ValueError (not a card-level DeckExportError, which carries a
            # ValidationReport): this is a WHOLE-DECK structural defect, not a set of
            # unusable cards. The CLI's top-level handler catches ValueError → a clean
            # `error:` line + non-zero exit, never a traceback.
            raise ValueError(
                f'deck {resolved.name!r} has {total} card(s); {fmt} requires >= {floor} '
                '— did the decklist fail to render (empty/near-empty deck)?'
            )


def _guard_forge_availability(
    install: ForgeInstall,
    decks: list[_ResolvedDeck],
    *,
    allow_missing: bool,
    fmt: str = 'constructed',
    engine: str = 'forge',
) -> None:
    """Fail BEFORE spawning a JVM if a deck is hollow or references an unloadable card.

    Two independent pre-JVM checks:

    * A DECK-SIZE FLOOR (:func:`_guard_deck_size`) rejects a below-minimum deck for
      ``fmt`` (constructed >= 40, commander >= 100) — a STRUCTURAL error that
      ``--allow-missing`` does NOT bypass and that runs even when the card index
      can't be built (R3-1). Checked FIRST.
    * Card AVAILABILITY routes through the DESTINATION's own validation —
      ``ForgeDckExporter.validate`` backed by a
      :class:`~pipeline.sim.forge_card_index.ForgeCardIndex` — so the classification
      lives in ONE place (the forge_dck card exporter), not re-implemented here. For
      an Airtable deck the hydrated :class:`Deck` is validated directly; for a
      ``.dck`` path it is reconstructed from the rendered names
      (:func:`_deck_from_dck`). A card ABSENT from Forge's DB is a BLOCKING
      :class:`DeckExportError` (naming the offenders) — unless ``allow_missing``,
      which downgrades it to a stderr warning. ``UNRESOLVED`` (name-only) cards are
      surfaced as warnings ONLY for store-resolved decks (a raw ``.dck`` legitimately
      carries no ``oracle_id``s). If the index can't be built (a minimal install
      without ``cardsfolder.zip``), the AVAILABILITY check is skipped — Forge's own
      loader remains the backstop — but the size floor above still applies.
    """
    from pipeline.sim.forge_card_index import ForgeCardIndex

    _guard_deck_size(decks, fmt)

    # The deck-size floor above is engine-agnostic; the CARD-availability check
    # below is Forge-specific (a ForgeCardIndex over the Forge DB). A non-Forge
    # engine (e.g. XMage) has its own card DB + loader as the backstop (the shipped
    # decks are verified to load there), so skip the Forge check for it. The size
    # floor still applies to every engine.
    if engine != 'forge':
        return

    try:
        index = ForgeCardIndex.from_install(install)
    except (FileNotFoundError, OSError):
        return

    exporter = get_exporter('forge_dck', availability=index)
    for resolved in decks:
        deck = resolved.deck if resolved.deck is not None else _deck_from_dck(resolved.name, resolved.text)
        report = exporter.validate(deck)
        if report.blocking:
            if allow_missing:
                print(f'warning: {DeckExportError(report)} (proceeding: --allow-missing)', file=sys.stderr)
            else:
                raise DeckExportError(report)
        # UNRESOLVED (name-only) is meaningful only when the deck was store-resolved.
        if resolved.deck is not None:
            for issue in report.warnings:
                print(f'warning: {resolved.name}: {issue.card_name} — {issue.detail}', file=sys.stderr)


def _match(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='simulate match',
        description='Play a single head-to-head between two decks; print the raw win tally (no gauntlet, no CI).',
    )
    parser.add_argument('deck_a', help='Deck A: a .dck path or an Airtable deck name.')
    parser.add_argument('deck_b', help='Deck B: a .dck path or an Airtable deck name.')
    parser.add_argument('-n', type=int, default=_DEFAULT_GAMES, help=f'games (default {_DEFAULT_GAMES}).')
    parser.add_argument('-s', '--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed', help=_FORMAT_HELP)
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if a deck references a card absent from Forge (else a hard error).',
    )
    parser.add_argument('-y', '--yes', action='store_true', help=_YES_HELP)
    _add_engine_arg(parser)
    args = parser.parse_args(argv)

    deck_a = _resolve_deck_arg(args.deck_a)
    deck_b = _resolve_deck_arg(args.deck_b)
    engine = get_engine(args.engine)
    install = _ensure_engine(engine, assume_yes=args.yes)
    _guard_forge_availability(
        install.handle, [deck_a, deck_b], allow_missing=args.allow_missing, fmt=args.fmt, engine=engine.name
    )
    result: MatchResult = engine.run_matchup(
        deck_a.ref, deck_b.ref, n=args.n, seed=args.seed, fmt=args.fmt, install=install
    )
    print(f'{deck_a.name} vs {deck_b.name}  ({args.fmt}, n={args.n}, seed={args.seed})')
    print(f'{deck_a.name}: {result.wins_a} wins   {deck_b.name}: {result.wins_b} wins   draws: {result.draws}')


def _deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='simulate deck',
        description='Evaluate one deck against a gauntlet of opponents; print win-rate ± CI, '
        'per-opponent results, and a telemetry profile.',
    )
    parser.add_argument('name', help='Candidate deck: a .dck path or an Airtable deck name.')
    parser.add_argument('--gauntlet', choices=_GAUNTLET_CHOICES, default='curated', help=_GAUNTLET_HELP)
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed', help=_FORMAT_HELP)
    parser.add_argument('--games', type=int, default=_DEFAULT_GAMES, help=f'games/opponent (default {_DEFAULT_GAMES}).')
    parser.add_argument(
        '--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}); part of the cache key.'
    )
    parser.add_argument('--force', action='store_true', help='Bypass the matchup cache (re-run every matchup).')
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if the candidate references a card absent from Forge (else a hard error).',
    )
    parser.add_argument('-y', '--yes', action='store_true', help=_YES_HELP)
    _add_engine_arg(parser, include_both=True)
    args = parser.parse_args(argv)

    _validate_gauntlet(args.gauntlet, args.fmt)
    candidate = _resolve_deck_arg(args.name)
    # `mine`/`both` GAUNTLET sources need the collection store; `curated` never
    # touches it. (Distinct from the `both` ENGINE selector below.)
    store = get_store() if args.gauntlet in ('mine', 'both') else None

    if args.engine == _BOTH_ENGINE:
        _deck_both(args, candidate, store)
        return

    engine = get_engine(args.engine)
    result = _evaluate_engine(engine, candidate, store, args)
    _print_sim_result(result)
    # After surfacing failures on stderr, fail the process if the run yielded no
    # usable games (all matchups failed) or was aborted — so automation can't read
    # success on a dead run (R2-3). A real low/zero win-rate still exits 0.
    _exit_nonzero_on_unusable_run(result)


def _guard_engine_supports_format(engine: SimEngine, fmt: str) -> None:
    """Raise :class:`EngineUnavailableError` if ``engine`` cannot run ``fmt``.

    Duck-typed on an optional ``supports_format(fmt) -> bool`` (mirrors how
    ``per_jvm_gib`` / ``max_concurrency`` are read off engines without widening the
    ``SimEngine`` Protocol, so the test fakes stay ``isinstance``-valid). An engine
    that does not declare the method supports every format. Modeled as
    ``EngineUnavailableError`` so both the single-engine handler (clean error) and
    ``_deck_both`` (clean SKIP) treat it uniformly — the engine genuinely can't run
    this request."""
    supports = getattr(engine, 'supports_format', None)
    if callable(supports) and not supports(fmt):
        raise EngineUnavailableError(f'the {engine.name} engine does not support the {fmt} format.')


def _evaluate_engine(
    engine: SimEngine, candidate: _ResolvedDeck, store: object | None, args: argparse.Namespace
) -> SimResult:
    """Resolve + guard + simulate ONE engine for the ``deck`` verb.

    The shared body of the single-engine and ``--engine both`` paths: provision the
    backend (:func:`_ensure_engine`), run the pre-JVM availability/size guard, and
    ``simulate`` the candidate over the gauntlet. An :class:`EngineUnavailableError`
    (backend not installed) propagates so ``both`` can catch it as a SKIP; a deck
    defect (size floor / Forge-absent card) is a hard error for the whole command.
    An engine that does not support the requested FORMAT also raises
    :class:`EngineUnavailableError` here — before provisioning — so ``both`` skips it
    cleanly instead of running it into a bogus 0-0-0 row.
    """
    # Pre-flight FORMAT guard (before provisioning): an engine that can't run this
    # format (e.g. XMage + commander) becomes a clean engine-level SKIP in `both`
    # and a clean error single-engine — never a per-matchup failure. Duck-typed:
    # an engine without `supports_format` supports every format.
    _guard_engine_supports_format(engine, args.fmt)
    install = _ensure_engine(engine, assume_yes=args.yes)
    _guard_forge_availability(
        install.handle, [candidate], allow_missing=args.allow_missing, fmt=args.fmt, engine=engine.name
    )
    return simulate(
        candidate.ref,
        args.gauntlet,
        games=args.games,
        fmt=args.fmt,
        seed=args.seed,
        engine=engine,
        force=args.force,
        store=store,
    )


def _deck_both(args: argparse.Namespace, candidate: _ResolvedDeck, store: object | None) -> None:
    """Run the gauntlet on EVERY registered engine and print a side-by-side read (task 3.1).

    For each engine in :func:`~pipeline.sim.engine.available_engines` (sorted → a
    stable Forge-then-XMage order), evaluate the candidate; an engine that is not
    installed (:class:`EngineUnavailableError`) is recorded as a SKIP and the others
    still run (design §8 — resilient degradation). The comparison surfaces each
    engine's win-rate ± CI, the Δ, and the PILOTING divergence — the "false read"
    check: an engine that under-pilots the deck's interaction (low counter/removal
    fire-rate) gives a weaker win-rate read where the engines disagree.

    Exits non-zero only if NO engine could run (all unavailable → nothing to
    compare), or — matching the single-engine contract — if an engine that DID run
    produced no usable games / was aborted (a broken half can't hide behind the
    other's numbers).
    """
    results: dict[str, SimResult] = {}
    skips: dict[str, str] = {}
    for name in available_engines():
        engine = get_engine(name)
        try:
            results[name] = _evaluate_engine(engine, candidate, store, args)
        except EngineUnavailableError as exc:
            skips[name] = str(exc)

    if not results:
        detail = '; '.join(f'{name}: {reason}' for name, reason in skips.items())
        raise EngineUnavailableError(f'no sim engine is available for --engine both ({detail}).')

    _print_engine_comparison(candidate.name, args.gauntlet, args.fmt, results, skips)
    _exit_nonzero_on_unusable_run(*results.values())


def _ab(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='simulate ab',
        description='A/B two deck variants over the SAME gauntlet; print each win-rate ± CI, the delta, '
        'and per-metric telemetry deltas.',
    )
    parser.add_argument('deck_a', help='Variant A: a .dck path or an Airtable deck name.')
    parser.add_argument('deck_b', help='Variant B: a .dck path or an Airtable deck name.')
    parser.add_argument('--gauntlet', choices=_GAUNTLET_CHOICES, default='curated', help=_GAUNTLET_HELP)
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed', help=_FORMAT_HELP)
    parser.add_argument('--games', type=int, default=_DEFAULT_GAMES, help=f'games/opponent (default {_DEFAULT_GAMES}).')
    parser.add_argument(
        '--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}); part of the cache key.'
    )
    parser.add_argument('--force', action='store_true', help='Bypass the matchup cache (re-run every matchup).')
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if a variant references a card absent from Forge (else a hard error).',
    )
    parser.add_argument('-y', '--yes', action='store_true', help=_YES_HELP)
    _add_engine_arg(parser)
    args = parser.parse_args(argv)

    _validate_gauntlet(args.gauntlet, args.fmt)
    variant_a = _resolve_deck_arg(args.deck_a)
    variant_b = _resolve_deck_arg(args.deck_b)
    store = get_store() if args.gauntlet in ('mine', 'both') else None
    engine = get_engine(args.engine)
    install = _ensure_engine(engine, assume_yes=args.yes)
    _guard_forge_availability(
        install.handle, [variant_a, variant_b], allow_missing=args.allow_missing, fmt=args.fmt, engine=engine.name
    )
    comparison: Comparison = compare(
        variant_a.ref,
        variant_b.ref,
        args.gauntlet,
        games=args.games,
        fmt=args.fmt,
        seed=args.seed,
        engine=engine,
        force=args.force,
        store=store,
    )
    _print_comparison(comparison)
    # Fail the process if EITHER variant produced no usable games or was aborted
    # (R2-3) — an A/B where one side never ran is not a comparison a consumer can
    # trust as a clean success.
    _exit_nonzero_on_unusable_run(comparison.a, comparison.b)


def _print_comparison(comparison: Comparison) -> None:
    """Print a :class:`Comparison`: Δ win-rate + CIs, per-metric deltas, winner."""
    a, b = comparison.a, comparison.b
    alo, ahi = a.win_rate_ci
    blo, bhi = b.win_rate_ci
    print(f'A: {a.candidate:<24} {_pct(a.win_rate)}  [{_pct(alo)}-{_pct(ahi)}]')
    print(f'B: {b.candidate:<24} {_pct(b.win_rate)}  [{_pct(blo)}-{_pct(bhi)}]')
    print(f'delta win-rate (A - B): {comparison.win_rate_delta * 100:+.1f} pts')
    stronger = comparison.stronger if comparison.stronger is not None else '(tie)'
    print(f'stronger: {stronger}')
    print('per-metric deltas (A - B):')
    for metric, delta in comparison.metric_deltas.items():
        print(f'  {metric:<24} {_fmt_signed(delta)}')
    # Surface either variant's matchup failures / abort (B2) — labelled by side so
    # a failed A vs a failed B are distinguishable.
    for label, side in (('A', a), ('B', b)):
        if side.aborted or side.failures:
            print(f'[variant {label}: {side.candidate}]', file=sys.stderr)
            _print_failures(side)


def _fmt_signed(value: float | None) -> str:
    """Format a signed metric delta ('-' when None)."""
    return '-' if value is None else f'{value:+.2f}'


# --------------------------------------------------------------------------- #
# --engine both — side-by-side engine comparison (task 3.1).
# --------------------------------------------------------------------------- #

#: A counter/removal fire-rate gap (in rate units) below which the two engines'
#: interaction piloting is "comparable" — no false-read is flagged.
_FALSE_READ_FIRE_GAP = 0.15
#: A win-rate gap below which the engines "agree" — a false-read needs BOTH a
#: piloting divergence AND a win-rate divergence to be worth warning about.
_FALSE_READ_WR_GAP = 0.05


def _print_engine_comparison(
    candidate: str,
    gauntlet: str,
    fmt: str,
    results: dict[str, SimResult],
    skips: dict[str, str],
) -> None:
    """Print the ``--engine both`` side-by-side: per-engine win-rate + piloting, Δ, false-read.

    One row per engine that RAN (win-rate ± CI, decided record, counter/removal
    fire-rate); then the pairwise Δ and the computed false-read note when exactly two
    engines ran. SKIPPED engines (unavailable) and any per-engine matchup
    failures/aborts go to stderr so stdout stays the clean comparison table.
    """
    print(f'candidate: {candidate}   gauntlet: {gauntlet} ({fmt})')
    print('engine comparison — per-engine win-rate + interaction piloting (the "false read" check):')
    print(f'  {"engine":<8} {"win-rate":<22} {"record":<12} {"counter-fire":<14} {"removal-fire":<14}')
    any_piloting_na = False
    for name, result in results.items():
        lo, hi = result.win_rate_ci
        win_rate = f'{_pct(result.win_rate)} [{_pct(lo)}-{_pct(hi)}]'
        record = f'{result.wins}-{result.losses}-{result.draws}'
        counter = _fire_cell(result.piloting, 'counter_fire')
        removal = _fire_cell(result.piloting, 'removal_fire')
        any_piloting_na = any_piloting_na or 'n/a' in (counter, removal)
        print(f'  {name:<8} {win_rate:<22} {record:<12} {counter:<14} {removal:<14}')

    # The piloting columns are the advertised "false read" differentiator; when they
    # render n/a (no otag lake for this deck) the table looks broken without a reason.
    # The single-engine path prints this hint; the `both` table must too.
    if any_piloting_na:
        print(
            "  (counter/removal-fire n/a: this deck's cards carry no otags — run the otag "
            'build to enable the interaction piloting "false read" check.)'
        )

    if len(results) < 2:
        # Only one engine produced results (the other was skipped / not registered).
        # A one-row table is NOT a comparison — say so explicitly rather than letting
        # the deltas + false-read silently no-op and read as "nothing to report".
        only = next(iter(results))
        print(f'  (only {only} ran — no cross-engine comparison; see the SKIPPED line(s) below.)')
    _print_engine_deltas(results)
    _print_false_read_note(results)

    for name, reason in skips.items():
        print(f'  {name}: SKIPPED (not available) — {reason}', file=sys.stderr)
    for name, result in results.items():
        if result.aborted or result.failures:
            print(f'[engine {name}: {result.candidate}]', file=sys.stderr)
            _print_failures(result)


def _fire_rate(piloting: PilotingProfile | None, attr: str) -> float | None:
    """The counter/removal fire-rate as a FLOAT (for the Δ math), or ``None`` when
    there is no honest number: the engine lacks hand visibility (``piloting is None``),
    the deck's interaction couldn't be classified (``not available``), or there were no
    opportunities (the fire-rate itself is ``None``). Companion to :func:`_fire_cell`,
    which renders the SAME value as a display string."""
    if piloting is None or not piloting.available:
        return None
    return getattr(piloting, attr)


def _fire_cell(piloting: PilotingProfile | None, attr: str) -> str:
    """Render a fire-rate table cell as a STRING: a percent, ``-`` (no opportunities),
    or ``n/a`` (no piloting signal — engine blind or deck unclassified). Companion to
    :func:`_fire_rate`, which returns the SAME value as a float for the delta math."""
    if piloting is None or not piloting.available:
        return 'n/a'
    rate = getattr(piloting, attr)
    return '-' if rate is None else _pct(rate)


def _print_engine_deltas(results: dict[str, SimResult]) -> None:
    """Print the pairwise Δ (win-rate + fire-rates) when exactly two engines ran.

    Δ is ``first - second`` in the sorted engine order (Forge - XMage). A fire-rate
    Δ is shown only when BOTH engines produced an honest fire-rate (else the
    subtraction would be meaningless)."""
    if len(results) != 2:
        return
    (name_a, a), (name_b, b) = results.items()
    parts = [f'win-rate {(a.win_rate - b.win_rate) * 100:+.1f} pts']
    for label, attr in (('counter-fire', 'counter_fire'), ('removal-fire', 'removal_fire')):
        fa, fb = _fire_rate(a.piloting, attr), _fire_rate(b.piloting, attr)
        if fa is not None and fb is not None:
            parts.append(f'{label} {(fa - fb) * 100:+.1f} pts')
    print(f'  Δ ({name_a} - {name_b}): ' + '   '.join(parts))


def _print_false_read_note(results: dict[str, SimResult]) -> None:
    """Print the computed false-read note when two engines ran with comparable piloting.

    The note is COUNTER-SPECIFIC on purpose: it is computed from the counter
    fire-rate only, so it claims exactly that — the engine with the materially LOWER
    counter fire-rate under-casts THIS DECK'S COUNTERS (not "interaction" broadly; an
    engine can under-cast counters while casting removal fine, so the removal column
    is left to speak for itself). Where the win-rates ALSO diverge, that under-casting
    engine's win-rate is the weaker read for a counter-reliant deck. Only warns when
    BOTH a counter-fire gap (:data:`_FALSE_READ_FIRE_GAP`) AND a win-rate gap
    (:data:`_FALSE_READ_WR_GAP`) are present; otherwise a neutral "comparable" line.
    Silent when either engine's counter piloting is unavailable (nothing to compare).
    """
    if len(results) != 2:
        return
    (name_a, a), (name_b, b) = results.items()
    fire_a, fire_b = _fire_rate(a.piloting, 'counter_fire'), _fire_rate(b.piloting, 'counter_fire')
    if fire_a is None or fire_b is None:
        return
    (low_name, low_fire, low_wr), (high_name, high_fire, _) = sorted(
        ((name_a, fire_a, a.win_rate), (name_b, fire_b, b.win_rate)),
        key=lambda t: t[1],
    )
    fire_gap = high_fire - low_fire
    win_rate_gap = abs(a.win_rate - b.win_rate)
    if fire_gap >= _FALSE_READ_FIRE_GAP and win_rate_gap >= _FALSE_READ_WR_GAP:
        print(
            f"  false-read: {low_name} casts this deck's counters {_pct(low_fire)} of the time vs "
            f'{high_name} {_pct(high_fire)} — {low_name} likely UNDER-CASTS its COUNTERS (its removal '
            f'fire-rate above may be fine), so its {_pct(low_wr)} win-rate is the weaker read where the '
            f"engines diverge ({_pct(win_rate_gap)} apart). Prefer {high_name}'s read for counter-reliant decks."
        )
    else:
        print("  false-read: engines' COUNTER piloting is comparable — the win-rates are consistent reads.")


def _gauntlet(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(
        prog='simulate gauntlet',
        description='Inspect a PACKAGED gauntlet field (offline — no Forge, no store, no network). '
        'Only `show` is supported.',
    )
    parser.add_argument('action', choices=('show',), help='Only `show` is supported.')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed', help=_FORMAT_HELP)
    parser.add_argument(
        '--source',
        default='curated',
        help="which PACKAGED gauntlet to list — 'curated' (default) or a named bundle shipped for "
        "the chosen --format (e.g. 'guilds' for constructed). 'mine'/'both' are NOT valid here "
        '(they need a live store). Default curated.',
    )
    args = parser.parse_args(argv)
    # `show` lists PACKAGED opponents only (no store, no Forge, no network), so
    # `mine`/`both` (which need a live store) are rejected here.
    if args.source in ('mine', 'both'):
        raise CollectionError(f'`gauntlet show` lists packaged decks only; {args.source!r} needs a live store.')
    decks = resolve_gauntlet(args.source, args.fmt)
    print(f'{args.source} gauntlet ({args.fmt}): {len(decks)} deck(s)')
    for deck in decks:
        print(f'  {deck.name}')


def _doctor(argv: list[str]) -> None:
    """Report every registered engine's resolvability + runtime pool + resource snapshot.

    Always prints the runtime-derived safe pool size + a free-RAM/disk snapshot
    (these need no engine). Then loops the registry: each engine reports available
    (version + capabilities + Forge install paths) or an ACTIONABLE "NOT AVAILABLE
    / how to enable" line — never a traceback. Exits non-zero ONLY when the DEFAULT
    engine is unavailable (the "is my sim usable?" signal); an opt-in engine like
    XMage being absent is informational. ``--provision`` fetches Forge on a miss.
    """
    parser = argparse.ArgumentParser(
        prog='simulate doctor',
        description='Report Forge/Java availability, the runtime-derived safe JVM pool size, and a '
        'free-RAM/disk snapshot. Read-only unless --provision is passed.',
    )
    parser.add_argument(
        '--provision',
        action='store_true',
        help='Download + cache Forge + a JRE now if not already available (one-time ~350MB).',
    )
    args = parser.parse_args(argv)

    pool = derive_pool_size()
    ram = free_ram_gib()
    disk = free_disk_gib()
    print('sim doctor')
    print(f'  derived pool size: {pool} concurrent JVM(s)')
    print(f'  free RAM:  {ram:.1f} GiB')
    print(f'  free disk: {disk:.1f} GiB')

    # Registry-driven: report EVERY registered engine's availability + version +
    # capabilities. Exit non-zero only when the DEFAULT engine is unavailable — that
    # is the "is my sim usable?" signal (the default is what runs without --engine,
    # and it auto-provisions). An OPT-IN engine like XMage (a manual local reactor)
    # being absent is INFORMATIONAL, not a failure, so it never fails the exit code.
    default_unavailable = False
    for name in available_engines():
        engine = get_engine(name)
        caps = engine.capabilities()
        try:
            # `--provision` now fetches ANY registered backend (Forge ~350MB, XMage
            # ~76MB shaded jar), not just the default — the "install everything" intent.
            provisionable = args.provision
            install: EngineInstall = _ensure_engine(engine) if provisionable else engine.resolve(provision=False)
        except EngineUnavailableError as exc:
            # Graceful: name WHY + HOW to enable, no traceback. Only the DEFAULT
            # engine's absence fails the exit code + gets the extra provision/env
            # how-to (it is the sole auto-provisionable backend this phase — a
            # hardcoded Forge block under every engine would misdirect XMage).
            if name == _DEFAULT_ENGINE:
                default_unavailable = True
            print(f'  {name}: NOT AVAILABLE')
            print(f'    {exc}', file=sys.stderr)
            if name == _DEFAULT_ENGINE:
                print(
                    f'    To enable: run `simulate doctor --provision` to auto-download Forge (~350MB, '
                    f'one-time), or set {ENV_FORGE_HOME} (+ {ENV_JAVA}) to reuse an existing install. '
                    f'(A `match`/`deck`/`ab` run also auto-provisions on first use.)',
                    file=sys.stderr,
                )
            continue

        print(f'  {name}: available' + ('  (provisioned)' if provisionable else ''))
        print(f'    version:      {install.version}')
        print(
            f'    capabilities: hand_visibility={caps.has_hand_visibility} '
            f'counter_metrics={caps.has_counter_metrics} '
            f'expected_nondecisive={caps.expected_nondecisive_rate:.0%}'
        )
        # Forge exposes its install paths (forge dir / jar / java) on the handle.
        forge_handle = install.handle
        if isinstance(forge_handle, ForgeInstall):
            print(f'    forge dir:    {forge_handle.forge_dir}')
            print(f'    jar:          {forge_handle.jar}')
            print(f'    java:         {forge_handle.java}')

    if default_unavailable:
        raise SystemExit(1)


def _log(argv: list[str]) -> None:
    """Retrieve a stored per-game verbose Forge log for forensic deep-diving.

    Reads the retained logs straight from DuckDB, keyed by the content hash of the
    deck ``.dck`` text. Without ``--game`` it lists the matching matchups (and,
    when a single matchup matches, its per-game index + outcome); with ``--game N``
    it prints that one game's full log. Re-running is NOT an option — Forge's seed
    is not reproducible — so this reads what was captured at run time.

    Offline vs live, by deck arg: a ``.dck`` PATH is read off disk (fully offline,
    and the exact text that was simulated). A bareword is treated as an Airtable
    NAME and resolved via a LIVE store lookup to the deck's CURRENT text — so if
    the deck was edited since the run, its hash no longer matches and the logs
    won't be found. For reliable forensics prefer the ``.dck`` path that was
    simulated.
    """
    from pipeline.sim.store import deck_hash, find_matchups, get_cached, get_game_logs

    parser = argparse.ArgumentParser(
        prog='simulate log',
        description='Retrieve a stored per-game verbose Forge log for a past matchup (offline, from DuckDB) — '
        'for forensic deep-diving. Without --game, lists matching matchups; with --game N, prints that game.',
    )
    parser.add_argument('deck_a', help='Deck A: a .dck path (offline) or an Airtable deck name (live lookup).')
    parser.add_argument('deck_b', help='Deck B: a .dck path (offline) or an Airtable deck name (live lookup).')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default=None, help='Narrow to a format.')
    parser.add_argument('--seed', type=int, default=None, help='Narrow to a specific run seed.')
    parser.add_argument('--games', type=int, default=None, dest='n_games', help='Narrow to a specific game count.')
    parser.add_argument('--forge', default=None, help='Narrow to a specific Forge version.')
    parser.add_argument('--game', type=int, default=None, help='Print this game index (0-based) full log.')
    parser.add_argument('--engine', choices=available_engines(), default=None, help='Narrow to a specific sim engine.')
    args = parser.parse_args(argv)

    dck_a = _resolve_deck_arg(args.deck_a).text
    dck_b = _resolve_deck_arg(args.deck_b).text
    rows = find_matchups(deck_a_hash=deck_hash(dck_a), deck_b_hash=deck_hash(dck_b), fmt=args.fmt)
    # Optional narrowing (engine / seed / game-count / engine-version) beyond the
    # store-level format filter — the levers a user pulls to disambiguate repeat
    # runs of a pair. ``--engine`` selects the backend that produced the run;
    # ``--forge`` narrows to a specific engine VERSION (its legacy name is kept).
    rows = [
        r
        for r in rows
        if (args.engine is None or r.engine == args.engine)
        and (args.seed is None or r.seed == args.seed)
        and (args.n_games is None or r.n_games == args.n_games)
        and (args.forge is None or r.forge_version == args.forge)
    ]
    if not rows:
        raise CollectionError(f'no stored matchup for {args.deck_a} vs {args.deck_b} (has it been simulated yet?)')

    if args.game is None:
        _print_matchup_index(rows, args.deck_a, args.deck_b, get_cached)
        return

    if len(rows) > 1:
        print(f'{len(rows)} matchups match — narrow with --seed / --games / --format / --forge:', file=sys.stderr)
        _print_matchup_rows(rows)
        raise SystemExit(1)

    logs = get_game_logs(rows[0].matchup_key, game_index=args.game)
    if not logs:
        # Only DECISIVE games have retained logs — clockout games are discarded at
        # store time (store.py), so ``n_games`` (the TOTAL) overstates what's
        # retrievable. Report the STORED count so the valid index range is honest.
        stored = len(get_game_logs(rows[0].matchup_key))
        raise CollectionError(
            f'no log for game {args.game} '
            f'({stored} retrievable game log(s) stored for this matchup, 0-based; '
            f'{rows[0].n_games} game(s) ran — clockout/non-decisive game logs are not retained)'
        )
    print(logs[0])


def _print_matchup_index(
    rows: list[MatchupRow],
    name_a: str,
    name_b: str,
    get_cached: Callable[[str], CachedMatchup | None],
) -> None:
    """List matching matchups; for a single match, enumerate its per-game outcomes."""
    if len(rows) > 1:
        print(
            f'{len(rows)} stored matchups for {name_a} vs {name_b} '
            '(pass --game N with --seed/--games/--forge to read one):'
        )
        _print_matchup_rows(rows)
        return
    row = rows[0]
    print(f'{name_a} vs {name_b}  ({row.format}, seed={row.seed}, {row.n_games} games, forge {row.forge_version})')
    print(f'  record: {row.wins_a}-{row.wins_b}-{row.draws}   ran: {row.created_at}')
    cached = get_cached(row.matchup_key)
    features = cached.features if cached is not None else []
    # Only DECISIVE games have RETAINED logs (clockout logs are discarded at store
    # time), so the retrievable count — ``len(features)`` — is what ``--game N``
    # can address, NOT ``n_games`` (the total that RAN). Name it so a forensic
    # index into a run with clockouts doesn't invite out-of-range indices.
    print(f'  {len(features)} of {row.n_games} game(s) retrievable (clockout/non-decisive logs not retained):')
    for i, feat in enumerate(features):
        kt = '-' if feat.kill_turn is None else feat.kill_turn
        wc = feat.wincon or '-'
        print(f'    [{i}] winner={feat.winner:<4} kill_turn={kt:<3} wincon={wc}')


def _print_matchup_rows(rows: list[MatchupRow]) -> None:
    """One line per matchup (key prefix / seed / games / format / version / record / when).

    The 8-char ``matchup_key`` prefix is the last-resort disambiguator: two runs
    of the same pair differing ONLY by Forge version share seed/games/format, so
    the key prefix (and ``--forge``) are what tell them apart.
    """
    for r in rows:
        print(
            f'  key={r.matchup_key[:8]} seed={r.seed} games={r.n_games} format={r.format} '
            f'forge={r.forge_version} record={r.wins_a}-{r.wins_b}-{r.draws} ran={r.created_at}'
        )


VERBS = {
    'match': _match,
    'deck': _deck,
    'ab': _ab,
    'gauntlet': _gauntlet,
    'log': _log,
    'doctor': _doctor,
}

#: One-line summary per verb for the top-level usage/`--help`.
_VERB_SUMMARIES = {
    'deck': 'evaluate a deck vs a gauntlet → win-rate ± CI + telemetry',
    'ab': 'A/B two variants over the same gauntlet',
    'match': 'a single head-to-head win tally (no gauntlet)',
    'gauntlet': 'inspect a packaged gauntlet field (offline)',
    'log': 'retrieve a stored per-game Forge log (offline forensics)',
    'doctor': 'check Forge/Java availability + safe pool size',
}


def _usage() -> str:
    """The top-level usage block listing every verb + a one-line summary."""
    lines = ["usage: simulate <verb> [args...]   (run `simulate <verb> -h` for a verb's flags)", '', 'verbs:']
    lines += [f'  {verb:<9} {_VERB_SUMMARIES.get(verb, "")}' for verb in VERBS]
    return '\n'.join(lines)


def main(argv: list[str] | None = None) -> None:
    """Dispatch ``argv[0]`` to the matching verb handler.

    Explicit ``-h``/``--help`` -> the top-level usage (verb list) on stdout, exit 0.
    No verb or an unknown verb -> usage on stderr + ``SystemExit(2)``. Expected,
    user-facing failures (Forge unavailable/failed, unknown deck, bad gauntlet
    source, missing creds) surface as a clean one-line ``error:`` + exit 1 — a
    genuine defect still tracebacks (mirrors ``collection/run.py``). ``SystemExit``
    (argparse, the doctor guard) passes through untouched.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ('-h', '--help'):
        print(_usage())
        return
    if not args or args[0] not in VERBS:
        print(_usage(), file=sys.stderr)
        raise SystemExit(2)
    verb = args[0]
    try:
        VERBS[verb](args[1:])
    except (
        EngineUnavailableError,
        ForgeUnavailableError,
        ForgeError,
        CollectionError,
        FileNotFoundError,
        ValueError,
        # get_engine() raises a self-explaining KeyError for an unknown/empty
        # registry (e.g. a packaging failure where no engine registered on import).
        # argparse normally constrains --engine to the live choices, so this is a
        # last-resort clean message rather than a raw traceback.
        KeyError,
    ) as exc:
        print(f'error: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
