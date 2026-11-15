"""Tests for the engine cache store (task 7.4)."""

from __future__ import annotations

from pipeline.transforms.strategy import engine_cache as ec
from pipeline.transforms.strategy.engine_cache import (
    SYNTHESIS_VERSION,
    EngineCacheRecord,
    ResolvedEngine,
    input_hash,
    load_cache,
    make_record,
    persist,
    persist_batch,
)


def _engine() -> ResolvedEngine:
    return ResolvedEngine(
        strategy='Artifact Mana-Loop Combo',
        win_condition='Infinite mana -> Urza win.',
        key_pieces=('Urza, Lord High Artificer', 'Isochron Scepter'),
        confidence='HIGH',
    )


def _input(**over) -> dict:
    base = {
        'slug': 'gauntlet__urza',
        'deck_name': 'Urza',
        'commanders': [{'name': 'Urza', 'oracle_text': 'x'}],
        'elected': [{'event': 'mana-produced', 'mass': 35}],
        'archetype_identity': [{'strategy': 'artifacts', 'share': 0.49}],
        'detected_combos': [],
        'commander_loop': None,
        'naming_resolution': {'named_strategy': 'artifacts'},
        'n_nonland': 60,
        'n_cards': 100,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# input_hash
# ---------------------------------------------------------------------------


def test_input_hash_is_stable_and_order_independent():
    a = _input()
    b = dict(reversed(list(_input().items())))
    assert input_hash(a) == input_hash(b)


def test_input_hash_ignores_deck_name_and_internal_keys():
    base = _input()
    renamed = _input(deck_name='Totally Different Name')
    flagged = _input(_sparse=True, _failed=False)
    assert input_hash(base) == input_hash(renamed)
    assert input_hash(base) == input_hash(flagged)


def test_input_hash_changes_on_engine_input_change():
    """A B1/B6-style change to the elected set must bust the hash."""
    base = _input()
    changed = _input(elected=[{'event': 'artifact-enters', 'mass': 67}])
    assert input_hash(base) != input_hash(changed)


# ---------------------------------------------------------------------------
# ResolvedEngine round-trip
# ---------------------------------------------------------------------------


def test_resolved_engine_round_trip():
    e = _engine()
    assert ResolvedEngine.from_dict(e.as_dict()) == e


def test_record_round_trip():
    rec = make_record('gauntlet__urza', 'Urza', _input(), _engine(), model_id='claude-sonnet-4-5')
    assert EngineCacheRecord.from_record(rec.to_record()) == rec


# ---------------------------------------------------------------------------
# persist / load / idempotency
# ---------------------------------------------------------------------------


def test_persist_and_load(tmp_path):
    rec = make_record('gauntlet__urza', 'Urza', _input(), _engine(), model_id='m')
    persist(rec, tmp_path)
    cache = load_cache(tmp_path)
    assert not cache.is_empty
    loaded = cache.get('gauntlet__urza')
    assert loaded is not None
    assert loaded.engine == _engine()


def test_persist_is_idempotent(tmp_path):
    rec = make_record('gauntlet__urza', 'Urza', _input(), _engine(), model_id='m')
    persist(rec, tmp_path)
    persist(rec, tmp_path)  # same input_hash + version -> no-op
    store = (tmp_path / 'engines.jsonl').read_text().strip().splitlines()
    assert len(store) == 1


def test_persist_refresh_on_input_change(tmp_path):
    persist(make_record('s', 'd', _input(), _engine(), model_id='m'), tmp_path)
    changed_engine = ResolvedEngine('New', 'w', ('Urza',), 'MED')
    persist(make_record('s', 'd', _input(elected=[{'event': 'x'}]), changed_engine, model_id='m'), tmp_path)
    cache = load_cache(tmp_path)
    # last write wins
    assert cache.get('s').engine.strategy == 'New'


# ---------------------------------------------------------------------------
# is_fresh staleness key
# ---------------------------------------------------------------------------


def test_is_fresh_true_for_matching_key(tmp_path):
    inp = _input()
    persist(make_record('s', 'd', inp, _engine(), model_id='m'), tmp_path)
    cache = load_cache(tmp_path)
    assert cache.is_fresh('s', input_hash(inp))


def test_is_fresh_false_on_missing(tmp_path):
    cache = load_cache(tmp_path)
    assert not cache.is_fresh('nope', 'deadbeef')


def test_is_fresh_false_on_input_change(tmp_path):
    persist(make_record('s', 'd', _input(), _engine(), model_id='m'), tmp_path)
    cache = load_cache(tmp_path)
    assert not cache.is_fresh('s', input_hash(_input(elected=[{'event': 'x'}])))


def test_is_fresh_false_on_newer_synthesis_version(tmp_path):
    inp = _input()
    persist(make_record('s', 'd', inp, _engine(), synthesis_version=SYNTHESIS_VERSION), tmp_path)
    cache = load_cache(tmp_path)
    # A record stamped at the current version is NOT fresh for a bumped version.
    assert not cache.is_fresh('s', input_hash(inp), synthesis_version=SYNTHESIS_VERSION + 1)


# ---------------------------------------------------------------------------
# persist_batch
# ---------------------------------------------------------------------------


def test_persist_batch_dedup(tmp_path):
    recs = [
        make_record('a', 'A', _input(slug='a'), _engine(), model_id='m'),
        make_record('b', 'B', _input(slug='b'), _engine(), model_id='m'),
    ]
    assert persist_batch(recs, tmp_path) == 2
    assert persist_batch(recs, tmp_path) == 0  # all fresh now
    assert len(load_cache(tmp_path).records) == 2


def test_empty_cache_default_dir_untouched(tmp_path, monkeypatch):
    """load_cache on a missing dir returns an empty, inert cache."""
    missing = tmp_path / 'does-not-exist'
    cache = ec.load_cache(missing)
    assert cache.is_empty
    assert cache.get('anything') is None
