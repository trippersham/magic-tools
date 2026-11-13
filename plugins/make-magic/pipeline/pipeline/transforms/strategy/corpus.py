"""Lake-backed corpus loaders for the full-corpus recalc (T7).

Resolves Forge ``.dck`` gauntlet decks and author-written owned-deck JSON against
the DuckDB lake (oracle_cards + the rolled-up card_otag), and attaches the extra
per-card meta the engine's T4/T5/T5.5 readings need (cmc, keywords,
color_identity, mana_cost, oracle_id). DuckDB is imported lazily so the pure
engine + the whole offline test suite never require it.

This is the ONLY part of the subpackage that touches the lake; the engine itself
(:func:`~.resolve.resolve`) is pure over a deck dict + a combo list.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

log = logging.getLogger('make_magic.transforms.strategy.corpus')

#: In-repo gauntlet root (relative to the pipeline package).
GAUNTLET_DIR = Path(__file__).resolve().parents[2] / 'data' / 'gauntlet' / 'commander'

_BUNDLE_BRACKET = {'casual': 2, 'mid': 3, 'cedh': 5, 'precons': 2}


def lake_root() -> Path:
    """Resolve the DuckDB lake root (``pipeline.store`` → ``MAKE_MAGIC_DATA_DIR`` → XDG)."""
    try:
        from pipeline import store

        return store.StorePaths.resolve().data_dir
    except Exception:
        base = os.environ.get('MAKE_MAGIC_DATA_DIR')
        return Path(base) if base else (Path.home() / '.local/share/make-magic')


def _norm(name: str) -> str:
    s = name.strip().lower()
    s = s.split('|')[0].strip()
    s = s.rstrip('+ ').strip()
    return re.sub(r'\s+', ' ', s)


class Lake:
    """Name → oracle_id → slug/type resolution, loaded once from the lake.

    Resolution prefers the *canonical* (most-tagged, non-art-series) printing
    when multiple oracle_cards rows share the same normalized name (#69).
    Art-series duplicates (``type_line == 'Card // Card'``) have a distinct
    oracle_id with zero otag taggings; a naive first-row-wins ``setdefault``
    would shadow the real card whenever the art-series row comes first in the
    parquet scan order.
    """

    #: Art-series type_line constant used by Scryfall for art cards.
    _ART_SERIES_TYPE = 'Card // Card'

    def __init__(self, root: Path | None = None) -> None:
        import duckdb

        root = root or lake_root()
        oracle = root / 'raw' / 'oracle_cards.parquet'
        card_otag = root / 'normalized' / 'card_otag.parquet'
        con = duckdb.connect(':memory:')

        # --- Load otag counts first so the name→oid loop can rank candidates. ---
        tags = con.execute(
            f"SELECT oracle_id, slug FROM read_parquet('{card_otag}')"
        ).fetchall()
        self.oid2slugs: dict[str, set[str]] = {}
        for oid, slug in tags:
            self.oid2slugs.setdefault(str(oid), set()).add(slug)

        # --- Build name→oid, preferring tagged/non-art-series rows. ---
        orc = con.execute(
            f"SELECT oracle_id, name, type_line, oracle_text FROM read_parquet('{oracle}')"
        ).fetchall()
        self.name2oid: dict[str, str] = {}
        self.oid2type: dict[str, str] = {}
        # Track the "quality score" of the current winner per name so a better
        # candidate can override it.
        _name_score: dict[str, int] = {}

        for oid, name, tl, otxt in orc:
            oid = str(oid)
            self.oid2type[oid] = tl or ''
            score = self._rank(oid, tl, otxt)
            self._register_name(_norm(name), oid, score, _name_score)
            if '//' in name:
                self._register_name(_norm(name.split('//')[0]), oid, score, _name_score)

    # ------------------------------------------------------------------

    @staticmethod
    def _rank(oid: str, type_line: str | None, oracle_text: str | None) -> int:
        """Score a candidate row: higher = more likely the canonical printing."""
        # The ranking intentionally does NOT reference oid2slugs via self
        # because _rank is called during __init__ before self is fully built;
        # instead the caller computes the tag-count component externally.
        s = 0
        if type_line and type_line != 'Card // Card':
            s += 4  # Not art-series — strong signal
        if oracle_text:
            s += 2  # Has oracle text
        return s

    def _register_name(
        self, normed: str, oid: str, score: int, scores: dict[str, int]
    ) -> None:
        """Insert ``normed → oid`` only if ``score`` beats the incumbent."""
        # Augment score with tag count (available because oid2slugs is loaded first).
        full_score = score + (1 if self.oid2slugs.get(oid) else 0)
        prev = scores.get(normed, -1)
        if full_score > prev:
            self.name2oid[normed] = oid
            scores[normed] = full_score

    # ------------------------------------------------------------------

    def resolve(self, name: str) -> tuple[str | None, str, set[str]]:
        oid = self.name2oid.get(_norm(name))
        if oid is None:
            return None, '', set()
        slugs = self.oid2slugs.get(oid, set())
        if not slugs and self.oid2type.get(oid, '') != self._ART_SERIES_TYPE:
            log.warning(
                'corpus.Lake: %r resolved (oid=%s) with 0 otag slugs — '
                'possible data gap (card should have tags?).',
                name, oid[:12],
            )
        return oid, self.oid2type.get(oid, ''), slugs


def parse_dck(path: Path) -> dict:
    """Forge ``.dck`` → ``{name, _entries:[(name, is_commander)]}``."""
    section = None
    entries: list[tuple[str, bool]] = []
    for raw in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if line.startswith('['):
            if 'commander' in low:
                section = 'cmd'
            elif 'metadata' in low or 'sideboard' in low:
                section = 'meta'
            else:
                section = 'main'
            continue
        if section == 'meta' or line.startswith('Name='):
            continue
        m = re.match(r'^(?:SB:\s*)?(\d+)\s+(.+)$', line)
        if not m:
            continue
        qty, name = int(m.group(1)), m.group(2).strip()
        is_cmd = section == 'cmd' or low.startswith('sb:')
        entries.extend((name, is_cmd) for _ in range(qty))
    return {'name': path.stem, '_entries': entries}


def load_manifest(gauntlet_dir: Path) -> dict[str, dict]:
    man_path = gauntlet_dir / 'manifest.json'
    if not man_path.exists():
        return {}
    return {row['file_name']: row for row in json.loads(man_path.read_text())}


def gauntlet_decks(gauntlet_dir: Path = GAUNTLET_DIR) -> list[dict]:
    """All ``.dck`` decks with bracket labels (manifest.bracket, else bundle fallback)."""
    man = load_manifest(gauntlet_dir)
    out: list[dict] = []
    for bundle in ('casual', 'mid', 'cedh', 'precons'):
        d = gauntlet_dir / bundle
        if not d.is_dir():
            continue
        for p in sorted(d.glob('*.dck')):
            row = man.get(p.name)
            bracket = int(row['bracket']) if row and row.get('bracket') else _BUNDLE_BRACKET[bundle]
            commander = row['commander'] if row else None
            out.append({'path': p, 'bundle': bundle, 'bracket': bracket,
                        'commander_hint': commander, 'source': 'gauntlet'})
    for p in sorted(gauntlet_dir.glob('*.dck')):
        out.append({'path': p, 'bundle': 'loose', 'bracket': 3,
                    'commander_hint': None, 'source': 'gauntlet'})
    return out


def build_deck_from_dck(info: dict, lake: Lake) -> dict:
    parsed = parse_dck(info['path'])
    cards, unresolved = [], []
    for name, is_cmd in parsed['_entries']:
        oid, tl, slugs = lake.resolve(name)
        if oid is None:
            unresolved.append(name)
            tl = ''
            slugs = set()
        cards.append({'name': name, 'type_line': tl, 'otags': sorted(slugs),
                      'role': 'commander' if is_cmd else None})
    return {'name': parsed['name'], 'cards': cards, 'bracket': info['bracket'],
            'bundle': info['bundle'], 'commander_hint': info['commander_hint'],
            'unresolved': unresolved, 'source': 'gauntlet'}


def _meta_score(type_line: str | None, oracle_text: str | None) -> int:
    """Rank a card-meta candidate: higher = more canonical (#69)."""
    s = 0
    if type_line and type_line != 'Card // Card':
        s += 4
    if oracle_text:
        s += 2
    return s


def load_card_meta(root: Path | None = None) -> dict[str, dict]:
    """Read-only name → {oracle_text, cmc, keywords, color_identity, …} from the lake.

    When multiple rows share the same normalized name (e.g. a real card and an
    art-series duplicate), the most-canonical row wins (#69): prefer non-art-series
    rows with oracle_text present, matching ``Lake._rank``.
    """
    import duckdb

    root = root or lake_root()
    oracle = root / 'raw' / 'oracle_cards.parquet'
    con = duckdb.connect(':memory:')
    rows = con.execute(
        'SELECT name, oracle_text, cmc, keywords, color_identity, oracle_id, mana_cost, type_line '
        f"FROM read_parquet('{oracle}')"
    ).fetchall()
    meta: dict[str, dict] = {}
    _scores: dict[str, int] = {}
    for name, txt, cmc, kw, ci, oid, mc, tl in rows:
        rec = {'oracle_text': txt or '', 'cmc': float(cmc) if cmc is not None else 0.0,
               'keywords': list(kw) if kw is not None else [],
               'color_identity': list(ci) if ci is not None else [],
               'oracle_id': str(oid) if oid is not None else None,
               'mana_cost': mc or '', 'type_line_full': tl or ''}
        score = _meta_score(tl, txt)
        normed = _norm(name)
        if score > _scores.get(normed, -1):
            meta[normed] = rec
            _scores[normed] = score
        if '//' in name:
            front = _norm(name.split('//')[0])
            if score > _scores.get(front, -1):
                meta[front] = rec
                _scores[front] = score
    return meta


def attach_meta(deck: dict, meta: dict[str, dict]) -> dict:
    """Fill each card's engine-required meta from the lake (idempotent, in place)."""
    for c in deck['cards']:
        m = meta.get(_norm(c['name']))
        if not c.get('oracle_text'):
            c['oracle_text'] = (m or {}).get('oracle_text', '')
        if m:
            c.setdefault('cmc', m['cmc'])
            c.setdefault('keywords', m['keywords'])
            c.setdefault('color_identity', m['color_identity'])
            if not c.get('mana_cost'):
                c['mana_cost'] = m.get('mana_cost', '')
            if not c.get('type_line'):
                c['type_line'] = m.get('type_line_full', '')
            if m.get('oracle_id') and not c.get('oracle_id'):
                c['oracle_id'] = m['oracle_id']
    return deck


def load_owned(owned_dir: Path) -> list[dict]:
    """Load author-written owned-deck JSON (bracket defaulted to 3, bundle=owned)."""
    out: list[dict] = []
    for p in sorted(Path(owned_dir).glob('*.json')):
        d = json.loads(p.read_text())
        d.setdefault('bracket', 3)
        d['bundle'] = 'owned'
        d['source'] = 'owned'
        d.setdefault('unresolved', [])
        out.append(d)
    return out
