#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "httpx",  # transitive: required by scryfall_cache
#     "typer",
#     "duckdb>=1.1",  # pipeline otag layer (transforms/store); fail-open if absent
#     "pydantic>=2.7",  # pipeline contracts (FactSheet); fail-open if absent
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# ///
"""
MTG deck fact sheet — emits a NEUTRAL, verifiable JSON fact sheet for a decklist.

This is NOT a scorer. It never assigns a role/quadrant, never decides if a card
is a wincon / ramp-vs-combo / engine-vs-clawback / "good." Those are contextual
roles → reasoning (LLM). This script emits only objective facts about the cards.

Two layers of facts:

  1. STRUCTURED facts (this file): Scryfall structured fields — cmc curve, color
     pips, produced_mana ramp/fixing, keyword census, instant-speed. These never
     touch oracle-text regex and are the OFFLINE fallback baseline.

  2. OTAG facts (delegated to ``pipeline.transforms.deck_factsheet.factsheet_for``):
     the multi-label ``otag_buckets`` map (bucket -> nonland card count) and a
     data-grounded ``susceptibility`` list, computed from Scryfall oracle-tags
     rolled up the tag DAG. Otags categorize where an oracle-text regex census
     would not — regex leaves 60%+ of nonlands uncategorized; otags do not.

Bundled + self-refreshing otag dataset: the otag layer routes through the
``ingest.oracle_tags`` puller's normal fetch -> cursor -> load path. On first
ONLINE use it pulls the FULL daily oracle-tags file into the store's ``raw/``
layer (the source of ~84-92% coverage) and REUSES that cached loaded copy on
later runs (daily cursor — no 18 MB refetch). With no network it fails open to
the bundled compressed snapshot (~20% baseline), and with no store at all it
degrades to structured facts only.

Graceful degradation (invariant I5): if the pipeline package or its otag data is
unavailable, this script STILL emits the structured facts with
``otag_buckets == {}`` and a clear "otag layer unavailable" signal in
``susceptibility``. It NEVER hard-requires the engine and NEVER crashes on it.

The emitted JSON validates against ``pipeline/contracts`` ``FactSheet`` in both
the pipeline-backed and the fallback path (same top-level shape).

Companion to card_tagger.py; reuses scryfall_cache.py for all Scryfall lookups.

Usage:
    ./deck_factsheet.py factsheet decklist.txt --output /tmp/deck-facts.json

Testing:
    uv run --with pytest --with typer --with httpx --with duckdb --with pydantic \
        pytest test_deck_factsheet.py -q

Maintenance:
    uvx ruff format deck_factsheet.py
    uvx ruff check deck_factsheet.py
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path

import typer

app = typer.Typer()

log = logging.getLogger('make_magic.deck_factsheet')

# CMC histogram buckets. 7+ collects everything at CMC >= 7.
_CMC_BUCKETS = ('0', '1', '2', '3', '4', '5', '6', '7+')
_PIP_SYMBOLS = ('W', 'U', 'B', 'R', 'G', 'C')

#: Prefix that marks the graceful-degradation signal in ``susceptibility`` when
#: the otag layer is unavailable. Kept as a constant so callers/tests can key on
#: it without matching prose.
_OTAG_UNAVAILABLE = (
    'otag layer unavailable: the oracle-tag buckets and susceptibility signals '
    'could not be computed (pipeline package or its snapshot is missing); '
    'reporting structured facts only.'
)


# --------------------------------------------------------------------------- #
# STRUCTURED-FACT functions — no network, no oracle-text regex, no I/O.
#
# Each takes a list of Scryfall-shaped card dicts (already face-normalized via
# _card_fields at the CLI boundary) and returns objective counts derived from
# Scryfall STRUCTURED fields only. These back the offline fallback and are the
# facts that would NOT change if a card were moved to a different deck.
#
# Functional interaction categorization (removal/counters/protection/draw/...) is
# NOT done here via oracle-text regex; it is delegated to the pipeline's otag
# buckets — see the delegation in build_factsheet().
# --------------------------------------------------------------------------- #


def is_land(type_line: str) -> bool:
    """A card is a land iff its FRONT face is a land.

    Uses only the front face so a modal DFC spell // land (e.g. Malakir Rebirth //
    Malakir Mire, type line "Instant // Land") is treated as the castable spell it
    is, not silently dropped from the nonland census/coverage.
    """
    front = (type_line or '').split('//')[0]
    return 'land' in front.lower()


def _type_line(card: dict) -> str:
    return card.get('type_line') or ''


def _is_instant_speed(card: dict) -> bool:
    """Instant-speed iff type line is an Instant OR the card has Flash.

    Structured signal (type line + Scryfall ``keywords``), NOT oracle-text regex.
    """
    if 'instant' in _type_line(card).lower():
        return True
    kw = [k.lower() for k in card.get('keywords', []) or []]
    return 'flash' in kw


# --- ramp & fixing (produced_mana — structured) ---------------------------- #


def _produces_mana(card: dict) -> bool:
    """A nonland produces mana iff Scryfall's structured produced_mana is set.

    The precise, structured signal — no regex over 'add {...}'. Cards whose own
    cast is cheapened ("this spell costs {1} less") have no produced_mana and
    correctly do NOT count as ramp.
    """
    return bool(card.get('produced_mana'))


def _is_ramp_source(card: dict) -> bool:
    """A NONLAND is a ramp source if it produces mana (structured produced_mana).

    This structured fallback keys on produced_mana ONLY (no oracle-text regex).
    Land-ramp / tutor-to-play cards that produce no mana themselves are captured
    by the otag ``ramp`` bucket in the pipeline layer, not here.
    """
    if is_land(_type_line(card)):
        return False
    return _produces_mana(card)


def _is_fixing_source(card: dict) -> bool:
    """A NONLAND ramp source that produces >1 distinct color or any-color.

    'Any color' shows up in produced_mana as all five colors (Scryfall lists the
    concrete colors a source can produce). >1 distinct WUBRG color => fixing.
    Colorless-only (['C']) is ramp but not fixing.
    """
    if is_land(_type_line(card)):
        return False
    pm = card.get('produced_mana') or []
    colors = {m for m in pm if m in ('W', 'U', 'B', 'R', 'G')}
    return len(colors) > 1


def _pip_counts(cards: list[dict]) -> dict:
    """Count colored/colorless mana pips across nonland mana costs.

    Structural fact from the mana cost symbols. Hybrid/Phyrexian pips are
    counted once per listed color symbol.
    """
    counts = dict.fromkeys(_PIP_SYMBOLS, 0)
    for c in cards:
        if is_land(_type_line(c)):
            continue
        cost = c.get('mana_cost') or ''
        for sym in re.findall(r'\{([^}]+)\}', cost):
            for part in sym.split('/'):
                if part in counts:
                    counts[part] += 1
    return counts


def ramp_and_fixing(cards: list[dict]) -> dict:
    """Count ramp sources, fixing sources, and pip distribution (nonland only)."""
    ramp = sum(1 for c in cards if _is_ramp_source(c))
    fixing = sum(1 for c in cards if _is_fixing_source(c))
    return {
        'ramp_sources': ramp,
        'fixing_sources': fixing,
        'pip_counts': _pip_counts(cards),
    }


# --- keyword census (Scryfall `keywords` — structured) --------------------- #


def keyword_census(cards: list[dict]) -> dict:
    """Count Scryfall `keywords` across the deck. Nonzero only, structured."""
    counter: Counter[str] = Counter()
    for c in cards:
        for kw in c.get('keywords', []) or []:
            counter[kw] += 1
    return dict(counter)


# --- shape (cmc curve — structured) ---------------------------------------- #


def _cmc_bucket(cmc: float) -> str:
    n = int(cmc or 0)
    return '7+' if n >= 7 else str(n)


def cmc_histogram(cards: list[dict]) -> dict:
    """CMC histogram over NONLAND cards, bucketed 0..6 and 7+."""
    hist = dict.fromkeys(_CMC_BUCKETS, 0)
    for c in cards:
        if is_land(_type_line(c)):
            continue
        hist[_cmc_bucket(c.get('cmc') or 0)] += 1
    return hist


def _avg_cmc(cards: list[dict]) -> float:
    nonland = [c for c in cards if not is_land(_type_line(c))]
    if not nonland:
        return 0.0
    total = sum(float(c.get('cmc') or 0) for c in nonland)
    return round(total / len(nonland), 2)


def _top_end_count(cards: list[dict]) -> int:
    return sum(1 for c in cards if not is_land(_type_line(c)) and float(c.get('cmc') or 0) >= 6)


def _shape(cards: list[dict]) -> dict:
    return {
        'nonland_count': sum(1 for c in cards if not is_land(_type_line(c))),
        'land_count': sum(1 for c in cards if is_land(_type_line(c))),
        'cmc_histogram': cmc_histogram(cards),
        'avg_cmc': _avg_cmc(cards),
        'top_end_count': _top_end_count(cards),
    }


# --- per-card records ------------------------------------------------------ #


def _card_record(card: dict) -> dict:
    """Raw per-card facts so the LLM has material without re-fetching."""
    return {
        'name': card.get('name', ''),
        'cmc': card.get('cmc'),
        'type_line': _type_line(card),
        'keywords': card.get('keywords', []) or [],
        'produced_mana': card.get('produced_mana'),
        'is_land': is_land(_type_line(card)),
        'oracle_text': card.get('oracle_text') or '',
    }


# --------------------------------------------------------------------------- #
# Pipeline integration — the otag layer (buckets + susceptibility).
#
# We add the pipeline package to sys.path with the SAME shim used to reach
# scryfall_cache, then call the pipeline's DONE, tested transforms. We do NOT
# re-implement the rollup, the crosswalk, or the puller's fetch/watermark/land
# logic here (constraint): the script is a CONSUMER of the pipeline's puller and
# transforms. The otag source is self-refreshing (puller-backed, cached) with a
# snapshot fallback; every layer and the delegation are try-guarded so a missing
# package / unusable store / missing snapshot / any error degrades gracefully.
# --------------------------------------------------------------------------- #

#: The pipeline package root (``plugins/make-magic/pipeline``) — the dir holding
#: the importable ``pipeline`` package. Mirrors the scryfall_cache path shim.
_PIPELINE_ROOT = Path(__file__).resolve().parents[1] / 'pipeline'


def _ensure_pipeline_on_path() -> None:
    """Add the pipeline package root to sys.path (idempotent)."""
    root = str(_PIPELINE_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)


def _rollup_to_card_otag(tags: list[dict]) -> dict[str, set[str]]:
    """Explode ``tags`` into the ``oracle_id -> set[slug]`` closure map.

    Wraps the pipeline's pure-Python DAG rollup (``otag_rollup.rollup_rows``);
    kept tiny and separate so BOTH the puller-backed path and the snapshot
    fallback path build the map from the same rollup.
    """
    from pipeline.transforms import otag_rollup

    card_otag: dict[str, set[str]] = {}
    for oracle_id, slug in otag_rollup.rollup_rows(tags):
        card_otag.setdefault(oracle_id, set()).add(slug)
    return card_otag


def _load_tags_via_puller() -> list[dict]:
    """Get the FULL oracle-tags via the ingest puller's normal path.

    This is the "bundled + self-refreshing dataset" pattern:

      * ``oracle_tags.sync()`` does fetch -> **cursor** check -> load into the
        store's ``raw/oracle_tags`` layer. On the FIRST online use it fetches the
        full ~18 MB daily file (the source of ~84-92% coverage) and loads it; on
        SUBSEQUENT runs the daily cursor short-circuits the re-load, so we do
        NOT refetch 18 MB every invocation — we REUSE the cached raw/ copy.
      * The puller itself FAILS OPEN to the bundled snapshot on any network/HTTP
        error, so this path still yields tags offline (just the capped baseline).

    After loading, we read the tags back out of ``raw/oracle_tags`` with the
    rollup's own reader so the rolled-up map is built from whatever source the
    puller actually loaded (full when online/cached, snapshot when offline).

    Raises on any store/duckdb failure so the caller can fall back to loading the
    bundled snapshot directly (never crashing the fact sheet).
    """
    from pipeline.ingest import oracle_tags
    from pipeline.transforms import otag_rollup

    # sync() loads raw/oracle_tags (cursor-gated; fail-open to snapshot).
    oracle_tags.sync()
    # Read back whatever was loaded (full daily file when online/cached).
    return otag_rollup._load_raw_tags()


def _load_card_otag() -> dict[str, set[str]] | None:
    """Build the ``oracle_id -> set[slug]`` closure, self-refreshing when online.

    "Bundled + self-refreshing dataset". Source selection, in order:

      1. **Puller-backed (preferred):** route through ``oracle_tags.sync()`` — its
         fetch -> cursor -> load pipeline — to get the FULL oracle-tags into
         the store's ``raw/`` layer on first online use and REUSE that cached
         loaded copy on later runs (daily cursor; no 18 MB refetch). This is
         what delivers the ~84-92% coverage. Read the loaded tags back and roll
         them up.
      2. **Snapshot fallback:** if the puller path raises (store/duckdb missing,
         load error, etc.), load the bundled compressed snapshot directly
         (``oracle_tags._load_snapshot``) — the offline baseline (~20% coverage).
         Note the puller ALSO fails open to this snapshot internally on a network
         error; this second try only fires when the store machinery itself is
         unusable.
      3. **None (structured-only):** if even the snapshot cannot load, return
         None so the caller degrades to structured facts only (invariant I5).

    Never crashes: every layer is try-guarded and fails open to the next.
    """
    try:
        _ensure_pipeline_on_path()
    except Exception as exc:
        log.warning('otag layer: pipeline path setup failed (%s); degrading.', exc)
        return None

    # 1) Puller-backed: full dataset on first online use, cached thereafter.
    try:
        tags = _load_tags_via_puller()
        return _rollup_to_card_otag(tags)
    except Exception as exc:
        log.warning('otag layer: puller path failed (%s); trying bundled snapshot.', exc)

    # 2) Snapshot fallback: bundled offline baseline (capped taggings).
    try:
        from pipeline.ingest import oracle_tags

        return _rollup_to_card_otag(oracle_tags._load_snapshot())
    except Exception as exc:
        log.warning('otag layer: snapshot load failed (%s); degrading.', exc)
        return None


def _pipeline_factsheet(
    cards: list[dict],
    deck: str | None,
    missing: list[str] | None,
    card_otag: dict[str, set[str]],
    focus: list[str] | None = None,
) -> dict | None:
    """Delegate the full fact sheet to the pipeline's ``factsheet_for``.

    Returns the pipeline-built FactSheet-valid dict (structured facts + otag
    ``otag_buckets`` + ``susceptibility`` + focus-relative ``focus``/
    ``focus_relative``), or None on ANY import/build failure so the caller
    degrades to the structured-only fallback.

    ``focus`` is the deck's NARROW declared focus set, passed READ-ONLY to the
    pipeline. This script NEVER writes ``Focus Otags`` (or any Deck field).
    """
    try:
        _ensure_pipeline_on_path()
        from pipeline.transforms.deck_factsheet import factsheet_for

        return factsheet_for(cards, card_otag=card_otag, deck=deck, missing=missing, focus=focus)
    except Exception as exc:
        log.warning('otag layer: factsheet_for failed (%s); degrading.', exc)
        return None


# --------------------------------------------------------------------------- #
# Fallback fact sheet — structured facts only, when the otag layer is absent.
#
# Emits the SAME top-level shape as the pipeline (so it still validates against
# contracts.FactSheet), degraded to the structured subset: instant_speed is real
# (structured); the functional interaction census fields are zeroed; coverage
# lists every nonland as uncategorized (an honest "no otag signal" tell);
# otag_buckets is empty; and susceptibility carries the clear "otag layer
# unavailable" diagnostic.
# --------------------------------------------------------------------------- #


def _fallback_factsheet(
    cards: list[dict],
    deck: str | None = None,
    missing: list[str] | None = None,
) -> dict:
    """Build the structured-only fact sheet (otag layer unavailable)."""
    nonland_names = [c.get('name', '') for c in cards if not is_land(_type_line(c))]
    return {
        'deck': deck,
        'shape': _shape(cards),
        'mana': ramp_and_fixing(cards),
        'keywords': keyword_census(cards),
        # Retired regex census -> zeroed; instant_speed stays (structured).
        'interaction': {
            'board_wipes': 0,
            'spot_removal': 0,
            'counterspells': 0,
            'protection': 0,
            'instant_speed': sum(1 for c in cards if _is_instant_speed(c)),
        },
        'card_advantage': {'repeatable_draw': 0, 'one_shot_draw': 0},
        'structural': {'etb_creatures': 0, 'graveyard_recursion_present': False},
        # No otag data -> every nonland is uncategorized (honest degraded tell).
        'coverage': {
            'categorized_pct': 0.0,
            'uncategorized_pct': 100.0 if nonland_names else 0.0,
            'uncategorized_cards': nonland_names,
        },
        'cards': [_card_record(c) for c in cards],
        'missing': missing or [],
        'otag_buckets': {},
        'susceptibility': [_OTAG_UNAVAILABLE],
        # Focus-relative signals need the otag layer to resolve; unavailable here.
        # The fields are still present (empty) so the contract holds.
        'focus': [],
        'focus_relative': {
            'coverage_of_focus': {},
            'thin_focus': [],
            'off_focus': [],
        },
    }


def build_factsheet(
    cards: list[dict],
    deck: str | None = None,
    missing: list[str] | None = None,
    card_otag: dict[str, set[str]] | None = None,
    focus: list[str] | None = None,
) -> dict:
    """Assemble the neutral fact sheet, otag-powered when the pipeline is present.

    NO role/quadrant labels appear anywhere in the output — only facts.

    The primary path delegates to the pipeline's ``factsheet_for`` (structured
    facts + ``otag_buckets`` + ``susceptibility`` + focus-relative signals). If
    ``card_otag`` is None (snapshot could not load) or the pipeline delegation
    fails for any reason, we degrade to ``_fallback_factsheet`` (structured facts,
    empty buckets, empty focus fields, a clear "otag layer unavailable" signal).
    Either way the output validates against ``contracts.FactSheet``.

    Args:
        cards: Face-normalized Scryfall-shaped card dicts (carry ``oracle_id``).
        deck: Optional deck name.
        missing: Decklist names that did not resolve to a card.
        card_otag: ``oracle_id -> set[slug]`` closure. When None, the otag layer
            is treated as unavailable and the fallback is used.
        focus: The deck's NARROW declared focus set (``Focus Otags``), READ-ONLY.
            Optional; when None/empty the focus-relative fields come back empty.
            This script NEVER writes the focus back to Airtable or anywhere else.
    """
    if card_otag is not None:
        built = _pipeline_factsheet(cards, deck, missing, card_otag, focus=focus)
        if built is not None:
            return built
    return _fallback_factsheet(cards, deck=deck, missing=missing)


# --------------------------------------------------------------------------- #
# Decklist parsing (inline-comment + set-annotation stripping; skips blanks /
# comments / section headers).
# --------------------------------------------------------------------------- #

_DECK_LINE = re.compile(r'^\s*(?:(\d+)x?\s+)?(.+?)\s*$')


def _parse_decklist(raw: str) -> list[tuple[int, str]]:
    """Parse 'N Card Name' / 'Nx Card Name' / 'Card Name' lines. Skips blanks,
    comments (#, //), and section headers (lines ending in ':')."""
    out: list[tuple[int, str]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith(('#', '//')):
            continue
        # Strip inline comments ("1 Sol Ring  # COMMANDER" -> "1 Sol Ring").
        s = re.split(r'\s+(?:#|//)', s, maxsplit=1)[0].strip()
        if not s or s.endswith(':'):
            continue
        m = _DECK_LINE.match(s)
        if not m:
            continue
        count = int(m.group(1)) if m.group(1) else 1
        name = m.group(2).strip()
        # Drop trailing set/collector annotations like "(C21) 123".
        name = re.sub(r'\s*\([0-9A-Za-z]{2,5}\)\s*[\d\-A-Za-z]*$', '', name).strip()
        if name:
            out.append((count, name))
    return out


# --------------------------------------------------------------------------- #
# Scryfall-backed CLI — thin shell over the pure functions above.
# --------------------------------------------------------------------------- #


def _get_cache():
    """Import ScryfallCache from the sibling script (same shim as card_tagger)."""
    script_dir = Path(__file__).resolve().parent
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    from scryfall_cache import ScryfallCache

    return ScryfallCache()


def _card_fields(card: dict) -> dict:
    """Normalize a Scryfall card into the flat shape the fact functions expect,
    falling back to card_faces[0] for double-faced cards (DFCs).

    Carries ``oracle_id`` through — it is the durable join key for the otag
    layer (Scryfall cards, incl. DFCs, carry a single top-level oracle_id)."""
    face = card
    if card.get('oracle_text') is None and card.get('card_faces'):
        face = card['card_faces'][0]
    return {
        'name': card.get('name', ''),
        'oracle_id': card.get('oracle_id'),
        'oracle_text': face.get('oracle_text', '') or '',
        'type_line': card.get('type_line') or face.get('type_line', '') or '',
        'cmc': card.get('cmc'),
        'keywords': card.get('keywords', []) or [],
        'produced_mana': card.get('produced_mana'),
        'mana_cost': face.get('mana_cost', card.get('mana_cost', '')) or '',
    }


def _parse_focus(focus: str | None) -> list[str]:
    """Parse a comma-separated ``--focus`` value into a clean list of entries.

    Splits on commas, trims whitespace, drops blanks, and de-duplicates while
    preserving order. Entries may be bucket names (``counters``, ``tokens``) or
    raw otag slugs; the pipeline resolves either level. READ-ONLY — the focus is
    only measured against, never written anywhere.
    """
    if not focus:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for part in focus.split(','):
        entry = part.strip()
        if entry and entry not in seen:
            seen.add(entry)
            out.append(entry)
    return out


@app.command()
def factsheet(
    path: str,
    output: str = typer.Option(None, '--output', help='Write JSON here instead of stdout'),
    focus: str = typer.Option(
        None,
        '--focus',
        help=(
            'Optional comma-separated focus set (bucket names and/or otag slugs) '
            "the deck CARES about, e.g. 'counters,typal,tokens'. READ-ONLY: the "
            "fact sheet measures the deck's cards against it; nothing is written "
            'back. Omit for no focus-relative analysis.'
        ),
    ),
) -> None:
    """Build a neutral fact sheet for a decklist file (fetches via Scryfall cache)."""
    cache = _get_cache()
    raw = Path(path).read_text()
    entries = _parse_decklist(raw)

    cards: list[dict] = []
    missing: list[str] = []
    for _count, name in entries:
        card = cache.get_card(name)
        if not card:
            missing.append(name)
            continue
        cards.append(_card_fields(card))

    deck_name = _deck_name_from_header(raw)
    focus_set = _parse_focus(focus)
    card_otag = _load_card_otag()  # None -> graceful fallback (I5).
    report = build_factsheet(cards, deck=deck_name, missing=missing, card_otag=card_otag, focus=focus_set)
    payload = json.dumps(report, indent=2)
    if output:
        Path(output).write_text(payload)
        typer.echo(
            f'Wrote {output} — {report["shape"]["nonland_count"]} nonland, '
            f'{report["shape"]["land_count"]} lands, {len(missing)} missing, '
            f'{len(report["otag_buckets"])} otag buckets'
        )
    else:
        typer.echo(payload)


def _deck_name_from_header(raw: str) -> str | None:
    """First non-empty comment line is treated as the deck name, if present."""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith('#'):
            return s.lstrip('#').strip() or None
        if s:
            return None
    return None


@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


if __name__ == '__main__':
    app()
