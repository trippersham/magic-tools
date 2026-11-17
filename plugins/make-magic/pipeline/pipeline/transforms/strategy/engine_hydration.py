"""Engine-hydration utilities (Phase PA).

Deterministic building blocks the user-initiated engine derivation uses:

1. ``unauthored_cards(deck_cards, authored_edges)`` — cards that carry an engine-relevant
   otag but are NOT already covered by an authored within-card edge query.
   These are what the derivation sends to haiku.

2. ``verify(query_str, role, ...)`` — the three-check verification gate for
   authored queries: RE2 pasteability, resolution + expected-hit, and a
   deterministic over-capture oracle using lake otags.

Neither function invokes an LLM.  Both are replay-deterministic given the same
lake snapshot.  The proactive detector is DROPPED; PA builds deterministic
utilities only.
"""

from __future__ import annotations

import json
import logging
import re as _re_mod
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pipeline.transforms.strategy.query import (
    CardView,
    _assert_re2_pasteable,  # the HELD RE2 guard — reused by import, not modified
    compile_query,
)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

#: A search client: takes a query string, returns card dicts each with at
#: least ``name`` (str) and ``otags`` (list[str]).
SearchClient = Callable[[str], list[dict]]

# ---------------------------------------------------------------------------
# Event-catalog access
# ---------------------------------------------------------------------------

_CATALOG_PATH = Path(__file__).resolve().parent / 'data' / 'event_catalog.json'


@lru_cache(maxsize=1)
def _load_events() -> dict[str, dict]:
    """Load the ``events`` dict from the catalog (module-level cache).

    Adjacency is DERIVED at load (ADR 0012/0022): the hand-enumerated
    ``produced_by``/``triggers`` are replaced by the descendant-closure of each
    side's owner-approved anchors via the one named rule
    ``derive_event_adjacency`` — the same view ``assets.load_assets`` serves, so
    the edge-author hydration universes never drift from the election path.
    """
    with _CATALOG_PATH.open() as f:
        raw_events = json.load(f)['events']
    try:
        from pipeline.transforms.strategy.adjacency import derive_event_adjacency

        return derive_event_adjacency(raw_events)
    except FileNotFoundError:
        # No lake (offline CI / unhydrated): committed enumerated adjacency.
        # Observable, not silent — production must hydrate the lake (ADR 0022).
        logging.getLogger(__name__).warning(
            'oracle-tag taxonomy absent; using committed enumerated event '
            'adjacency (derived adjacency disabled — hydrate the lake). ADR 0022.'
        )
        return raw_events


def engine_relevant_otags() -> tuple[frozenset[str], frozenset[str]]:
    """Compute TRIGGER and EFFECT otag universes from the event catalog.

    Returns ``(trigger_otags, effect_otags)`` where:

    - **trigger_otags** = union of every event's ``triggers``
    - **effect_otags**  = union of every event's ``produced_by``
    """
    events = _load_events()
    trigger: set[str] = set()
    effect: set[str] = set()
    for ev in events.values():
        trigger.update(ev['triggers'])
        effect.update(ev['produced_by'])
    return frozenset(trigger), frozenset(effect)


# ---------------------------------------------------------------------------
# Curated edge queries — convenience helper
# ---------------------------------------------------------------------------


def curated_edge_queries() -> list[tuple[str, str]]:
    """Return the ``(name, query)`` pairs for curated within-card edge attributes.

    These are the edge-category entries from ``SYNTH_ALWAYS_ATTRIBUTES`` that
    have registered roles in ``CURATED_EDGE_ROLES``.  The caller concatenates
    these with any machine-store edge queries to form the ``authored_edges``
    argument to :func:`unauthored_cards`.
    """
    from pipeline.transforms.strategy.edge_roles import CURATED_EDGE_ROLES
    from pipeline.transforms.strategy.vocab import SYNTH_ALWAYS_ATTRIBUTES

    return [(n, q) for n, q in SYNTH_ALWAYS_ATTRIBUTES if n in CURATED_EDGE_ROLES]


# ---------------------------------------------------------------------------
# unauthored_cards — deterministic, no inference
# ---------------------------------------------------------------------------


def unauthored_cards(
    deck_cards: list[dict],
    authored_edges: list[tuple[str, str]],
) -> list[dict]:
    """Cards carrying an engine-relevant otag NOT covered by any authored edge.

    The derivation sends these to haiku.  No connectivity/loop logic — the
    derivation's walk handles that.  Simplicity is the point.

    Parameters
    ----------
    deck_cards
        Deck card dicts, each with at least ``otags`` (list[str]).
    authored_edges
        ``(name, query)`` pairs for ALL authored within-card edge attributes
        (curated + machine store).  Assemble via :func:`curated_edge_queries`
        plus machine-store attrs.

    Returns
    -------
    list[dict]
        Unauthored cards — engine-relevant and uncovered — in deck order.
    """
    trigger_otags, effect_otags = engine_relevant_otags()
    relevant = trigger_otags | effect_otags

    # Compile edge queries once.
    compiled = [(n, compile_query(q)) for n, q in authored_edges]

    result: list[dict] = []
    for card in deck_cards:
        card_otags = frozenset(t.lower() for t in (card.get('otags') or []))
        # Engine-relevant?
        if not (card_otags & relevant):
            continue
        # Covered by an authored edge?
        view = CardView.from_card(card)
        if any(pred(view) for _, pred in compiled):
            continue
        result.append(card)
    return result


# ---------------------------------------------------------------------------
# verify — the three-check verification gate
# ---------------------------------------------------------------------------

#: Over-capture rejection threshold.
#:
#: The minimum fraction of captured cards that must **corroborate** — their lake
#: otags intersect BOTH the trigger-event's ``triggers`` AND the
#: effect-event's ``produced_by``.
#:
#: **Why 0.5 (50%):**
#:
#: A well-authored query constrains both sides via otag atoms + regex proximity,
#: so its captured set is predominantly correct (corroboration near 100%).
#: A bare regex without otag constraints captures many false positives whose
#: otags have nothing to do with the claimed role, dropping the rate well below
#: 50%.  The known false-positive classes — self-life-loss "drain"
#: (Transcendence), "gains an ability" (word-sense collision), generic burn
#: (burn-player without life-loss-matters) — all fail corroboration on the
#: trigger side when the overall set is broad enough, diluting the rate.
#: The 50% threshold is conservative enough to permit a few edge-case cards
#: (DFCs, modal spells) while rejecting any query whose captured set is
#: dominated by non-corroborating cards.
CORROBORATION_THRESHOLD: float = 0.5

#: Soft breadth ceiling for synth verification.
#:
#: A synth query that passes all hard checks (RE2-pasteable, resolves, expected-
#: hit, text-anchor) but captures more than this many cards is flagged as "too
#: broad" rather than terminally accepted.  The gate_and_persist caller may
#: attempt a single reroute (tighten the query or promote to an otag-backed
#: edge) before accepting or rejecting.
SYNTH_BREADTH_WARN: int = 100


@dataclass(frozen=True)
class VerificationResult:
    """Outcome of the three-check verification gate.

    Attributes
    ----------
    passed
        ``True`` iff all three checks passed.
    pasteability_ok
        Check (a): the query compiles and all regex patterns pass the HELD
        RE2 pasteability guard in ``query.py``.
    resolves
        Check (b): the query resolves to >= 1 card.
    hit_count
        Number of cards captured.
    expected_cards_hit
        Check (b): all expected source card names appear in the captured set.
    corroboration_rate
        Check (c): fraction of captured cards that corroborate (lake otags
        intersect BOTH trigger- and effect-event role otag sets).
    threshold
        The corroboration threshold applied.
    failing_cards
        Names of captured cards that did NOT corroborate (debugging aid).
    substrate
        Which resolution substrate was used (``'scryfall'``, ``'lake'``,
        ``'fixture'``, etc.).
    error
        Error message if a check failed exceptionally.
    """

    passed: bool
    pasteability_ok: bool
    resolves: bool
    hit_count: int
    expected_cards_hit: bool
    corroboration_rate: float
    threshold: float
    failing_cards: list[str]
    substrate: str
    error: str | None = None
    synth_bypass: bool = False
    breadth_warning: bool = False


#: Regex to extract ``/.../`` bodies from ``oracle:`` and ``type:`` atoms.
_REGEX_BODY_RE = _re_mod.compile(r'(?:oracle|type):/([^/]*)/')

#: Regex to detect ``'...'`` literal bodies from ``oracle:`` and ``type:`` atoms.
_LITERAL_BODY_RE = _re_mod.compile(r"(?:oracle|type):'([^']+)'")


def verify(
    query_str: str,
    role: tuple[str, str],
    *,
    expected_card_names: list[str] | None = None,
    search_client: SearchClient | None = None,
    threshold: float = CORROBORATION_THRESHOLD,
    decision: str = 'edge',
) -> VerificationResult:
    """Three-check verification gate for an authored within-card edge query.

    Checks
    ------
    (a) **Pasteability** — the query compiles and every regex body passes the
        HELD RE2 guard (``_assert_re2_pasteable``).
    (b) **Resolves + expected hit** — the query captures >= 1 card and (if
        *expected_card_names* is given) includes the source card(s).
    (c) **Over-capture corroboration** — at least *threshold* fraction of
        captured cards have lake otags intersecting BOTH the trigger-event's
        ``triggers`` AND the effect-event's ``produced_by``.

    For ``decision='synth'``, check (c) is **bypassed** in favour of a
    regex/literal-specificity check.  A synth is by definition a mechanic with
    no otag, so otag corroboration cannot run; the right gate is RE2-pasteable +
    resolves-on-Scryfall + hits the source card + the query has at least one
    ``oracle:`` or ``type:`` text anchor (regex ``/.../`` or literal ``'...'``).
    This keeps the normal ``decision='edge'`` corroboration gate strong (edges
    reference existing otags and MUST still corroborate).

    Parameters
    ----------
    query_str
        Scryfall-pasteable query string.
    role
        ``(trigger_event, produced_event)`` — event-catalog slugs.
    expected_card_names
        If given, check (b) requires these names in the captured set
        (case-insensitive match).
    search_client
        Injectable ``(query_str) -> list[dict]``.  Tests inject
        :func:`make_fixture_search_client`; production uses the lake or
        Scryfall.  If ``None``, falls back to the lake search client.
    threshold
        Corroboration threshold (default :data:`CORROBORATION_THRESHOLD`).
    decision
        ``'edge'`` (default) for normal otag-corroborated edge queries.
        ``'synth'`` for mechanic-with-no-otag queries — bypasses
        corroboration in favour of text-anchor specificity.
    """
    failing_cards: list[str] = []
    trigger_event, produced_event = role

    # ---- Check (a): Pasteability ----------------------------------------
    try:
        compile_query(query_str)
        # Explicit RE2 guard on each regex body.  compile_query already runs
        # it inside _atom_pred, but the explicit call documents the contract.
        for body in _REGEX_BODY_RE.findall(query_str):
            _assert_re2_pasteable(body)
        pasteability_ok = True
    except ValueError as exc:
        return VerificationResult(
            passed=False,
            pasteability_ok=False,
            resolves=False,
            hit_count=0,
            expected_cards_hit=False,
            corroboration_rate=0.0,
            threshold=threshold,
            failing_cards=[],
            substrate='none',
            error=f'Pasteability check failed: {exc}',
        )

    # ---- Check (b): Resolves + expected hit -----------------------------
    client = search_client or make_lake_search_client()
    substrate = getattr(client, '_substrate', 'injected')

    try:
        captured = client(query_str)
    except Exception as exc:
        return VerificationResult(
            passed=False,
            pasteability_ok=True,
            resolves=False,
            hit_count=0,
            expected_cards_hit=False,
            corroboration_rate=0.0,
            threshold=threshold,
            failing_cards=[],
            substrate=substrate,
            error=f'Resolution failed: {exc}',
        )

    hit_count = len(captured)
    resolves = hit_count > 0

    if not resolves:
        return VerificationResult(
            passed=False,
            pasteability_ok=True,
            resolves=False,
            hit_count=0,
            expected_cards_hit=False,
            corroboration_rate=0.0,
            threshold=threshold,
            failing_cards=[],
            substrate=substrate,
            error='Query captured 0 cards.',
        )

    # Expected-card check.
    expected_hit = True
    if expected_card_names:
        captured_lower = {c.get('name', '').lower() for c in captured}
        expected_hit = all(n.lower() in captured_lower for n in expected_card_names)

    # ---- Check (c): Over-capture corroboration --------------------------
    #
    # Synth-gate bypass (decision='synth' ONLY):
    #
    # A synth is by definition a mechanic with no otag, so otag corroboration
    # cannot run.  For synth decisions, the right gate is RE2-pasteable +
    # resolves + hits the source card + the query contains at least one oracle
    # or type TEXT ANCHOR (regex ``/.../`` or literal ``'...'``).  This keeps
    # the normal ``decision='edge'`` corroboration gate strong: edge queries
    # reference existing otags and MUST still corroborate, even when the event
    # is novel or the cards happen to have no otags.
    if decision == 'synth':
        has_regex = bool(_REGEX_BODY_RE.search(query_str))
        has_literal = bool(_LITERAL_BODY_RE.search(query_str))
        text_anchor = has_regex or has_literal
        hard_passed = pasteability_ok and resolves and expected_hit and text_anchor

        # Soft over-breadth gate: all hard checks pass but too many hits.
        # breadth_warning is ONLY set when hard checks pass — a hard failure
        # (missing text anchor, etc.) takes priority.
        broad = hard_passed and hit_count > SYNTH_BREADTH_WARN
        synth_passed = hard_passed and not broad

        if broad:
            error_msg = f'Synth breadth warning: query captures {hit_count} cards (> {SYNTH_BREADTH_WARN})'
        elif not hard_passed:
            error_msg = 'Synth bypass: query lacks oracle/type text anchor'
        else:
            error_msg = None

        return VerificationResult(
            passed=synth_passed,
            pasteability_ok=pasteability_ok,
            resolves=resolves,
            hit_count=hit_count,
            expected_cards_hit=expected_hit,
            corroboration_rate=-1.0,  # sentinel: synth bypass, not computed
            threshold=threshold,
            failing_cards=[],
            substrate=substrate,
            synth_bypass=True,
            breadth_warning=broad,
            error=error_msg,
        )

    events = _load_events()
    trigger_expected = frozenset(events.get(trigger_event, {}).get('triggers', []))
    effect_expected = frozenset(events.get(produced_event, {}).get('produced_by', []))

    corroborating = 0
    for card in captured:
        card_otags = frozenset(t.lower() for t in (card.get('otags') or []))
        if (card_otags & trigger_expected) and (card_otags & effect_expected):
            corroborating += 1
        else:
            failing_cards.append(card.get('name', '<unknown>'))

    rate = corroborating / hit_count
    corr_ok = rate >= threshold

    passed = pasteability_ok and resolves and expected_hit and corr_ok

    return VerificationResult(
        passed=passed,
        pasteability_ok=pasteability_ok,
        resolves=resolves,
        hit_count=hit_count,
        expected_cards_hit=expected_hit,
        corroboration_rate=rate,
        threshold=threshold,
        failing_cards=failing_cards,
        substrate=substrate,
    )


# ---------------------------------------------------------------------------
# Search-client factories
# ---------------------------------------------------------------------------


def make_fixture_search_client(
    cards: list[dict],
    substrate_label: str = 'fixture',
) -> SearchClient:
    """Hermetic search client backed by an in-memory card list.

    Runs the compiled query predicate against each card's :class:`CardView`.
    No lake, no network — for tests.
    """

    def search(query_str: str) -> list[dict]:
        pred = compile_query(query_str)
        return [c for c in cards if pred(CardView.from_card(c))]

    search._substrate = substrate_label  # type: ignore[attr-defined]
    return search


def make_lake_search_client() -> SearchClient:
    """Search client backed by the local DuckDB lake.

    Loads ``oracle_cards.parquet`` + ``card_otag.parquet``, compiles the query
    predicate, and scans all ~40k cards.  No network.
    """

    def search(query_str: str) -> list[dict]:
        return _lake_search(query_str)

    search._substrate = 'lake'  # type: ignore[attr-defined]
    return search


def make_scryfall_search_client(
    *,
    lake_fallback: bool = True,
    pace_ms: int = 100,
) -> SearchClient:
    """Scryfall API client, enriched with lake otags for corroboration.

    Calls ``https://api.scryfall.com/cards/search`` then looks up each result
    card's otags in the local lake (by ``oracle_id``).  Falls back to the pure
    lake client if Scryfall is unreachable and *lake_fallback* is ``True``.

    Parameters
    ----------
    lake_fallback
        Fall back to lake-only if Scryfall is unreachable.
    pace_ms
        Minimum ms between Scryfall API calls (rate-limit courtesy).
    """

    def search(query_str: str) -> list[dict]:
        resolved = _scryfall_resolve(query_str, pace_ms=pace_ms)
        if resolved is None:
            if not lake_fallback:
                raise ConnectionError('Scryfall unreachable and lake_fallback=False')
            search._substrate = 'lake'  # type: ignore[attr-defined]
            return _lake_search(query_str)

        search._substrate = 'scryfall'  # type: ignore[attr-defined]
        # Enrich with lake otags for the corroboration check.
        try:
            otag_map = _load_lake_otag_by_oracle_id()
        except FileNotFoundError:
            return resolved  # no lake -> otags stay empty
        for card in resolved:
            card['otags'] = otag_map.get(card.get('oracle_id', ''), [])
        return resolved

    search._substrate = 'scryfall'  # type: ignore[attr-defined]
    return search


# ---------------------------------------------------------------------------
# Private lake + Scryfall helpers
# ---------------------------------------------------------------------------


def _lake_search(query_str: str) -> list[dict]:
    """Run a compiled query against the local lake.  Returns matching cards
    with otags attached."""
    import duckdb

    data_dir = Path.home() / '.local' / 'share' / 'make-magic'
    oracle_path = data_dir / 'raw' / 'oracle_cards.parquet'
    otag_path = data_dir / 'normalized' / 'card_otag.parquet'

    if not oracle_path.exists() or not otag_path.exists():
        raise FileNotFoundError(f'Lake data not found at {data_dir}')

    pred = compile_query(query_str)
    con = duckdb.connect(':memory:')
    try:
        # Build oracle_id -> otag list.
        otag_rows = con.execute(
            f"SELECT CAST(oracle_id AS VARCHAR) AS oid, slug FROM read_parquet('{otag_path}')"
        ).fetchall()
        otag_map: dict[str, list[str]] = {}
        for oid, tag in otag_rows:
            otag_map.setdefault(oid, []).append(tag)

        # Scan cards.
        card_rows = con.execute(
            f'SELECT '
            f'  CAST(oracle_id AS VARCHAR) AS oracle_id, '
            f'  name, '
            f"  COALESCE(oracle_text, '') AS oracle_text, "
            f"  COALESCE(type_line, '')   AS type_line, "
            f"  COALESCE(mana_cost, '')   AS mana_cost, "
            f'  keywords '
            f"FROM read_parquet('{oracle_path}')"
        ).fetchall()
    finally:
        con.close()

    results: list[dict] = []
    for oracle_id, name, oracle_text, type_line, mana_cost, keywords in card_rows:
        kw_list = list(keywords) if keywords else []
        otags = otag_map.get(oracle_id, [])
        card = {
            'oracle_id': oracle_id,
            'name': name,
            'oracle_text': oracle_text,
            'type_line': type_line,
            'mana_cost': mana_cost,
            'keywords': kw_list,
            'otags': otags,
        }
        view = CardView.from_card(card)
        if pred(view):
            results.append(card)

    return results


def _scryfall_resolve(
    query_str: str,
    *,
    pace_ms: int = 100,
) -> list[dict] | None:
    """Call Scryfall search API.  Returns ``None`` if unreachable."""
    url = 'https://api.scryfall.com/cards/search?q=' + urllib.parse.quote(query_str, safe='')
    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'make-magic/1.0',
            'Accept': 'application/json',
        },
    )
    time.sleep(pace_ms / 1000.0)  # rate-limit courtesy

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    if data.get('object') != 'list':
        return []

    cards: list[dict] = []
    for c in data.get('data', []):
        cards.append(
            {
                'name': c.get('name', ''),
                'oracle_id': c.get('oracle_id', ''),
                'oracle_text': c.get('oracle_text', ''),
                'type_line': c.get('type_line', ''),
                'mana_cost': c.get('mana_cost', ''),
                'keywords': c.get('keywords', []),
                'otags': [],  # Scryfall search does not return community otags
            }
        )
    return cards


@lru_cache(maxsize=1)
def _load_lake_otag_by_oracle_id() -> dict[str, list[str]]:
    """Load ``oracle_id -> [otag, ...]`` from the lake (cached)."""
    import duckdb

    data_dir = Path.home() / '.local' / 'share' / 'make-magic'
    otag_path = data_dir / 'normalized' / 'card_otag.parquet'
    if not otag_path.exists():
        raise FileNotFoundError(f'card_otag.parquet not found at {otag_path}')

    con = duckdb.connect(':memory:')
    try:
        rows = con.execute(
            f"SELECT CAST(oracle_id AS VARCHAR) AS oid, slug FROM read_parquet('{otag_path}')"
        ).fetchall()
    finally:
        con.close()

    result: dict[str, list[str]] = {}
    for oid, tag in rows:
        result.setdefault(oid, []).append(tag)
    return result
