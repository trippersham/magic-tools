"""Nudge-magnitude sweep harness (experiment: is the opportunistic band real or empty?).

Walks the single Φ magnitude axis alpha ∈ {0=thin, small, medium, large≈dedicated} for a set of
decks spanning the combo-centrality spectrum, measuring per cell:

* COST         — wall seconds/game (driven vs driverless).
* FIRE-RATE    — fraction of solo games emitting ``MACRO_FIRE_REAL`` (combo assembled + fired).
* PLAY QUALITY — (a) solo own-turn kill clock (driven vs driverless: the gate's regression proxy);
                 (b) DEFENDED win-rate vs a fixed CP7 field (the real regresses-play detector).

Each solo cell runs one goldfish batch (one JVM) of ``n`` games; cells run concurrently under a
bounded thread pool (each blocks on its own JVM subprocess). Every completed cell is appended to a
JSONL ledger immediately, so a kill loses at most the in-flight cells and a rerun skips finished ones.

This is EXPERIMENT harness code (committed to the experiment branch), not a product entrypoint.

Usage (from plugins/make-magic/pipeline, with the sweep env exported):
    uv run python -m pipeline.sim.nudge_sweep solo    --n 80 --workers 5 --ledger <path.jsonl>
    uv run python -m pipeline.sim.nudge_sweep defended --n 12 --workers 4 --ledger <path.jsonl>
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from pipeline.contracts.models import Deck, DeckCard
from pipeline.sim import driver_gate, drivers
from pipeline.sim.core import wilson_ci
from pipeline.sim.driver_batch import _driver_uuid, _parse_dck
from pipeline.sim.driver_compare import _sequential_run_matchups, compare_gauntlet
from pipeline.sim.engine import get_engine
from pipeline.transforms.combo_detect import load_combos, win_combos_in_deck

# The 3 decks across the combo-centrality spectrum (see research/nudge-magnitude-sweep.md Step 2).
_GAUNTLET = Path(__file__).resolve().parents[2] / 'pipeline' / 'data' / 'gauntlet' / 'commander'
DECKS: dict[str, str] = {
    'heliod': 'mid/mono-white__heliod-sun-crowned.dck',        # HIGH: combo IS the plan
    'marchesa': 'casual/grixis__marchesa-the-black-rose.dck',  # MID: board-combo + real grind plan
    'ghave': 'mid/abzan__ghave-guru-of-spores.dck',            # LOW: fringe combo, go-wide grind
}

# The single magnitude axis. 0 == thin (Φ=0 + macro); 40000 == the dedicated Φ per-piece scale.
ALPHAS: tuple[int, ...] = (0, 2000, 12000, 40000)

# A small FIXED CP7 field for the defended lens (mid-power, varied colors; NOT the sweep decks).
DEFENDED_OPPONENTS: tuple[str, ...] = (
    # Stratified 6-deck power field (aggro / tempo / control-stax / combo-control / counters /
    # aggro-combo) — replaces the original 2-deck field whose bare-CP7 arm swung ~0.42-0.63 at
    # n=24/cell (a ~±0.2 noise floor). 6 opponents x n>=15 -> ~90 decided games/piloting/cell.
    'mid/gruul__xenagos.dck',
    'mid/izzet__niv-mizzet-parun.dck',
    'mid/dimir__yuriko-the-tiger-s-shadow.dck',
    'mid/abzan__anafenza-the-foremost.dck',
    'mid/azorius__grand-arbiter-augustin-iv.dck',
    'mid/boros__winota-joiner-of-forces.dck',
)

# Per-game parse of the harness's own solo line.
_GAME_RE = re.compile(
    r'GOLDFISH GAME \d+/\d+ .*?killed=(?P<killed>true|false) '
    r'ownKillTurn=(?P<own>\d+|NONE) globalTurn=(?P<global>\d+) .*?ms=(?P<ms>\d+)'
)

_LEDGER_LOCK = threading.Lock()


@dataclass(frozen=True)
class DeckCtx:
    key: str
    deck_ref: tuple[str, str]
    combo_names: tuple[str, ...]
    fmt: str
    dck_text: str

    def deck_for_alpha(self, alpha: int | None) -> Deck:
        # Distinct uuid per cell so each driver compiles/installs into its own classes dir.
        tag = 'driverless' if alpha is None else f'a{alpha}'
        base = _driver_uuid(f'commander/nudge/{self.key}.dck')
        return Deck(
            name=f'{self.key}-{tag}',
            uuid=f'{base}{tag}',
            cards=[DeckCard(name=n, quantity=1, role='commander') for n in _commanders(self.dck_text)],
        )


def _commanders(dck_text: str) -> tuple[str, ...]:
    _all, cmd = _parse_dck(dck_text)
    return cmd


def _load_ctx(key: str) -> DeckCtx:
    text = (_GAUNTLET / DECKS[key]).read_text(encoding='utf-8')
    names, _cmd = _parse_dck(text)
    combos = win_combos_in_deck(set(names), load_combos())
    if not combos:
        raise SystemExit(f'{key}: no win combo detected')
    combo = combos[0]
    return DeckCtx(
        key=key,
        deck_ref=(key, text),
        combo_names=combo.card_names,
        fmt='commander' if _commanders(text) else 'constructed',
        dck_text=text,
    )


def _install_driver(ctx: DeckCtx, alpha: int) -> tuple[str, str]:
    """Author + compile + publish + stamp meta for one (deck, alpha). Returns (classes_dir, fqcn)."""
    from pipeline.sim.driver_authoring import seed_nudge_quad
    from pipeline.transforms.combo_detect import Combo

    combo = win_combos_in_deck(set(_parse_dck(ctx.dck_text)[0]), load_combos())[0]
    assert isinstance(combo, Combo)
    spec = seed_nudge_quad(combo, alpha=alpha)
    deck = ctx.deck_for_alpha(alpha)
    classes_dir, fqcn = driver_gate.compile_quad_driver(deck, spec, data_dir=None)
    drivers.write_meta(
        deck,
        drivers.DriverMeta(
            deck_version=drivers.version(deck),
            harness_version=drivers.harness_version(data_dir=None),
            fqcn=fqcn,
            gates_passed=True,
            gate_mode='skipped',
            extra={'gate': 'skipped-for-nudge-sweep', 'alpha': alpha},
        ),
        data_dir=None,
    )
    return classes_dir, fqcn


def _append_ledger(ledger: Path, row: dict) -> None:
    with _LEDGER_LOCK, ledger.open('a', encoding='utf-8') as fh:
        fh.write(json.dumps(row) + '\n')


def _done_cells(ledger: Path) -> set[str]:
    if not ledger.exists():
        return set()
    done = set()
    for line in ledger.read_text(encoding='utf-8').splitlines():
        if line.strip():
            done.add(json.loads(line)['cell'])
    return done


def _parse_solo(output: str) -> dict:
    kills, bricks, ms_list, own_turns = 0, 0, [], []
    for m in _GAME_RE.finditer(output):
        ms_list.append(int(m.group('ms')))
        if m.group('killed') == 'true':
            kills += 1
            own_turns.append(int(m.group('own')))
        else:
            bricks += 1
    return {
        'games_parsed': kills + bricks,
        'kills': kills,
        'bricks': bricks,
        'own_turns': sorted(own_turns),
        'ms': ms_list,
        'fire_count': output.count('MACRO_FIRE_REAL'),
        'reachable_count': output.count('DRIVER_MACRO_FIRED'),
        'registered': 'DRIVER_REGISTERED' in output,
    }


def _run_solo_cell(ctx: DeckCtx, alpha: int | None, n: int, engine, install, ledger: Path) -> str:
    cell = f'solo:{ctx.key}:{"driverless" if alpha is None else alpha}'
    driver = None if alpha is None else _INSTALLED[(ctx.key, alpha)]
    t0 = time.time()
    gf, output = engine.goldfish_output(ctx.deck_ref, games=n, install=install, driver=driver, fmt=ctx.fmt)
    wall = time.time() - t0
    parsed = _parse_solo(output)
    g = parsed['games_parsed'] or n
    fire_lo, fire_hi = wilson_ci(parsed['fire_count'], g)
    row = {
        'cell': cell,
        'mode': 'solo',
        'deck': ctx.key,
        'alpha': alpha,
        'n': n,
        'median_kills_own': gf.median_kills_own,
        'max_turn': gf.max_turn,
        'wall_s_total': round(wall, 1),
        'mean_ms_per_game': round(sum(parsed['ms']) / len(parsed['ms']), 0) if parsed['ms'] else None,
        'fire_rate': round(parsed['fire_count'] / g, 4),
        'fire_ci': [round(fire_lo, 4), round(fire_hi, 4)],
        **parsed,
    }
    _append_ledger(ledger, row)
    print(f'[done] {cell}  fire={row["fire_rate"]:.2f}{row["fire_ci"]}  '
          f'medKillOwn={gf.median_kills_own}  meanMs={row["mean_ms_per_game"]}', flush=True)
    return cell


def _run_defended_cell(ctx: DeckCtx, alpha: int, n: int, engine, install, ledger: Path) -> str:
    cell = f'defended:{ctx.key}:{alpha}'
    driver = _INSTALLED[(ctx.key, alpha)]
    opponents = [(p, (_GAUNTLET / p).read_text(encoding='utf-8')) for p in DEFENDED_OPPONENTS]
    t0 = time.time()
    gc = compare_gauntlet(
        ctx.deck_ref, driver, opponents,
        install=install, games=n, seed=42, fmt=ctx.fmt, engine=engine,
        run_matchups=_sequential_run_matchups,
    )
    wall = time.time() - t0
    row = {
        'cell': cell,
        'mode': 'defended',
        'deck': ctx.key,
        'alpha': alpha,
        'n_per_opp': n,
        'n_opponents': len(opponents),
        'winrate_driver': gc.winrate_driver,
        'winrate_driver_ci': list(gc.winrate_driver_ci),
        'winrate_cp7': gc.winrate_cp7,
        'winrate_cp7_ci': list(gc.winrate_cp7_ci),
        'winrate_delta': gc.winrate_delta,
        'wall_s_total': round(wall, 1),
    }
    _append_ledger(ledger, row)
    print(f'[done] {cell}  wr_driver={gc.winrate_driver:.2f}{list(gc.winrate_driver_ci)}  '
          f'wr_cp7={gc.winrate_cp7:.2f}  wall={wall:.0f}s', flush=True)
    return cell


# Populated during install; shared read-only by the worker threads.
_INSTALLED: dict[tuple[str, int], tuple[str, str]] = {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Nudge-magnitude sweep')
    ap.add_argument('mode', choices=['solo', 'defended'])
    ap.add_argument('--decks', default=','.join(DECKS), help='comma list of deck keys')
    ap.add_argument('--alphas', default=','.join(map(str, ALPHAS)), help='comma list of alpha values')
    ap.add_argument('--n', type=int, required=True, help='games per cell (per opponent for defended)')
    ap.add_argument('--workers', type=int, default=5)
    ap.add_argument('--ledger', required=True, type=Path)
    args = ap.parse_args(argv)

    deck_keys = [d.strip() for d in args.decks.split(',') if d.strip()]
    alphas = [int(a) for a in args.alphas.split(',') if a.strip() != '']
    args.ledger.parent.mkdir(parents=True, exist_ok=True)

    engine = get_engine('xmage')
    install = engine.resolve(provision=False)
    ctxs = {k: _load_ctx(k) for k in deck_keys}

    # Compile + install every driver SERIALLY first (cheap; avoids concurrent-compile races).
    print('== installing drivers ==', flush=True)
    for k in deck_keys:
        for a in alphas:
            _INSTALLED[(k, a)] = _install_driver(ctxs[k], a)
            print(f'  installed {k} alpha={a}', flush=True)

    done = _done_cells(args.ledger)

    # Build the work list.
    tasks = []
    for k in deck_keys:
        if args.mode == 'solo':
            for cell_alpha in [None, *alphas]:
                cell = f'solo:{k}:{"driverless" if cell_alpha is None else cell_alpha}'
                if cell not in done:
                    tasks.append((ctxs[k], cell_alpha))
        else:
            for a in alphas:
                cell = f'defended:{k}:{a}'
                if cell not in done:
                    tasks.append((ctxs[k], a))

    print(f'== running {len(tasks)} {args.mode} cells ({len(done)} already done) '
          f'workers={args.workers} n={args.n} ==', flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = []
        for ctx, a in tasks:
            if args.mode == 'solo':
                futs.append(pool.submit(_run_solo_cell, ctx, a, args.n, engine, install, args.ledger))
            else:
                futs.append(pool.submit(_run_defended_cell, ctx, a, args.n, engine, install, args.ledger))
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as exc:  # ledger the failure, keep the sweep alive
                print(f'[ERROR] cell failed: {exc!r}', flush=True)

    print('== sweep complete ==', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
