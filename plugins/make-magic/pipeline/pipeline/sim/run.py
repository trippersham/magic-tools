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
  * ``gauntlet show`` — list the curated gauntlet decks (offline, no Forge).
  * ``doctor`` — Forge/Java resolution + version + derived pool size + a
    free-RAM/disk snapshot; graceful whether or not Forge is present.

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

from pipeline.collection import CollectionError, get_store
from pipeline.destinations.deck_export import get_exporter
from pipeline.sim.core import (
    Comparison,
    SimResult,
    compare,
    forge_version,
    simulate,
)
from pipeline.sim.forge_runtime import (
    ENV_FORGE_HOME,
    ENV_JAVA,
    ForgeInstall,
    ForgeUnavailableError,
    resolve,
)
from pipeline.sim.gauntlet import resolve_gauntlet
from pipeline.sim.governor import derive_pool_size, free_disk_gib, free_ram_gib
from pipeline.sim.runner import ForgeError, MatchResult, run_matchup

__all__ = ('main',)

#: Default per-opponent / per-matchup games when the caller doesn't pass one.
_DEFAULT_GAMES = 4
#: Default RNG seed (Forge's ``-s`` is not reliably reproducible; see core).
_DEFAULT_SEED = 42
#: Gauntlet sources exposed on the CLI (mirrors ``gauntlet._SOURCES``).
_GAUNTLET_CHOICES = ('curated', 'mine', 'both')
#: Format choices exposed on the CLI.
_FORMAT_CHOICES = ('constructed', 'commander')


# --------------------------------------------------------------------------- #
# Deck-arg resolution.
# --------------------------------------------------------------------------- #


def _resolve_deck_arg(arg: str) -> tuple[str, str]:
    """Resolve a deck reference to a ``(name, dck_text)`` pair.

    A ``.dck`` path (arg ends in ``.dck`` or is an existing file) is read
    straight off disk — its stem is the deck name. Anything else is treated as an
    Airtable deck NAME: resolved via ``get_store().get_deck`` and rendered with
    the Forge ``.dck`` exporter. This is the single resolver both the deck-ref
    verbs (``match`` / ``deck`` / ``ab``) route through.
    """
    from pathlib import Path

    path = Path(arg)
    if arg.endswith('.dck') or path.is_file():
        if not path.is_file():
            raise CollectionError(f'deck file not found: {arg}')
        return (path.stem, path.read_text())

    deck = get_store().get_deck(arg)
    return (deck.name, get_exporter('forge_dck').export(deck))


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
    print(
        f'matchups: {len(result.per_opponent)}  '
        f'(cached {result.cached_matchups}, fresh {result.fresh_matchups})'
    )
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


# --------------------------------------------------------------------------- #
# Verbs.
# --------------------------------------------------------------------------- #


def _match(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='simulate match')
    parser.add_argument('deck_a', help='Deck A: a .dck path or an Airtable deck name.')
    parser.add_argument('deck_b', help='Deck B: a .dck path or an Airtable deck name.')
    parser.add_argument('-n', type=int, default=_DEFAULT_GAMES, help=f'games (default {_DEFAULT_GAMES}).')
    parser.add_argument('-s', '--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    args = parser.parse_args(argv)

    deck_a = _resolve_deck_arg(args.deck_a)
    deck_b = _resolve_deck_arg(args.deck_b)
    install = resolve()
    result: MatchResult = run_matchup(
        install, deck_a, deck_b, n=args.n, seed=args.seed, fmt=args.fmt
    )
    print(f'{deck_a[0]} vs {deck_b[0]}  ({args.fmt}, n={args.n}, seed={args.seed})')
    print(
        f'{deck_a[0]}: {result.wins_a} wins   '
        f'{deck_b[0]}: {result.wins_b} wins   '
        f'draws: {result.draws}'
    )


def _deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='simulate deck')
    parser.add_argument('name', help='Candidate deck: a .dck path or an Airtable deck name.')
    parser.add_argument('--gauntlet', choices=_GAUNTLET_CHOICES, default='curated')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    parser.add_argument('--games', type=int, default=_DEFAULT_GAMES, help=f'games/opponent (default {_DEFAULT_GAMES}).')
    parser.add_argument('--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--force', action='store_true', help='Bypass the matchup cache (re-run every matchup).')
    args = parser.parse_args(argv)

    candidate = _resolve_deck_arg(args.name)
    # `mine`/`both` need the collection store; `curated` never touches it.
    store = get_store() if args.gauntlet in ('mine', 'both') else None
    install = resolve()
    result = simulate(
        candidate,
        args.gauntlet,
        games=args.games,
        fmt=args.fmt,
        seed=args.seed,
        install=install,
        force=args.force,
        store=store,
    )
    _print_sim_result(result)


def _ab(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='simulate ab')
    parser.add_argument('deck_a', help='Variant A: a .dck path or an Airtable deck name.')
    parser.add_argument('deck_b', help='Variant B: a .dck path or an Airtable deck name.')
    parser.add_argument('--gauntlet', choices=_GAUNTLET_CHOICES, default='curated')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    parser.add_argument('--games', type=int, default=_DEFAULT_GAMES, help=f'games/opponent (default {_DEFAULT_GAMES}).')
    parser.add_argument('--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--force', action='store_true', help='Bypass the matchup cache (re-run every matchup).')
    args = parser.parse_args(argv)

    variant_a = _resolve_deck_arg(args.deck_a)
    variant_b = _resolve_deck_arg(args.deck_b)
    store = get_store() if args.gauntlet in ('mine', 'both') else None
    install = resolve()
    comparison: Comparison = compare(
        variant_a,
        variant_b,
        args.gauntlet,
        games=args.games,
        fmt=args.fmt,
        seed=args.seed,
        install=install,
        force=args.force,
        store=store,
    )
    _print_comparison(comparison)


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


def _fmt_signed(value: float | None) -> str:
    """Format a signed metric delta ('-' when None)."""
    return '-' if value is None else f'{value:+.2f}'


def _gauntlet(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='simulate gauntlet')
    parser.add_argument('action', choices=('show',), help='Only `show` is supported.')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    args = parser.parse_args(argv)
    # `show` lists the CURATED opponents only (no store, no Forge, no network).
    decks = resolve_gauntlet('curated', args.fmt)
    print(f'curated gauntlet ({args.fmt}): {len(decks)} deck(s)')
    for deck in decks:
        print(f'  {deck.name}')


def _doctor(argv: list[str]) -> None:
    """Report Forge/Java resolvability + runtime pool sizing + resource snapshot.

    Always prints the runtime-derived safe pool size + a free-RAM/disk snapshot
    (these need no Forge). Then attempts :func:`resolve`; on success prints the
    resolved paths + Forge version and exits 0. On :class:`ForgeUnavailableError`
    it prints an ACTIONABLE "not available / how to enable" message and exits
    non-zero — never a traceback.
    """
    parser = argparse.ArgumentParser(prog='simulate doctor')
    parser.parse_args(argv)

    pool = derive_pool_size()
    ram = free_ram_gib()
    disk = free_disk_gib()
    print('sim doctor')
    print(f'  derived pool size: {pool} concurrent JVM(s)')
    print(f'  free RAM:  {ram:.1f} GiB')
    print(f'  free disk: {disk:.1f} GiB')

    try:
        install: ForgeInstall = resolve()
    except ForgeUnavailableError as exc:
        # Graceful: name WHY + HOW to enable, exit non-zero, no traceback.
        print('  forge: NOT AVAILABLE')
        print(f'    {exc}')
        print(
            f'    To enable: set {ENV_FORGE_HOME} to a Forge home (with the desktop jar + res/) '
            f'and {ENV_JAVA} to a java binary, or provision the cached install.',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    print('  forge: available')
    print(f'    version:   {forge_version()}')
    print(f'    forge dir: {install.forge_dir}')
    print(f'    jar:       {install.jar}')
    print(f'    java:      {install.java}')


VERBS = {
    'match': _match,
    'deck': _deck,
    'ab': _ab,
    'gauntlet': _gauntlet,
    'doctor': _doctor,
}


def main(argv: list[str] | None = None) -> None:
    """Dispatch ``argv[0]`` to the matching verb handler.

    Unknown/absent verb -> usage on stderr + ``SystemExit(2)``. Expected,
    user-facing failures (Forge unavailable/failed, unknown deck, bad gauntlet
    source, missing creds) surface as a clean one-line ``error:`` + exit 1 — a
    genuine defect still tracebacks (mirrors ``collection/run.py``). ``SystemExit``
    (argparse, the doctor guard) passes through untouched.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in VERBS:
        avail = ', '.join(sorted(VERBS))
        print(
            f'usage: simulate <verb> [args...]\n  verbs: {avail}',
            file=sys.stderr,
        )
        raise SystemExit(2)
    verb = args[0]
    try:
        VERBS[verb](args[1:])
    except (ForgeUnavailableError, ForgeError, CollectionError, FileNotFoundError, ValueError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == '__main__':
    main()
