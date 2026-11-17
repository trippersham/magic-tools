"""Tests for engine-synthesizer engine synthesis: prompt, backend, parse/validate, reader.

All tests use the in-process MockBackend -- no claude -p, no network, no key.
"""

from __future__ import annotations

import json

import pytest

from pipeline.transforms.strategy import engine_cache
from pipeline.transforms.strategy.engine_synthesis import (
    MockBackend,
    SynthError,
    build_prompt,
    build_synthesis_input,
    deck_card_names,
    load_prompt_rules,
    parse_engine_response,
    resolve_engine,
    synthesize_engine,
    validate_engine,
)

ANCHOR = 'gauntlet__urza-lord-high-artificer'

GOOD_ENGINE = {
    'strategy': 'Artifact Mana-Loop Combo',
    'win_condition': 'Generate infinite mana, then win via Urza.',
    'key_pieces': ['Urza, Lord High Artificer'],
    'confidence': 'HIGH',
}


# ---------------------------------------------------------------------------
# build_synthesis_input (deterministic, uses the committed fixture)
# ---------------------------------------------------------------------------


def test_build_synthesis_input_shape(load_deck, combos):
    deck = load_deck(ANCHOR)
    inp = build_synthesis_input(deck, combos, slug=ANCHOR)
    assert inp['slug'] == ANCHOR
    assert {
        'commanders',
        'elected',
        'archetype_identity',
        'detected_combos',
        'commander_loop',
        'naming_resolution',
        'in_deck_cards',
        'n_nonland',
        'n_cards',
    } <= set(inp)
    assert any('Urza' in c['name'] for c in inp['commanders'])
    # The in-deck roster must ground key_pieces (R5): the commander is present
    # and no name carries the raw |SET|collector suffix.
    assert inp['in_deck_cards']
    assert all('|' not in n for n in inp['in_deck_cards'])


def test_build_synthesis_input_is_deterministic(load_deck, combos):
    deck = load_deck(ANCHOR)
    a = build_synthesis_input(deck, combos, slug=ANCHOR)
    b = build_synthesis_input(deck, combos, slug=ANCHOR)
    assert engine_cache.input_hash(a) == engine_cache.input_hash(b)


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------


def test_build_prompt_contains_rules_and_deck(load_deck, combos):
    deck = load_deck(ANCHOR)
    inp = build_synthesis_input(deck, combos, slug=ANCHOR)
    prompt = build_prompt(inp)
    assert 'Engine-Namer' in prompt  # from the vendored rules
    assert ANCHOR in prompt
    assert '_sparse' not in prompt  # internal keys stripped


def test_prompt_rules_vendored():
    assert 'MANDATORY' in load_prompt_rules()


# ---------------------------------------------------------------------------
# parse_engine_response
# ---------------------------------------------------------------------------


def test_parse_plain_json():
    raw = json.dumps({'slug': 's', 'engine': GOOD_ENGINE})
    assert parse_engine_response(raw)['engine'] == GOOD_ENGINE


def test_parse_fenced_json():
    raw = '```json\n' + json.dumps({'slug': 's', 'engine': GOOD_ENGINE}) + '\n```'
    assert parse_engine_response(raw)['engine'] == GOOD_ENGINE


def test_parse_with_prose_prefix():
    raw = 'Here is the result:\n{"slug": "s", "engine": ' + json.dumps(GOOD_ENGINE) + '}'
    assert parse_engine_response(raw)['slug'] == 's'


def test_parse_garbage_raises():
    with pytest.raises(SynthError):
        parse_engine_response('not json at all')


# ---------------------------------------------------------------------------
# validate_engine
# ---------------------------------------------------------------------------


def test_validate_good_engine():
    assert validate_engine(GOOD_ENGINE, {'Urza, Lord High Artificer'}) == []


def test_validate_missing_key():
    bad = dict(GOOD_ENGINE)
    del bad['confidence']
    assert any('missing' in p for p in validate_engine(bad))


def test_validate_empty_strategy():
    bad = dict(GOOD_ENGINE, strategy='')
    assert any('never-empty' in p for p in validate_engine(bad))


def test_validate_bad_confidence():
    bad = dict(GOOD_ENGINE, confidence='MAYBE')
    assert any('confidence' in p for p in validate_engine(bad))


def test_validate_out_of_deck_key_pieces():
    bad = dict(GOOD_ENGINE, key_pieces=['Black Lotus'])
    problems = validate_engine(bad, {'Urza, Lord High Artificer'})
    assert any('R5' in p for p in problems)


def test_validate_tolerates_set_collector_suffix():
    """Lake decks carry name|SET|collector; the model names cards plainly."""
    engine = dict(GOOD_ENGINE, key_pieces=['Gonti, Canny Acquisitor'])
    # Deck names are suffixed; plain model name must still validate.
    assert validate_engine(engine, {'Gonti, Canny Acquisitor|OTC|1'}) == []
    # And the reverse (model echoes the suffix) also validates.
    engine2 = dict(GOOD_ENGINE, key_pieces=['Gonti, Canny Acquisitor|OTC|1'])
    assert validate_engine(engine2, {'Gonti, Canny Acquisitor'}) == []


# ---------------------------------------------------------------------------
# synthesize_engine (MockBackend)
# ---------------------------------------------------------------------------


def test_synthesize_engine_mock(load_deck, combos):
    deck = load_deck(ANCHOR)
    inp = build_synthesis_input(deck, combos, slug=ANCHOR)
    # Use a real in-deck card so the R5 check passes.
    names = deck_card_names(deck)
    engine_dict = dict(GOOD_ENGINE, key_pieces=[next(iter(names))])
    backend = MockBackend(default_engine=engine_dict)
    engine = synthesize_engine(inp, backend, card_names=names)
    assert engine.strategy == 'Artifact Mana-Loop Combo'
    assert len(backend.calls) == 1


def test_synthesize_engine_rejects_out_of_deck(load_deck, combos):
    deck = load_deck(ANCHOR)
    inp = build_synthesis_input(deck, combos, slug=ANCHOR)
    backend = MockBackend(default_engine=dict(GOOD_ENGINE, key_pieces=['Nonexistent Card XYZ']))
    with pytest.raises(SynthError):
        synthesize_engine(inp, backend, card_names=deck_card_names(deck))


# ---------------------------------------------------------------------------
# resolve_engine reader (miss -> synth -> hit)
# ---------------------------------------------------------------------------


def test_resolve_engine_miss_then_hit(load_deck, combos, tmp_path):
    deck = load_deck(ANCHOR)
    names = deck_card_names(deck)
    engine_dict = dict(GOOD_ENGINE, key_pieces=[next(iter(names))])
    backend = MockBackend(default_engine=engine_dict)

    # First call: cache miss -> synthesise + persist.
    engine, meta = resolve_engine(
        deck,
        combos,
        slug=ANCHOR,
        backend=backend,
        cache_dir=tmp_path,
    )
    assert meta['status'] == 'synthesized'
    assert engine is not None
    assert len(backend.calls) == 1

    # Second call: cache hit, no backend call.
    engine2, meta2 = resolve_engine(
        deck,
        combos,
        slug=ANCHOR,
        backend=backend,
        cache_dir=tmp_path,
    )
    assert meta2['status'] == 'hit'
    assert engine2 == engine
    assert len(backend.calls) == 1  # backend NOT called again


def test_resolve_engine_miss_no_backend(load_deck, combos, tmp_path):
    deck = load_deck(ANCHOR)
    engine, meta = resolve_engine(
        deck,
        combos,
        slug=ANCHOR,
        backend=None,
        cache_dir=tmp_path,
        allow_synth=False,
    )
    assert engine is None
    assert meta['status'] == 'miss'


def test_resolve_engine_error_on_bad_backend(load_deck, combos, tmp_path):
    deck = load_deck(ANCHOR)
    backend = MockBackend(default_engine=dict(GOOD_ENGINE, key_pieces=['Not In Deck']))
    engine, meta = resolve_engine(
        deck,
        combos,
        slug=ANCHOR,
        backend=backend,
        cache_dir=tmp_path,
    )
    assert engine is None
    assert meta['status'] == 'error'
    assert meta['problems']
