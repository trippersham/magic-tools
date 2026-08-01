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
    forge_version,
    simulate,
)
from pipeline.sim.forge_runtime import (
    ENV_FORGE_HOME,
    ENV_JAVA,
    FORGE_VERSION,
    ForgeInstall,
    ForgeUnavailableError,
    ensure,
    resolve,
)
from pipeline.sim.gauntlet import gauntlet_sources, resolve_gauntlet
from pipeline.sim.governor import derive_pool_size, free_disk_gib, free_ram_gib
from pipeline.sim.runner import ForgeError, MatchResult, run_matchup

if TYPE_CHECKING:
    from collections.abc import Callable

    from pipeline.sim.store import CachedMatchup, MatchupRow

__all__ = ('main',)

#: Default per-opponent / per-matchup games when the caller doesn't pass one.
_DEFAULT_GAMES = 4
#: Default RNG seed (Forge's ``-s`` is not reliably reproducible; see core).
_DEFAULT_SEED = 42
#: Format choices exposed on the CLI.
_FORMAT_CHOICES = ('constructed', 'commander')
#: Gauntlet sources exposed on the CLI: the core sources + every named bundle
#: shipped for any format (union), so ``--gauntlet <bundle>`` is accepted
#: regardless of ``--format`` arg order. ``resolve_gauntlet`` still validates the
#: (source, format) pairing at run time (a bundle only shipped for constructed
#: raises for commander).
_GAUNTLET_CHOICES = tuple(dict.fromkeys(src for fmt in _FORMAT_CHOICES for src in gauntlet_sources(fmt)))


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


def _forge_fetch_notice() -> None:
    """One-time 'downloading Forge' notice (stderr) so a first run isn't a silent hang."""
    print(
        f'Forge not found locally — downloading Forge {FORGE_VERSION} + a JRE '
        '(~350MB, one-time; reused after). This may take a few minutes…',
        file=sys.stderr,
    )


def _ensure_forge() -> ForgeInstall:
    """Resolve Forge for a game verb, AUTO-PROVISIONING (fetch) on first use.

    Game verbs (``match`` / ``deck`` / ``ab``) call this instead of the read-only
    :func:`~pipeline.sim.forge_runtime.resolve` so a fresh box provisions Forge
    itself on the first run (the design's fetch-at-runtime promise), surfacing a
    one-time notice before the download. An impossible fetch (offline) still
    raises ``ForgeUnavailableError`` → the ``main`` handler prints a clean error.
    """
    return ensure(on_fetch=_forge_fetch_notice)


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


def _dck_card_names(dck_text: str) -> list[str]:
    """The card names referenced by a rendered ``.dck`` ([Main] + [Commander]).

    Parses ``<qty> <name>`` lines under the card sections, skipping headers,
    metadata, and the sideboard — so the availability guard can check exactly the
    names Forge will try to load. Accepts REAL Forge ``.dck`` files, not just our
    exporter's output: section headers are matched case-insensitively (Forge
    writes ``[main]``) and a pinned printing (``<name>|SET`` or ``<name>|SET|art``)
    is stripped to the bare name (the index knows names, not printings).
    """
    names: list[str] = []
    in_cards = False
    for line in dck_text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith('['):
            in_cards = stripped.lower() in ('[main]', '[commander]')
            continue
        if not in_cards:
            continue
        qty, _, name = stripped.partition(' ')
        name = name.split('|', 1)[0].strip()
        if qty.isdigit() and name:
            names.append(name)
    return names


def _deck_from_dck(name: str, dck_text: str) -> Deck:
    """Reconstruct a minimal :class:`Deck` from rendered ``.dck`` text (for a path arg).

    Only the card NAMES are recoverable from a raw ``.dck`` (no ``oracle_id``s), so
    every card is name-only — that is exactly why the guard suppresses the
    resulting ``UNRESOLVED`` warnings for path decks and acts only on the
    (real) ``ABSENT_FROM_TARGET`` findings.
    """
    cards = [DeckCard(name=cn) for cn in _dck_card_names(dck_text)]
    return Deck(name=name, cards=cards)


def _guard_forge_availability(
    install: ForgeInstall,
    decks: list[_ResolvedDeck],
    *,
    allow_missing: bool,
) -> None:
    """Fail BEFORE spawning a JVM if a deck references a card Forge cannot load.

    Routes through the DESTINATION's own validation — ``ForgeDckExporter.validate``
    backed by a :class:`~pipeline.sim.forge_card_index.ForgeCardIndex` — so the
    card-availability classification lives in ONE place (the forge_dck card
    exporter), not re-implemented here. For an Airtable deck the hydrated
    :class:`Deck` is validated directly; for a ``.dck`` path it is reconstructed
    from the rendered names (:func:`_deck_from_dck`). A card ABSENT from Forge's DB
    is a BLOCKING :class:`DeckExportError` (naming the offenders) — unless
    ``allow_missing``, which downgrades it to a stderr warning. ``UNRESOLVED``
    (name-only) cards are surfaced as warnings ONLY for store-resolved decks (a raw
    ``.dck`` legitimately carries no ``oracle_id``s). If the index can't be built
    (a minimal install without ``cardsfolder.zip``), the guard is skipped — Forge's
    own loader remains the backstop.
    """
    from pipeline.sim.forge_card_index import ForgeCardIndex

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
    parser = argparse.ArgumentParser(prog='simulate match')
    parser.add_argument('deck_a', help='Deck A: a .dck path or an Airtable deck name.')
    parser.add_argument('deck_b', help='Deck B: a .dck path or an Airtable deck name.')
    parser.add_argument('-n', type=int, default=_DEFAULT_GAMES, help=f'games (default {_DEFAULT_GAMES}).')
    parser.add_argument('-s', '--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if a deck references a card absent from Forge (else a hard error).',
    )
    args = parser.parse_args(argv)

    deck_a = _resolve_deck_arg(args.deck_a)
    deck_b = _resolve_deck_arg(args.deck_b)
    install = _ensure_forge()
    _guard_forge_availability(install, [deck_a, deck_b], allow_missing=args.allow_missing)
    result: MatchResult = run_matchup(install, deck_a.ref, deck_b.ref, n=args.n, seed=args.seed, fmt=args.fmt)
    print(f'{deck_a.name} vs {deck_b.name}  ({args.fmt}, n={args.n}, seed={args.seed})')
    print(f'{deck_a.name}: {result.wins_a} wins   {deck_b.name}: {result.wins_b} wins   draws: {result.draws}')


def _deck(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(prog='simulate deck')
    parser.add_argument('name', help='Candidate deck: a .dck path or an Airtable deck name.')
    parser.add_argument('--gauntlet', choices=_GAUNTLET_CHOICES, default='curated')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default='constructed')
    parser.add_argument('--games', type=int, default=_DEFAULT_GAMES, help=f'games/opponent (default {_DEFAULT_GAMES}).')
    parser.add_argument('--seed', type=int, default=_DEFAULT_SEED, help=f'RNG seed (default {_DEFAULT_SEED}).')
    parser.add_argument('--force', action='store_true', help='Bypass the matchup cache (re-run every matchup).')
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if the candidate references a card absent from Forge (else a hard error).',
    )
    args = parser.parse_args(argv)

    candidate = _resolve_deck_arg(args.name)
    # `mine`/`both` need the collection store; `curated` never touches it.
    store = get_store() if args.gauntlet in ('mine', 'both') else None
    install = _ensure_forge()
    _guard_forge_availability(install, [candidate], allow_missing=args.allow_missing)
    result = simulate(
        candidate.ref,
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
    parser.add_argument(
        '--allow-missing',
        action='store_true',
        help='Proceed even if a variant references a card absent from Forge (else a hard error).',
    )
    args = parser.parse_args(argv)

    variant_a = _resolve_deck_arg(args.deck_a)
    variant_b = _resolve_deck_arg(args.deck_b)
    store = get_store() if args.gauntlet in ('mine', 'both') else None
    install = _ensure_forge()
    _guard_forge_availability(install, [variant_a, variant_b], allow_missing=args.allow_missing)
    comparison: Comparison = compare(
        variant_a.ref,
        variant_b.ref,
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
    parser.add_argument(
        '--source',
        default='curated',
        help='Which packaged gauntlet to list: `curated` (default) or a named bundle '
        f'(shipped: {", ".join(gauntlet_sources("constructed"))}).',
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
    """Report Forge/Java resolvability + runtime pool sizing + resource snapshot.

    Always prints the runtime-derived safe pool size + a free-RAM/disk snapshot
    (these need no Forge). Then attempts :func:`resolve`; on success prints the
    resolved paths + Forge version and exits 0. On :class:`ForgeUnavailableError`
    it prints an ACTIONABLE "not available / how to enable" message and exits
    non-zero — never a traceback.
    """
    parser = argparse.ArgumentParser(prog='simulate doctor')
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

    try:
        # `--provision` fetches on a miss (the one-time download); otherwise the
        # check is read-only (`resolve`), so `doctor` never surprises with a pull.
        install: ForgeInstall = _ensure_forge() if args.provision else resolve()
    except ForgeUnavailableError as exc:
        # Graceful: name WHY + HOW to enable, exit non-zero, no traceback.
        print('  forge: NOT AVAILABLE')
        print(f'    {exc}')
        print(
            f'    To enable: run `simulate doctor --provision` to auto-download Forge (~350MB, '
            f'one-time), or set {ENV_FORGE_HOME} (+ {ENV_JAVA}) to reuse an existing install. '
            f'(A `match`/`deck`/`ab` run also auto-provisions on first use.)',
            file=sys.stderr,
        )
        raise SystemExit(1) from exc

    print('  forge: available' + ('  (provisioned)' if args.provision else ''))
    print(f'    version:   {forge_version()}')
    print(f'    forge dir: {install.forge_dir}')
    print(f'    jar:       {install.jar}')
    print(f'    java:      {install.java}')


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

    parser = argparse.ArgumentParser(prog='simulate log')
    parser.add_argument('deck_a', help='Deck A: a .dck path (offline) or an Airtable deck name (live lookup).')
    parser.add_argument('deck_b', help='Deck B: a .dck path (offline) or an Airtable deck name (live lookup).')
    parser.add_argument('--format', dest='fmt', choices=_FORMAT_CHOICES, default=None, help='Narrow to a format.')
    parser.add_argument('--seed', type=int, default=None, help='Narrow to a specific run seed.')
    parser.add_argument('--games', type=int, default=None, dest='n_games', help='Narrow to a specific game count.')
    parser.add_argument('--forge', default=None, help='Narrow to a specific Forge version.')
    parser.add_argument('--game', type=int, default=None, help='Print this game index (0-based) full log.')
    args = parser.parse_args(argv)

    dck_a = _resolve_deck_arg(args.deck_a).text
    dck_b = _resolve_deck_arg(args.deck_b).text
    rows = find_matchups(deck_a_hash=deck_hash(dck_a), deck_b_hash=deck_hash(dck_b), fmt=args.fmt)
    # Optional narrowing (seed / game-count / forge-version) beyond the store-level
    # format filter — the levers a user pulls to disambiguate repeat runs of a pair.
    rows = [
        r
        for r in rows
        if (args.seed is None or r.seed == args.seed)
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
        raise CollectionError(f'no log for game {args.game} (matchup has {rows[0].n_games} game(s), 0-based)')
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
    print('  games (pass --game <index> for the full log):')
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
