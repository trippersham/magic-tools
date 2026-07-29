#!/usr/bin/env -S uv run --python 3.12 --script
#
# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "make-magic-pipeline",  # pipeline contracts/transforms + scryfall_cache's package import
#     "typer",                # this script's CLI
# ]
# [tool.uv]
# exclude-newer = "2026-06-08T00:00:00Z"
# [tool.uv.sources]
# make-magic-pipeline = { path = "../pipeline", editable = true }
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
``sources.oracle_tags`` puller's normal fetch -> cursor -> load path. On first
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
from pathlib import Path

log = logging.getLogger('make_magic.deck_factsheet')

#: Prefix that marks the graceful-degradation signal in ``susceptibility`` when
#: the otag layer is unavailable. Kept as a constant so callers/tests can key on
#: it without matching prose.
_OTAG_UNAVAILABLE = (
    'otag layer unavailable: the oracle-tag buckets and susceptibility signals '
    'could not be computed (pipeline package or its snapshot is missing); '
    'reporting structured facts only.'
)


# --------------------------------------------------------------------------- #
# STRUCTURED-FACT functions — ONE copy of the math lives in the shared pipeline
# transform (``pipeline.transforms.deck_factsheet``); this script is a CONSUMER.
#
# The in-script census math (shape / mana / pip / keyword / cmc / per-card /
# instant-speed / is_land) was DELETED in #5 Task 6a and is now sourced from the
# transform via ``_facts()``. The wrappers below preserve the script's public
# names (kept for its tests + the CLI shell) but delegate to the single copy, so
# the fact sheet output is byte-identical while there is no duplicated logic.
#
# The transform import is LAZY (never at module top level) so the script keeps
# its graceful degradation (invariant I5): if the pipeline package is missing the
# fallback path is not reachable anyway (it needs the same transform), but the
# import failure surfaces as the normal degrade, never a hard crash on load.
# --------------------------------------------------------------------------- #


def _facts():
    """Return the shared ``pipeline.transforms.deck_factsheet`` module (lazy).

    The SOLE home for the structured-fact math + the otag mart. Adds the pipeline
    package root to sys.path first (idempotent) so the PEP-723 script — which does
    not vendor the package — can reach it.
    """
    _ensure_pipeline_on_path()
    from pipeline.transforms import deck_factsheet as _t

    return _t


def is_land(type_line: str) -> bool:
    """A card is a land iff its FRONT face is a land (delegates to the transform).

    Front-face only so a modal DFC spell // land (e.g. Malakir Rebirth // Malakir
    Mire, "Instant // Land") is treated as the castable spell it is.
    """
    return _facts().is_land(type_line)


def _type_line(card: dict) -> str:
    return card.get('type_line') or ''


def _is_instant_speed(card: dict) -> bool:
    """Instant-speed iff type line is an Instant OR the card has Flash (transform)."""
    return _facts()._is_instant_speed(card)


def ramp_and_fixing(cards: list[dict]) -> dict:
    """Ramp/fixing/pip distribution (nonland only), structured-only fallback rule.

    Ramp keys on structured ``produced_mana`` ONLY (the transform's
    ``structured_ramp`` — the regex-free baseline); the pipeline path additionally
    counts land-fetch ramp. Fixing + pip counts reuse the transform's copy.
    """
    t = _facts()
    ramp = sum(1 for c in cards if t.structured_ramp(c))
    fixing = sum(1 for c in cards if t._is_fixing_source(c))
    return {
        'ramp_sources': ramp,
        'fixing_sources': fixing,
        'pip_counts': t._pip_counts(cards),
    }


def keyword_census(cards: list[dict]) -> dict:
    """Count Scryfall `keywords` across the deck (nonzero only) — transform copy."""
    return _facts()._keywords(cards)


def cmc_histogram(cards: list[dict]) -> dict:
    """CMC histogram over NONLAND cards, bucketed 0..6 and 7+ (from the shape mart)."""
    return _facts()._shape(cards)['cmc_histogram']


def _avg_cmc(cards: list[dict]) -> float:
    return _facts()._shape(cards)['avg_cmc']


def _top_end_count(cards: list[dict]) -> int:
    return _facts()._shape(cards)['top_end_count']


def _shape(cards: list[dict]) -> dict:
    return _facts()._shape(cards)


def _pip_counts(cards: list[dict]) -> dict:
    return _facts()._pip_counts(cards)


def _card_record(card: dict) -> dict:
    """Raw per-card facts so the LLM has material without re-fetching (transform)."""
    return _facts()._card_record(card)


# --------------------------------------------------------------------------- #
# Pipeline integration — the otag layer (buckets + susceptibility).
#
# We add the pipeline package to sys.path with the SAME shim used to reach
# scryfall_cache, then call the pipeline's DONE, tested transforms. We do NOT
# re-implement the rollup, the crosswalk, or the puller's fetch/cursor/land
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
    """Get the FULL oracle-tags via the sources puller's normal path.

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
    from pipeline.sources import oracle_tags
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
        from pipeline.sources import oracle_tags

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
    """Build the structured-only fact sheet (otag layer unavailable).

    Every structured block is sourced from the shared transform (via the
    delegating wrappers above): ``_shape`` / ``ramp_and_fixing`` / ``keyword_census``
    / ``_is_instant_speed`` / ``_card_record``, plus the transform's ``_coverage``
    over an EMPTY otag map (every nonland uncategorized — the honest degraded
    tell). The functional census (interaction/card_advantage/structural) is zeroed
    because it needs the otag layer, which is absent here.
    """
    t = _facts()
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
        # No otag data -> every nonland is uncategorized (transform's coverage over
        # an empty otag map: categorized 0%, all nonland names uncategorized).
        'coverage': t._coverage(cards, {}),
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
# Deck entry point — build a fact sheet from a resolved ``contracts.Deck``.
#
# The SECOND entry point (alongside raw-text ``_parse_decklist``): a caller that
# already has a ``Deck`` from the CollectionStore (local YAML in offline mode)
# hands its hydrated ``DeckCard``s straight in, no Scryfall fetch. Each DeckCard
# is a hydrated base-``Card`` (name + Scryfall enrichment), which we map into the
# flat card-dict shape the fact functions + the pipeline transform consume.
# ``mana_value`` maps to ``cmc`` and ``mana_cost`` (Scryfall `mana_cost`) is
# carried through so pip_counts computes on this path (unresolved cards degrade
# to an empty mana cost, i.e. zero pips).
# --------------------------------------------------------------------------- #


def _deck_card_to_fields(card) -> dict:  # a contracts.DeckCard (duck-typed)
    """Map a hydrated ``DeckCard`` to the flat fact-function card dict.

    Carries ``oracle_id`` (the durable otag join key) and ``cmc`` (from
    ``mana_value``). Missing enrichment (unresolved card) stays null/empty so the
    census degrades honestly rather than crashing.
    """
    return {
        'name': card.name,
        'oracle_id': card.oracle_id,
        'oracle_text': card.oracle_text or '',
        'type_line': card.type_line or '',
        'cmc': card.mana_value,
        'keywords': list(card.keywords or []),
        'produced_mana': card.produced_mana,
        # Scryfall `mana_cost` (e.g. `{2}{G}{G}`) drives pip_counts; hydrated on
        # the Card contract. Unresolved cards have None -> '' (zero pips).
        'mana_cost': getattr(card, 'mana_cost', None) or '',
    }


def factsheet_from_deck(deck, focus: list[str] | None = None) -> dict:  # a contracts.Deck
    """Build a neutral fact sheet from a resolved ``contracts.Deck``.

    The offline entry point: the deck's ``DeckCard``s are already hydrated (via
    the CollectionStore's ``CardResolver``), so NO Scryfall fetch happens here.
    Loads the otag closure (self-refreshing / snapshot / degrade) exactly like the
    text path, then delegates to ``build_factsheet``. Output validates against
    ``contracts.FactSheet``.
    """
    cards = [_deck_card_to_fields(c) for c in deck.cards]
    card_otag = _load_card_otag()  # None -> graceful fallback (I5).
    return build_factsheet(cards, deck=deck.name, missing=[], card_otag=card_otag, focus=focus or [])


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

#: A TRAILING printing annotation, e.g. the "(Borderless)" in "Parallel Lives
#: (Borderless)" or "(Retro)" in "Sol Ring (Retro)". Real cardlist names carry
#: these cosmetic suffixes but Scryfall oracle names do not, so an exact name
#: lookup misses. We strip a single trailing parenthetical as a fallback.
_PRINTING_ANNOTATION = re.compile(r'\s*\([^()]*\)\s*$')


def _strip_printing_annotation(name: str) -> str:
    """Strip a TRAILING parenthetical printing annotation from a card name.

    ``"Parallel Lives (Borderless)"`` -> ``"Parallel Lives"``. A name with no
    trailing parenthetical is returned unchanged.
    """
    return _PRINTING_ANNOTATION.sub('', name).strip()


def _resolve_card(cache, name: str) -> dict | None:
    """Resolve a decklist ``name`` to a Scryfall card, tolerating printing suffixes.

    Tries the exact name first, then falls back to the name with a trailing
    printing annotation ("(Borderless)", "(Retro)", ...) stripped. Returns
    ``None`` if still unresolved so the caller keeps its ``missing`` behavior.
    """
    card = cache.get_card(name)
    if card:
        return card
    stripped = _strip_printing_annotation(name)
    if stripped != name:
        return cache.get_card(stripped)
    return None


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


def _deck_name_from_header(raw: str) -> str | None:
    """First non-empty comment line is treated as the deck name, if present."""
    for line in raw.splitlines():
        s = line.strip()
        if s.startswith('#'):
            return s.lstrip('#').strip() or None
        if s:
            return None
    return None


def _run_cli() -> None:
    """The typer CLI entrypoint. ``typer`` is imported HERE (lazily), NOT at module
    scope, so importing this module for ``factsheet_from_deck`` never drags typer
    into an importer's environment — e.g. the ``collection`` CLI reuses
    ``factsheet_from_deck`` and declares no typer dependency. typer stays in this
    script's OWN inline deps for when it's run directly as a CLI.
    """
    import typer

    # PEP 563 (`from __future__ import annotations`) makes the callback's
    # ``ctx: typer.Context`` annotation a STRING that typer eval-resolves against
    # this module's globals. Since ``typer`` is imported lazily (function-local),
    # expose it in module globals HERE so that resolution works — this only runs
    # under ``__main__`` (direct CLI use), so an importer of ``factsheet_from_deck``
    # never gains a module-level typer.
    globals()['typer'] = typer

    app = typer.Typer()

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
            card = _resolve_card(cache, name)
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

    @app.callback(invoke_without_command=True)
    def main(ctx: typer.Context) -> None:
        if ctx.invoked_subcommand is None:
            typer.echo(ctx.get_help())

    app()


if __name__ == '__main__':
    _run_cli()
