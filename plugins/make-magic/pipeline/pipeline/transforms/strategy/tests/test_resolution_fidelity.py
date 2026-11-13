"""Tests for resolution-fidelity fixes (#68 DFC oracle_text, #69 art-series first-row-wins).

Bug #68: scryfall_bulk._project drops oracle_text for DFC/card_faces cards.
Bug #69: corpus.Lake and load_card_meta resolve to art-series (tagless) rows
         when they precede the real card in oracle_cards.parquet.

These tests are written FIRST (TDD red phase) against the current buggy code,
and must all FAIL until the fixes are applied.
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Bug #68 — DFC oracle_text dropped by _project
# ---------------------------------------------------------------------------

class TestDfcOracleTextProject:
    """scryfall_bulk._project must join card_faces oracle_text for DFCs."""

    def _make_dfc_card(self) -> dict:
        """A synthetic Scryfall card dict that mimics a real DFC (no top-level oracle_text)."""
        return {
            'oracle_id': '00000000-0000-0000-0000-000000000001',
            'name': 'Esika, God of the Tree // The Prismatic Bridge',
            'cmc': 3.0,
            'mana_cost': '{1}{G}{G}',
            'type_line': 'Legendary Creature — God // Legendary Enchantment',
            'colors': ['G'],
            'color_identity': ['B', 'G', 'R', 'U', 'W'],
            'produced_mana': ['B', 'G', 'R', 'U', 'W'],
            'keywords': [],
            'oracle_text': None,  # DFCs have no top-level oracle_text
            'power': '1',
            'toughness': '4',
            'image_uris': {'art_crop': 'https://example.com/art.jpg', 'normal': 'https://example.com/normal.jpg'},
            'scryfall_uri': 'https://scryfall.com/card/khm/168/esika-god-of-the-tree-the-prismatic-bridge',
            'set_name': 'Kaldheim',
            'card_faces': [
                {
                    'name': 'Esika, God of the Tree',
                    'oracle_text': '{T}: Add one mana of any color.\nOther legendary creatures you control have vigilance and "{T}: Add one mana of any color."',
                    'mana_cost': '{1}{G}{G}',
                    'type_line': 'Legendary Creature — God',
                    'power': '1',
                    'toughness': '4',
                },
                {
                    'name': 'The Prismatic Bridge',
                    'oracle_text': 'At the beginning of your upkeep, reveal cards from the top of your library until you reveal a creature or planeswalker card. Put that card onto the battlefield and the rest on the bottom of your library in a random order.',
                    'mana_cost': '{W}{U}{B}{R}{G}',
                    'type_line': 'Legendary Enchantment',
                },
            ],
        }

    def test_project_preserves_dfc_oracle_text(self):
        """_project must extract oracle_text from card_faces when top-level is absent."""
        from pipeline.sources.scryfall_bulk import _project

        card = self._make_dfc_card()
        projected = _project(card)

        assert projected['oracle_text'] is not None, (
            'DFC oracle_text must not be None after projection'
        )
        assert projected['oracle_text'] != '', (
            'DFC oracle_text must not be empty after projection'
        )
        # Should contain the front face text at minimum
        assert 'Add one mana of any color' in projected['oracle_text']

    def test_project_joins_both_faces(self):
        """_project should include text from both faces of a DFC."""
        from pipeline.sources.scryfall_bulk import _project

        card = self._make_dfc_card()
        projected = _project(card)

        # Both faces' text should be present
        assert 'Add one mana of any color' in projected['oracle_text']
        assert 'reveal cards from the top' in projected['oracle_text']

    def test_project_preserves_single_face_oracle_text(self):
        """_project must not break normal (single-faced) cards that have top-level oracle_text."""
        from pipeline.sources.scryfall_bulk import _project

        card = {
            'oracle_id': '00000000-0000-0000-0000-000000000002',
            'name': 'Lightning Bolt',
            'cmc': 1.0,
            'mana_cost': '{R}',
            'type_line': 'Instant',
            'colors': ['R'],
            'color_identity': ['R'],
            'produced_mana': [],
            'keywords': [],
            'oracle_text': 'Lightning Bolt deals 3 damage to any target.',
            'power': None,
            'toughness': None,
            'image_uris': {},
            'scryfall_uri': 'https://scryfall.com/card/leb/162/lightning-bolt',
            'set_name': 'Limited Edition Beta',
        }
        projected = _project(card)
        assert projected['oracle_text'] == 'Lightning Bolt deals 3 damage to any target.'


# ---------------------------------------------------------------------------
# Bug #69 — Art-series first-row-wins in Lake and load_card_meta
# ---------------------------------------------------------------------------

def _make_test_parquets(tmp_path: Path) -> Path:
    """Build minimal oracle_cards + card_otag parquets that reproduce the art-series bug.

    Creates two oracle_cards entries for 'Baral, Chief of Compliance':
      1. An art-series row (Card // Card) with a different oracle_id — listed FIRST
      2. The real card — listed SECOND

    And card_otag entries only for the real card's oracle_id.
    """
    import duckdb

    raw_dir = tmp_path / 'raw'
    raw_dir.mkdir(parents=True)
    norm_dir = tmp_path / 'normalized'
    norm_dir.mkdir(parents=True)

    con = duckdb.connect(':memory:')

    # oracle_cards: art-series FIRST, then real card
    oracle_data = [
        {
            'oracle_id': 'aaaaaaaa-0000-0000-0000-art000000001',
            'name': 'Baral, Chief of Compliance // Baral, Chief of Compliance',
            'cmc': 0.0,
            'mana_cost': None,
            'type_line': 'Card // Card',
            'colors': [],
            'color_identity': [],
            'produced_mana': [],
            'keywords': [],
            'oracle_text': None,
            'power': None,
            'toughness': None,
            'art_crop': None,
            'image_normal': None,
            'scryfall_uri': None,
            'set_name': 'Art Series: Aether Revolt',
        },
        {
            'oracle_id': 'bbbbbbbb-0000-0000-0000-real00000001',
            'name': 'Baral, Chief of Compliance',
            'cmc': 2.0,
            'mana_cost': '{1}{U}',
            'type_line': 'Legendary Creature — Human Wizard',
            'colors': ['U'],
            'color_identity': ['U'],
            'produced_mana': [],
            'keywords': [],
            'oracle_text': 'Instant and sorcery spells you cast cost {1} less to cast.\nWhenever a spell or ability you control counters a spell, you may draw a card. If you do, discard a card.',
            'power': '1',
            'toughness': '3',
            'art_crop': 'https://example.com/baral_art.jpg',
            'image_normal': 'https://example.com/baral.jpg',
            'scryfall_uri': 'https://scryfall.com/card/aer/28/baral-chief-of-compliance',
            'set_name': 'Aether Revolt',
        },
    ]
    tmp_json = raw_dir / '_test_oracle.json'
    tmp_json.write_text(json.dumps(oracle_data))
    con.execute(f"COPY (SELECT * FROM read_json('{tmp_json}')) TO '{raw_dir / 'oracle_cards.parquet'}' (FORMAT PARQUET)")
    tmp_json.unlink()

    # card_otag: tags ONLY for the real card's oracle_id
    otag_data = [
        {'oracle_id': 'bbbbbbbb-0000-0000-0000-real00000001', 'slug': 'cost-reduction'},
        {'oracle_id': 'bbbbbbbb-0000-0000-0000-real00000001', 'slug': 'counters-matter'},
        {'oracle_id': 'bbbbbbbb-0000-0000-0000-real00000001', 'slug': 'card-draw'},
    ]
    tmp_json = norm_dir / '_test_otag.json'
    tmp_json.write_text(json.dumps(otag_data))
    con.execute(f"COPY (SELECT * FROM read_json('{tmp_json}')) TO '{norm_dir / 'card_otag.parquet'}' (FORMAT PARQUET)")
    tmp_json.unlink()

    return tmp_path


class TestLakeArtSeriesResolution:
    """Lake must prefer the real (tagged) printing over an art-series duplicate."""

    def test_resolve_prefers_tagged_oracle_id(self, tmp_path: Path):
        """When art-series and real card share a name, resolve picks the real oracle_id."""
        from pipeline.transforms.strategy.corpus import Lake

        root = _make_test_parquets(tmp_path)
        lake = Lake(root=root)

        oid, type_line, slugs = lake.resolve('Baral, Chief of Compliance')

        assert oid is not None, 'Baral should resolve'
        assert type_line != 'Card // Card', (
            f'Resolved to art-series type_line: {type_line}'
        )
        assert 'Legendary Creature' in type_line
        assert oid == 'bbbbbbbb-0000-0000-0000-real00000001', (
            f'Resolved to art-series oracle_id: {oid}'
        )

    def test_resolve_returns_nonempty_slugs(self, tmp_path: Path):
        """Resolved slugs must be non-empty for a card that has tags."""
        from pipeline.transforms.strategy.corpus import Lake

        root = _make_test_parquets(tmp_path)
        lake = Lake(root=root)

        oid, type_line, slugs = lake.resolve('Baral, Chief of Compliance')

        assert len(slugs) > 0, (
            f'Baral should have otag slugs, got empty set (resolved oid={oid})'
        )
        assert 'cost-reduction' in slugs


class TestLoadCardMetaArtSeries:
    """load_card_meta must prefer the real card's meta over art-series."""

    def test_meta_has_real_oracle_text(self, tmp_path: Path):
        """load_card_meta for a name with both art-series and real rows returns real oracle_text."""
        from pipeline.transforms.strategy.corpus import load_card_meta, _norm

        root = _make_test_parquets(tmp_path)
        meta = load_card_meta(root=root)

        key = _norm('Baral, Chief of Compliance')
        assert key in meta, f'Baral not found in meta (keys={list(meta.keys())[:5]})'
        rec = meta[key]

        assert rec['oracle_text'] != '', (
            'oracle_text should be the real card text, not empty art-series'
        )
        assert 'cost' in rec['oracle_text'].lower(), (
            f'Expected cost-reduction text, got: {rec["oracle_text"][:80]}'
        )

    def test_meta_has_real_cmc(self, tmp_path: Path):
        """load_card_meta must return the real card's CMC, not the art-series 0.0."""
        from pipeline.transforms.strategy.corpus import load_card_meta, _norm

        root = _make_test_parquets(tmp_path)
        meta = load_card_meta(root=root)

        key = _norm('Baral, Chief of Compliance')
        rec = meta[key]
        assert rec['cmc'] == 2.0, f'Expected CMC 2.0 (real Baral), got {rec["cmc"]}'


# ---------------------------------------------------------------------------
# Bug #69 — Integration: affected gauntlet commander resolves correctly
# ---------------------------------------------------------------------------

def _check_lake() -> bool:
    """Check if the lake parquets are available at the configured or default path."""
    base = os.environ.get('MAKE_MAGIC_DATA_DIR')
    root = Path(base) if base else (Path.home() / '.local/share/make-magic')
    return (root / 'raw' / 'oracle_cards.parquet').exists() and (
        root / 'normalized' / 'card_otag.parquet'
    ).exists()


_LAKE_AVAILABLE = _check_lake()


@pytest.mark.skipif(not _LAKE_AVAILABLE, reason='lake parquets not available')
class TestGauntletCommanderResolution:
    """Known-affected gauntlet commanders (#69) must resolve with real otags from the live lake."""

    # 22 known-affected commanders from the bug report
    AFFECTED_COMMANDERS = [
        'Baral, Chief of Compliance',
        "K'rrik, Son of Yawgmoth",
        'Yawgmoth, Thran Physician',
        'Urza, Lord High Artificer',
        'Godo, Bandit Warlord',
        'Tasha, the Witch Queen',
        'Emry, Lurker of the Loch',
        'Omnath, Locus of Mana',
        'Giada, Font of Hope',
        'Zada, Hedron Grinder',
        'Braids, Arisen Nightmare',
        'Isshin, Two Heavens as One',
        'Aang, at the Crossroads',
        'Rafiq of the Many',
        'Ramos, Dragon Engine',
        'Tovolar, Dire Overlord',
        'Shiko, Paragon of the Way',
        'Rakdos, the Muscle',
        'Ureni of the Unwritten',
    ]

    def test_affected_commanders_resolve_with_tags(self):
        """Every known-affected commander must resolve with non-empty otags."""
        from pipeline.transforms.strategy.corpus import Lake

        lake = Lake()
        empty = []
        for name in self.AFFECTED_COMMANDERS:
            oid, type_line, slugs = lake.resolve(name)
            if not slugs:
                empty.append(name)

        assert not empty, (
            f'{len(empty)} commanders still resolve with empty otags: {empty}'
        )

    def test_affected_commanders_not_art_series(self):
        """No known-affected commander should resolve to a Card // Card type_line."""
        from pipeline.transforms.strategy.corpus import Lake

        lake = Lake()
        art_series = []
        for name in self.AFFECTED_COMMANDERS:
            oid, type_line, slugs = lake.resolve(name)
            if type_line == 'Card // Card':
                art_series.append(name)

        assert not art_series, (
            f'{len(art_series)} commanders resolve to art-series: {art_series}'
        )


# ---------------------------------------------------------------------------
# Bug #68 — DFC oracle_text populated from card_faces after data refresh
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _LAKE_AVAILABLE, reason='lake parquets not available')
class TestDfcOracleTextInLake:
    """After re-ingesting oracle_cards, DFC cards must carry oracle_text from card_faces."""

    def test_ral_monsoon_mage_has_oracle_text(self):
        """Ral, Monsoon Mage (#68 gut-check) must have non-empty oracle_text from the lake."""
        from pipeline.transforms.strategy.corpus import load_card_meta, _norm

        meta = load_card_meta()
        key = _norm('Ral, Monsoon Mage // Ral, Leyline Prodigy')
        assert key in meta, f'Ral not found in card_meta (lake stale?)'
        rec = meta[key]
        assert rec['oracle_text'], (
            'Ral, Monsoon Mage must have non-empty oracle_text after #68 data fix '
            '(was None when card_faces oracle_text was not joined)'
        )
        assert 'instant and sorcery' in rec['oracle_text'].lower(), (
            f'Expected Ral spellslinger text, got: {rec["oracle_text"][:80]}'
        )

    def test_birgi_has_oracle_text(self):
        """Birgi, God of Storytelling (transform DFC) must have oracle_text from the lake."""
        from pipeline.transforms.strategy.corpus import load_card_meta, _norm

        meta = load_card_meta()
        key = _norm('Birgi, God of Storytelling')
        assert key in meta, f'Birgi not found in card_meta'
        rec = meta[key]
        assert rec['oracle_text'], (
            'Birgi must have non-empty oracle_text after #68 data fix'
        )

    def test_known_dfcs_have_oracle_text(self):
        """A sample of known DFCs must all have non-empty oracle_text from the lake."""
        from pipeline.transforms.strategy.corpus import load_card_meta, _norm

        meta = load_card_meta()
        known_dfcs = [
            'Birgi, God of Storytelling',
            'Esika, God of the Tree',
            'Valki, God of Lies',
            'Shatterskull Smashing',
            'Hagra Mauling',
            'Realm-Cloaked Giant',
        ]
        empty = []
        for name in known_dfcs:
            key = _norm(name)
            rec = meta.get(key)
            if rec is None or not rec['oracle_text']:
                empty.append(name)

        assert not empty, (
            f'{len(empty)} known DFCs still have empty oracle_text: {empty}'
        )


# ---------------------------------------------------------------------------
# Full-corpus canonical SHA (lake-gated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not _LAKE_AVAILABLE, reason='lake parquets not available')
class TestFullCorpusCanonicalSha:
    """The canonical full-corpus SHA must match the recorded baseline."""

    def test_full_corpus_sha_matches_baseline(self):
        """full_corpus_sha() must equal the committed FULL_CORPUS_SHA constant."""
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            full_corpus_sha,
            FULL_CORPUS_SHA,
        )

        computed = full_corpus_sha()
        assert computed == FULL_CORPUS_SHA, (
            f'Full-corpus SHA drift: recorded {FULL_CORPUS_SHA[:16]}... '
            f'!= computed {computed[:16]}...'
        )

    def test_full_corpus_record_count(self):
        """The gauntlet corpus must contain exactly 170 decks."""
        from pipeline.transforms.strategy.run import recalc_corpus

        records = recalc_corpus()
        assert len(records) == 170, (
            f'Expected 170 gauntlet decks, got {len(records)}'
        )
