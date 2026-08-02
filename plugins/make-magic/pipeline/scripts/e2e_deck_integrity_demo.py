#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",  # the collection CLI + local backend under test
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# [tool.uv.sources]
# make-magic-pipeline = { path = "..", editable = true }
# ///
"""Runnable behavioral DEMO of the deck-integrity prevent -> detect -> recover loop.

Drives the REAL collection CLI (``pipeline.collection.run.main``) end-to-end against
the LOCAL backend in a throwaway ``MAKE_MAGIC_DATA_DIR``, and PRINTS the real CLI
stdout/stderr at each step so the evidence can be captured. Offline: only the
card-enrichment resolver is stubbed (name-only), matching the pytest e2e; every
guard, the DuckDB mirror, and the audit/recover verbs run for real.

Run it:

    uv run --python 3.12 scripts/e2e_deck_integrity_demo.py      # from pipeline/
    # or, as a module after `uv sync`:
    uv run python -m scripts.e2e_deck_integrity_demo

It exits 0 on success (the recovered deck is back to exactly 100) and nonzero if
any narrative invariant fails — so it doubles as a smoke check.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def _banner(step: str) -> None:
    print('\n' + '=' * 72)
    print(step)
    print('=' * 72)


def _run(argv: list[str], *, expect_exit: int = 0) -> None:
    """Invoke the real CLI for one verb, printing the exact command + its output."""
    from pipeline.collection import run as cli

    print(f'$ collection {" ".join(argv)}')
    sys.stdout.flush()
    saved = sys.argv
    sys.argv = ['collection', *argv]
    try:
        cli.main()
        code = 0
    except SystemExit as exc:  # argparse/guards abort with a nonzero exit.
        code = int(exc.code or 0)
    finally:
        sys.argv = saved
        sys.stdout.flush()
        sys.stderr.flush()
    if code != expect_exit:
        raise SystemExit(f'STEP FAILED: expected exit {expect_exit}, got {code} for `{" ".join(argv)}`')


def _deck_size(name: str) -> int:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    return sum(c.quantity for c in LocalYamlStore(resolver=_StubResolver()).get_deck(name).cards)


def _inventory_names() -> set[str]:
    from pipeline.collection.adapters.local_yaml import LocalYamlStore

    return {c.name for c in LocalYamlStore(resolver=_StubResolver()).list_inventory()}


def _baseline_size(name: str) -> int | None:
    from pipeline import store as _lake
    from pipeline.collection import last_known_good_deck

    with _lake.connect() as conn:
        good = last_known_good_deck(conn, name, 'local')
    return None if good is None else int(good['size'])


class _StubResolver:
    """Name-only resolver — keeps enrichment offline (no Scryfall)."""

    def get_card(self, name: str) -> None:
        return None


def _write_deck(path: Path, name: str, *, extra_cards: int) -> Path:
    cards: list[dict[str, object]] = [{'name': 'Sol Ring', 'role': 'commander'}]
    cards += [{'name': f'{name} Card {i}'} for i in range(extra_cards)]
    path.write_text(json.dumps({'name': name, 'format': 'Commander', 'cards': cards}))
    return path


def main() -> None:
    # Isolate everything under a throwaway data dir; pin the local backend.
    tmp = Path(tempfile.mkdtemp(prefix='mm-e2e-'))
    os.environ['MAKE_MAGIC_DATA_DIR'] = str(tmp / 'data')
    os.environ['MAKE_MAGIC_BACKEND'] = 'local'

    # Stub the package-default resolver so no card enrichment reaches the network.
    import pipeline.collection.resolver as resolver_mod

    resolver_mod.default_card_resolver = lambda: _StubResolver()  # type: ignore[assignment]

    print(f'(local backend; throwaway MAKE_MAGIC_DATA_DIR={tmp / "data"})')

    # --- 1. SEED ------------------------------------------------------------- #
    _banner('STEP 1 — SEED: two 100-card Commander decks sharing Sol Ring')
    _run(['add-card', 'Sol Ring'])
    _run(['save-deck', '--from-json', str(_write_deck(tmp / 'alpha.json', 'Alpha EDH', extra_cards=99))])
    _run(['save-deck', '--from-json', str(_write_deck(tmp / 'beta.json', 'Beta EDH', extra_cards=99))])
    print(f'[check] Alpha EDH size={_deck_size("Alpha EDH")}, Beta EDH size={_deck_size("Beta EDH")}')

    # --- 2. BASELINE CAPTURE ------------------------------------------------- #
    _banner('STEP 2 — BASELINE: audit-decks records the known-good 100 state')
    _run(['audit-decks'])
    print(f'[check] mirror known-good baseline for Alpha EDH = {_baseline_size("Alpha EDH")}')

    # --- 3. PREVENTION ------------------------------------------------------- #
    _banner('STEP 3 — PREVENTION: remove-card "Sol Ring" WITHOUT --force must ABORT')
    # The CLI cascade guard aborts via argparse `parser.error`, which exits 2.
    _run(['remove-card', 'Sol Ring'], expect_exit=2)
    print(f'[check] Sol Ring still in inventory: {"Sol Ring" in _inventory_names()}')
    print(f'[check] Alpha EDH still {_deck_size("Alpha EDH")}, Beta EDH still {_deck_size("Beta EDH")}')

    # --- 4. DRIFT ------------------------------------------------------------ #
    _banner('STEP 4 — DRIFT: force the delete, then the cascade drops Sol Ring from Alpha EDH')
    _run(['remove-card', 'Sol Ring', '--force'])
    drifted = tmp / 'alpha_drifted.json'
    drifted.write_text(
        json.dumps(
            {'name': 'Alpha EDH', 'format': 'Commander', 'cards': [{'name': f'Alpha EDH Card {i}'} for i in range(99)]}
        )
    )
    _run(['save-deck', '--from-json', str(drifted), '--confirm'])
    print(f'[check] Alpha EDH is now {_deck_size("Alpha EDH")} (drifted below target)')

    # --- 5. DETECTION -------------------------------------------------------- #
    _banner('STEP 5 — DETECTION: audit-decks flags UNDER-TARGET + diffs Sol Ring')
    _run(['audit-decks'])

    # --- 6. RECOVERY --------------------------------------------------------- #
    _banner('STEP 6a — RECOVERY (dry-run): propose restoring Sol Ring, write NOTHING')
    _run(['recover-decks', 'Alpha EDH'])
    print(f'[check] after dry-run, Alpha EDH still {_deck_size("Alpha EDH")} (nothing written)')

    _banner('STEP 6b — RECOVERY (--confirm): restore Alpha EDH to exactly 100')
    _run(['recover-decks', 'Alpha EDH', '--confirm'])
    print(
        f'[check] Alpha EDH recovered to {_deck_size("Alpha EDH")}; '
        f'Sol Ring back in inventory: {"Sol Ring" in _inventory_names()}'
    )

    # --- 7. BASELINE INTEGRITY ---------------------------------------------- #
    _banner('STEP 7 — INTEGRITY: the known-good baseline was never overwritten')
    print(f'[check] mirror known-good baseline for Alpha EDH still = {_baseline_size("Alpha EDH")}')
    _run(['audit-decks'])

    # Final gate.
    assert _deck_size('Alpha EDH') == 100, 'recovery did not restore Alpha EDH to 100'
    assert _baseline_size('Alpha EDH') == 100, 'baseline was overwritten by drift'
    print('\nDEMO OK — prevent -> detect -> recover loop verified end-to-end.')


if __name__ == '__main__':
    main()
