"""Tests for skill_authoring.py (Phase PD -- skill-layer authoring glue).

All tests are **hermetic**: no live LLM, no live network, no DuckDB lake.
Haiku subagent output is SIMULATED with canned :class:`AuthoredEdge` lists.
The fixture search client runs compiled query predicates against in-memory
card lists.

Test organisation:

- ``TestAuthoredEdgeShape``           -- dataclass shape
- ``TestGateAndPersistAccept``        -- good edges pass gate + persist
- ``TestGateAndPersistReject``        -- bad edges rejected by gate
- ``TestGateAndPersistMixed``         -- mix of good + bad
- ``TestEndToEndProvisionalToResolved``-- unauthored_cards -> canned haiku -> gate -> RESOLVED
- ``TestStrCoercion``                 -- persist/load_store accept str store_dir
- ``TestPromptFileExists``            -- decomposition prompt exists on disk
- ``TestDefaultPathParity``           -- byte-parity gate (curated SHAs unchanged)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.transforms.strategy.engine_hydration import (
    SYNTH_BREADTH_WARN,
    VerificationResult,
    curated_edge_queries,
    make_fixture_search_client,
    unauthored_cards,
)
from pipeline.transforms.strategy.engine_report import (
    CompletenessState,
    identify_engines,
)
from pipeline.transforms.strategy.machine_vocab import (
    empty_store,
    load_store,
)
from pipeline.transforms.strategy.machine_vocab import (
    persist as mv_persist,
)
from pipeline.transforms.strategy.skill_authoring import (
    DECOMPOSE_PROMPT_PATH,
    AuthoredEdge,
    gate_and_persist,
)
from pipeline.transforms.strategy.tests.conftest import LAKE_AVAILABLE

_skip_no_lake = pytest.mark.skipif(not LAKE_AVAILABLE, reason='lake parquets not available')

FIX = Path(__file__).resolve().parent / 'fixtures'
RELAY_DECKS = FIX / 'relay_decks'


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_relay_fixture() -> dict:
    return json.loads((RELAY_DECKS / 'relay__queza-lichs-mastery.json').read_text())


def _card(
    name: str,
    oracle_text: str,
    otags: list[str],
    *,
    keywords: list[str] | None = None,
    type_line: str = 'Creature',
    mana_cost: str = '{1}',
) -> dict:
    return {
        'name': name,
        'oracle_text': oracle_text,
        'otags': otags,
        'keywords': keywords or [],
        'type_line': type_line,
        'mana_cost': mana_cost,
    }


def _fixture_cards() -> list[dict]:
    """The relay fixture's full card list."""
    return _load_relay_fixture()['cards']


# A synthetic corroboration card added to search pools so the Erebos edge
# passes the over-capture gate. In a real run the 40K-card lake / Scryfall
# provides such cards; the small relay fixture lacks one.
_CORROBORATION_FRIEND = _card(
    'Firesong and Sunspeaker',
    'Whenever a white instant or sorcery spell causes you to gain life, '
    'Firesong and Sunspeaker deals 3 damage to target creature or player.',
    ['lifegain-matters', 'removal', 'lifegain', 'burn-player'],
    type_line='Legendary Creature',
)


def _search_pool() -> list[dict]:
    """Relay fixture cards + the synthetic corroboration friend.

    The search client simulates what Scryfall/lake returns for a query.
    The small relay fixture lacks cards that corroborate the Erebos edge,
    so one synthetic card is added (as would exist in a real 40K-card pool).
    """
    return [*_fixture_cards(), _CORROBORATION_FRIEND]


# Canned "good" authored edges that SHOULD pass the gate
# (matching the 7 unauthored cards in the relay fixture).
def _good_edges() -> list[AuthoredEdge]:
    """Seven canned edges covering the 7 unauthored_cards/unauthored cards."""
    return [
        # Vilis, Broker of Blood: self-life-loss -> draw
        AuthoredEdge(
            source_card_name='Vilis, Broker of Blood',
            source_card_oracle_id='vilis-oracle-id',
            attr_name='self-loss-to-draw',
            query='oracle:/los[a-z]*[^.]*life[^.]*draw/ AND otag:self-life-loss-matters AND otag:draw',
            trigger_event='life-loss-drain',
            produced_event='draw-a-card',
        ),
        # Archangel of Thune: lifegain -> counters
        AuthoredEdge(
            source_card_name='Archangel of Thune',
            source_card_oracle_id='archangel-oracle-id',
            attr_name='gain-to-counters',
            query='oracle:/gain[^.]*life[^.]*counter/ AND otag:lifegain-matters AND otag:counters',
            trigger_event='lifegain',
            produced_event='counter-placed',
        ),
        # Well of Lost Dreams: lifegain -> draw (cost-gated).
        # NOTE: "gain...life...draw" spans two sentences in Well's text
        # (period after "you gained."), so the regex boundary [^.]* cannot
        # bridge them. An otag-only query covers it instead.
        AuthoredEdge(
            source_card_name='Well of Lost Dreams',
            source_card_oracle_id='well-oracle-id',
            attr_name='gain-to-draw-cost-gated',
            query='otag:lifegain-matters AND otag:draw-engine',
            trigger_event='lifegain',
            produced_event='draw-a-card',
        ),
        # Trudge Garden: lifegain -> tokens
        AuthoredEdge(
            source_card_name='Trudge Garden',
            source_card_oracle_id='trudge-oracle-id',
            attr_name='gain-to-tokens',
            query='otag:lifegain-matters AND otag:tokens',
            trigger_event='lifegain',
            produced_event='token-created',
        ),
        # Cleric of Life's Bond: lifegain -> counters.
        # Broader query (no gains-pp-counters constraint) so that Archangel
        # (which has ``counters``, a produced_by for counter-placed) also gets
        # captured and corroborates.  Cleric alone has ``gains-pp-counters``
        # which is a counter CONSUMER otag, not a producer, so it cannot
        # corroborate the effect side alone.
        AuthoredEdge(
            source_card_name="Cleric of Life's Bond",
            source_card_oracle_id='cleric-oracle-id',
            attr_name='gain-to-counters-cleric',
            query='oracle:/gain[^.]*life[^.]*counter/ AND otag:lifegain-matters',
            trigger_event='lifegain',
            produced_event='counter-placed',
        ),
        # Rhox Faithmender: covered by a broad lifegain-increaser query
        AuthoredEdge(
            source_card_name='Rhox Faithmender',
            source_card_oracle_id='rhox-oracle-id',
            attr_name='lifegain-amplifier',
            query='otag:lifegain-increaser',
            trigger_event='lifegain',
            produced_event='lifegain',
        ),
        # Erebos's Intervention: lifegain + removal (utility spell).
        # Erebos itself has only producer otags (no trigger otags), so a
        # synthetic corroboration card in the search pool enables the gate.
        AuthoredEdge(
            source_card_name="Erebos's Intervention",
            source_card_oracle_id='erebos-oracle-id',
            attr_name='removal-lifegain',
            query='otag:lifegain AND otag:removal',
            trigger_event='lifegain',
            produced_event='permanent-dies',
        ),
    ]


# Canned "bad" authored edges that SHOULD be rejected.
def _bad_edges() -> list[AuthoredEdge]:
    """Three canned edges that over-capture or have bad regex."""
    return [
        # Bad: bare regex with no otag constraint -> over-capture
        AuthoredEdge(
            source_card_name='Over-Capture Card',
            source_card_oracle_id='overcapture-oracle-id',
            attr_name='bare-los-gain',
            query='oracle:/los[^.]*gain/',
            trigger_event='life-loss-drain',
            produced_event='lifegain',
        ),
        # Bad: lookahead (RE2 violation)
        AuthoredEdge(
            source_card_name='Regex-Fail Card',
            source_card_oracle_id='regex-fail-oracle-id',
            attr_name='bad-regex',
            query='oracle:/(?=.*draw)gain/',
            trigger_event='lifegain',
            produced_event='draw-a-card',
        ),
        # Bad: source card not in captured set (query doesn't match it)
        AuthoredEdge(
            source_card_name='Nonexistent Card XYZ',
            source_card_oracle_id='nonexistent-oracle-id',
            attr_name='mismatched-source',
            query='otag:lifegain-matters',
            trigger_event='lifegain',
            produced_event='draw-a-card',
        ),
    ]


# =====================================================================
# TestAuthoredEdgeShape
# =====================================================================


class TestAuthoredEdgeShape:
    """AuthoredEdge dataclass basics."""

    def test_frozen(self) -> None:
        edge = _good_edges()[0]
        with pytest.raises(AttributeError):
            edge.attr_name = 'x'  # type: ignore[misc]

    def test_default_model_id(self) -> None:
        edge = AuthoredEdge(
            source_card_name='X',
            source_card_oracle_id='id',
            attr_name='a',
            query='otag:foo',
            trigger_event='lifegain',
            produced_event='draw-a-card',
        )
        assert edge.model_id == 'claude-3-5-haiku-20241022'

    def test_all_fields_accessible(self) -> None:
        edge = _good_edges()[0]
        assert edge.source_card_name
        assert edge.source_card_oracle_id
        assert edge.attr_name
        assert edge.query
        assert edge.trigger_event
        assert edge.produced_event


# =====================================================================
# TestGateAndPersistAccept
# =====================================================================


@_skip_no_lake
class TestGateAndPersistAccept:
    """Good edges pass the gate and get persisted."""

    def test_single_good_edge_accepted(self, tmp_path: Path) -> None:
        client = make_fixture_search_client(_search_pool())
        edge = _good_edges()[0]  # Vilis: self-loss -> draw

        result = gate_and_persist(
            [edge],
            deck_context='test-deck',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.persisted) == 1
        assert len(result.rejected) == 0
        assert result.persisted[0].accepted is True

    def test_persisted_edge_has_provenance(self, tmp_path: Path) -> None:
        client = make_fixture_search_client(_search_pool())
        edge = _good_edges()[0]

        store_dir = tmp_path / 'mv'
        gate_and_persist(
            [edge],
            deck_context='queza-test',
            store_dir=store_dir,
            search_client=client,
        )
        store = load_store(store_dir)
        assert not store.is_empty
        attr = store.attributes[0]
        assert attr.provenance.source == 'haiku'
        assert attr.provenance.model_id == 'claude-3-5-haiku-20241022'
        assert attr.provenance.source_card_name == 'Vilis, Broker of Blood'
        assert attr.provenance.deck_context == 'queza-test'
        assert attr.provenance.scryfall_resolved_count >= 1

    def test_persisted_edge_in_jsonl(self, tmp_path: Path) -> None:
        client = make_fixture_search_client(_search_pool())
        edge = _good_edges()[0]

        store_dir = tmp_path / 'mv'
        gate_and_persist(
            [edge],
            deck_context='test-deck',
            store_dir=store_dir,
            search_client=client,
        )
        jsonl = (store_dir / 'attributes.jsonl').read_text().strip()
        rec = json.loads(jsonl)
        assert rec['name'] == 'self-loss-to-draw'
        assert rec['source'] == 'haiku'

    def test_verification_result_attached(self, tmp_path: Path) -> None:
        client = make_fixture_search_client(_search_pool())
        edge = _good_edges()[0]

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        vr = result.persisted[0].verification
        assert vr.passed is True
        assert vr.pasteability_ok is True
        assert vr.resolves is True
        assert vr.expected_cards_hit is True

    def test_multiple_good_edges_all_accepted(self, tmp_path: Path) -> None:
        client = make_fixture_search_client(_search_pool())
        edges = _good_edges()

        result = gate_and_persist(
            edges,
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.persisted) == 7
        assert len(result.rejected) == 0


# =====================================================================
# TestGateAndPersistReject
# =====================================================================


@_skip_no_lake
class TestGateAndPersistReject:
    """Bad edges are rejected by the gate."""

    def test_overcapture_rejected(self, tmp_path: Path) -> None:
        """Bare regex without otag constraints -> over-capture rejection."""
        # Build a card pool with false positives.
        cards = [
            _card(
                'Exquisite Blood',
                'Whenever an opponent loses life, you gain that much life.',
                ['life-loss-matters', 'lifegain', 'repeatable-lifegain'],
            ),
            _card('Drain Spell', 'Target player loses 2 life and you gain 2 life.', ['burn-player']),
            _card('Combat Trick', 'Target creature loses all abilities and gains trample.', ['gives-trample']),
            _card('Over-Capture Card', 'You lose life and gain stuff.', ['burn-player']),
        ]
        client = make_fixture_search_client(cards)
        edge = _bad_edges()[0]  # bare oracle:/los[^.]*gain/

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.rejected) == 1
        assert len(result.persisted) == 0
        assert 'over-capture' in result.rejected[0].reason

    def test_regex_violation_rejected(self, tmp_path: Path) -> None:
        """Lookahead in regex -> pasteability rejection."""
        client = make_fixture_search_client(_search_pool())
        edge = _bad_edges()[1]  # oracle:/(?=.*draw)gain/

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.rejected) == 1
        assert 'pasteability' in result.rejected[0].reason

    def test_expected_card_missing_rejected(self, tmp_path: Path) -> None:
        """Source card not in captured set -> rejection."""
        client = make_fixture_search_client(_search_pool())
        edge = _bad_edges()[2]  # 'Nonexistent Card XYZ' not in fixture

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.rejected) == 1
        assert 'expected-hit' in result.rejected[0].reason

    def test_rejected_edges_not_persisted(self, tmp_path: Path) -> None:
        """Rejected edges leave no trace in the store."""
        client = make_fixture_search_client(_search_pool())
        bad = _bad_edges()

        store_dir = tmp_path / 'mv'
        gate_and_persist(
            bad,
            deck_context='test',
            store_dir=store_dir,
            search_client=client,
        )
        store = load_store(store_dir)
        assert store.is_empty


# =====================================================================
# TestGateAndPersistMixed
# =====================================================================


@_skip_no_lake
class TestGateAndPersistMixed:
    """Mix of good + bad edges: good persist, bad rejected."""

    def test_mixed_batch(self, tmp_path: Path) -> None:
        # Use a card pool that exercises both good and bad edges.
        pool = [
            *_search_pool(),
            _card('Over-Capture Card', 'You lose life and gain stuff.', ['burn-player']),
            _card('Drain Spell', 'Target player loses 2 life and you gain 2 life.', ['burn-player']),
            _card('Combat Trick', 'Target creature loses all abilities and gains trample.', ['gives-trample']),
        ]
        client = make_fixture_search_client(pool)

        good = _good_edges()[:2]  # Vilis + Archangel
        bad = _bad_edges()[:2]  # over-capture + regex
        batch = good + bad

        store_dir = tmp_path / 'mv'
        result = gate_and_persist(
            batch,
            deck_context='test',
            store_dir=store_dir,
            search_client=client,
        )
        assert len(result.persisted) == 2
        assert len(result.rejected) == 2

        # Persisted names.
        persisted_names = {g.edge.attr_name for g in result.persisted}
        assert 'self-loss-to-draw' in persisted_names
        assert 'gain-to-counters' in persisted_names

        # Rejected names.
        rejected_names = {r.edge.attr_name for r in result.rejected}
        assert 'bare-los-gain' in rejected_names
        assert 'bad-regex' in rejected_names

        # Store has exactly the 2 good edges.
        store = load_store(store_dir)
        assert len(store.attributes) == 2


# =====================================================================
# TestEndToEndProvisionalToResolved
# =====================================================================


@_skip_no_lake
class TestEndToEndProvisionalToResolved:
    """The full canned end-to-end: unauthored_cards -> canned haiku -> gate -> RESOLVED.

    Demonstrates:
    1. Queza fixture + empty store -> PROVISIONAL (7 unauthored cards)
    2. Canned haiku edges covering all 7 unauthored cards
    3. gate_and_persist: good persist, bad reject
    4. identify_engines with enriched store -> RESOLVED
    """

    def test_e2e_flip_to_resolved(self, tmp_path: Path) -> None:
        fixture = _load_relay_fixture()
        deck = fixture

        # Step 1: PROVISIONAL with empty store.
        report_before = identify_engines(deck, store=empty_store())
        assert report_before.state is CompletenessState.PROVISIONAL
        assert len(report_before.dark_cards) == 7

        # Step 2: Gate the 7 good + 3 bad canned edges.
        # The search pool simulates what Scryfall/lake returns:
        # fixture cards + synthetic corroboration friend + FP cards.
        store_dir = tmp_path / 'mv'
        good = _good_edges()
        bad = _bad_edges()

        extended_pool = [
            *_search_pool(),
            _card('Over-Capture Card', 'You lose life and gain stuff.', ['burn-player']),
            _card('Drain Spell', 'Target player loses 2 life and you gain 2 life.', ['burn-player']),
            _card('Combat Trick', 'Target creature loses all abilities and gains trample.', ['gives-trample']),
        ]
        ext_client = make_fixture_search_client(extended_pool)

        result = gate_and_persist(
            good + bad,
            deck_context='queza-e2e',
            store_dir=store_dir,
            search_client=ext_client,
        )
        assert len(result.persisted) == 7
        assert len(result.rejected) == 3

        # Step 3: RESOLVED with enriched store.
        enriched_store = load_store(store_dir)
        assert not enriched_store.is_empty

        report_after = identify_engines(deck, store=enriched_store)
        assert report_after.state is CompletenessState.RESOLVED
        assert report_after.dark_cards == []

        # Engines still found (the cycle did not disappear).
        assert len(report_after.engines) > 0

    def test_frontier_shrinks_after_gate(self, tmp_path: Path) -> None:
        """After gating, there are no unauthored cards (all unauthored cards covered)."""
        fixture = _load_relay_fixture()
        cards = fixture['cards']
        # Search pool includes corroboration friend (for Erebos edge).
        client = make_fixture_search_client(_search_pool())

        # Before: 7 unauthored cards.
        curated = curated_edge_queries()
        front_before = unauthored_cards(cards, curated)
        assert len(front_before) == 7

        # Gate + persist.
        store_dir = tmp_path / 'mv'
        gate_and_persist(
            _good_edges(),
            deck_context='test',
            store_dir=store_dir,
            search_client=client,
        )
        enriched_store = load_store(store_dir)
        full_edges = curated + enriched_store.edge_queries()

        # After: 0 unauthored cards.
        front_after = unauthored_cards(cards, full_edges)
        assert len(front_after) == 0

    def test_compounding_across_decks(self, tmp_path: Path) -> None:
        """A second deck with the same card reuses the stored edge."""
        client = make_fixture_search_client(_search_pool())

        store_dir = tmp_path / 'mv'
        # First deck: gate Vilis edge.
        gate_and_persist(
            [_good_edges()[0]],  # Vilis
            deck_context='deck-1',
            store_dir=store_dir,
            search_client=client,
        )

        # Second deck: store already has Vilis -> no re-authoring needed.
        store = load_store(store_dir)
        assert store.has_edge_for_card('vilis-oracle-id')


# =====================================================================
# TestStrCoercion
# =====================================================================


@_skip_no_lake
class TestStrCoercion:
    """persist() and load_store() accept str store_dir (PB fix)."""

    def test_persist_with_str_store_dir(self, tmp_path: Path) -> None:
        from pipeline.transforms.strategy.machine_vocab import (
            CardAttribute,
            Provenance,
        )

        store_dir_str = str(tmp_path / 'str_test')
        attr = CardAttribute(
            name='test-attr',
            query='otag:foo',
            provenance=Provenance(source='haiku'),
        )
        # Previously raised TypeError; now should work.
        mv_persist(attr, store_dir=store_dir_str)

        # Verify it's readable.
        store = load_store(store_dir_str)
        assert not store.is_empty
        assert store.attributes[0].name == 'test-attr'

    def test_load_store_with_str_store_dir(self, tmp_path: Path) -> None:
        from pipeline.transforms.strategy.machine_vocab import (
            CardAttribute,
            Provenance,
        )

        store_dir = tmp_path / 'str_load_test'
        attr = CardAttribute(
            name='test-attr',
            query='otag:bar',
            provenance=Provenance(source='haiku'),
        )
        mv_persist(attr, store_dir=store_dir)

        # Load with str.
        store = load_store(str(store_dir))
        assert not store.is_empty
        assert store.attributes[0].query == 'otag:bar'

    def test_gate_and_persist_with_str_store_dir(self, tmp_path: Path) -> None:
        """gate_and_persist also accepts str store_dir (passes through)."""
        client = make_fixture_search_client(_search_pool())
        edge = _good_edges()[0]

        store_dir_str = str(tmp_path / 'str_gate_test')
        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=store_dir_str,
            search_client=client,
        )
        assert len(result.persisted) == 1

        # Verify readable via str.
        store = load_store(store_dir_str)
        assert not store.is_empty


# =====================================================================
# TestPromptFileExists
# =====================================================================


class TestPromptFileExists:
    """The decomposition prompt is a real file on disk."""

    def test_prompt_path_exists(self) -> None:
        assert DECOMPOSE_PROMPT_PATH.exists(), f'Decomposition prompt not found at {DECOMPOSE_PROMPT_PATH}'

    def test_prompt_not_empty(self) -> None:
        content = DECOMPOSE_PROMPT_PATH.read_text()
        assert len(content) > 500, 'Prompt file seems too short'

    def test_prompt_contains_key_sections(self) -> None:
        content = DECOMPOSE_PROMPT_PATH.read_text()
        assert 'trigger_event' in content
        assert 'produced_event' in content
        assert '[^.]*' in content
        assert 'RE2' in content
        assert 'otag' in content
        assert 'Replacement Effects' in content
        assert 'Cost-Gated' in content
        assert 'DFC' in content


# =====================================================================
# TestDefaultPathParity
# =====================================================================

# =====================================================================
# TestSynthBreadthReroute — the soft over-breadth gate + reroute seam
# =====================================================================


@_skip_no_lake
class TestSynthBreadthReroute:
    """Soft over-breadth gate and reroute handling in gate_and_persist.

    When a synth passes all hard checks but captures > SYNTH_BREADTH_WARN
    cards, the verification sets ``breadth_warning=True``.  The reroute seam
    in ``gate_and_persist`` handles this via an optional ``reauthor`` callback.
    """

    # -- Helpers for building broad and narrow synth card pools ----------------

    @staticmethod
    def _broad_synth_edge(
        name: str = 'broad-counter-synth',
        query: str = "oracle:'counter'",
    ) -> AuthoredEdge:
        """A synth edge whose query captures >100 cards in a broad pool."""
        return AuthoredEdge(
            source_card_name='Jadzia Dax',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name=name,
            query=query,
            trigger_event='counter-placed',
            produced_event='mana-from-counters',
            decision='synth',
        )

    @staticmethod
    def _broad_pool(n: int = 150) -> list[dict]:
        """A card pool with n cards all matching ``oracle:'counter'``."""
        cards = [_card(f'Noise {i}', 'Put a counter on something.', []) for i in range(n)]
        cards[0] = _card('Jadzia Dax', 'number of counters on her.', [])
        return cards

    @staticmethod
    def _narrow_pool() -> list[dict]:
        """A card pool with few cards matching ``oracle:'number of counters on'``."""
        return [
            _card('Jadzia Dax', 'add an amount of {G} equal to the number of counters on her.', []),
            _card('Fungal Behemoth', 'power and toughness equal to the number of counters on it.', []),
            _card('Irrelevant', 'Flying', []),
        ]

    # -- Tests ----------------------------------------------------------------

    def test_synth_within_breadth_persisted_normally(self, tmp_path: Path) -> None:
        """A synth with <= 100 hits is persisted without breadth warning."""
        edge = AuthoredEdge(
            source_card_name='Jadzia Dax',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name='counter-to-mana',
            query="oracle:'number of counters on'",
            trigger_event='counter-placed',
            produced_event='mana-from-counters',
            decision='synth',
        )
        client = make_fixture_search_client(self._narrow_pool())

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
        )
        assert len(result.persisted) == 1
        assert len(result.rejected) == 0
        assert result.persisted[0].verification.breadth_warning is False

        # Stored provenance has NO breadth flag.
        store = load_store(tmp_path / 'mv')
        assert not store.attributes[0].provenance.breadth_warning

    def test_synth_over_breadth_no_reauthor_rejected(self, tmp_path: Path) -> None:
        """A broad synth without a reauthor callback is rejected, not persisted."""
        edge = self._broad_synth_edge()
        client = make_fixture_search_client(self._broad_pool())

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            # No reauthor callback — default None.
        )
        assert len(result.persisted) == 0
        assert len(result.rejected) == 1
        assert 'breadth' in result.rejected[0].reason.lower()
        assert result.rejected[0].verification.breadth_warning is True

        # Nothing in the store.
        store = load_store(tmp_path / 'mv')
        assert store.is_empty

    def test_reauthor_returns_edge_with_otag(self, tmp_path: Path) -> None:
        """reauthor returns an edge-with-otag: re-gated through edge path.

        Uses real catalog events so corroboration can pass:
        - trigger_event='counter-placed' triggers includes 'counters-matter'
        - produced_event='counter-placed' produced_by includes 'counters'
        """
        original = self._broad_synth_edge()

        # The reauthor callback "discovers" otags and returns an edge decision.
        rerouted_edge = AuthoredEdge(
            source_card_name='Jadzia Dax',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name='counter-to-counter-otag',
            query='otag:counters-matter AND otag:counters',
            trigger_event='counter-placed',
            produced_event='counter-placed',
            decision='edge',  # promoted to edge
        )

        def stub_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> AuthoredEdge:
            return rerouted_edge

        # Build a pool where the otag-based query corroborates.
        # Cards need otags from counter-placed.triggers ('counters-matter')
        # AND counter-placed.produced_by ('counters') for corroboration.
        pool = [
            _card('Jadzia Dax', 'number of counters on her.', ['counters-matter', 'counters']),
            _card('Corroborator', 'Another counters card.', ['counters-matter', 'counters']),
            *self._broad_pool(),  # original broad pool for the first verify
        ]

        client = make_fixture_search_client(pool)

        result = gate_and_persist(
            [original],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=stub_reauthor,
        )
        assert len(result.persisted) == 1
        assert len(result.rejected) == 0
        assert result.persisted[0].rerouted is True
        assert result.persisted[0].reroute_outcome == 'promoted-to-edge'

        # The persisted edge uses the REROUTED query/name.
        store = load_store(tmp_path / 'mv')
        assert store.attributes[0].name == 'counter-to-counter-otag'
        assert store.attributes[0].query == 'otag:counters-matter AND otag:counters'
        assert not store.attributes[0].provenance.breadth_warning

    def test_reauthor_returns_tightened_synth(self, tmp_path: Path) -> None:
        """reauthor returns a tighter synth (<=100 hits): persisted clean."""
        original = self._broad_synth_edge()

        tightened = AuthoredEdge(
            source_card_name='Jadzia Dax',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name='counter-to-mana-tight',
            query="oracle:'number of counters on'",
            trigger_event='counter-placed',
            produced_event='mana-from-counters',
            decision='synth',  # still synth, but tighter
        )

        def stub_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> AuthoredEdge:
            return tightened

        # Broad pool for original verify, narrow pool cards also present for reroute.
        pool = self._broad_pool() + self._narrow_pool()
        client = make_fixture_search_client(pool)

        result = gate_and_persist(
            [original],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=stub_reauthor,
        )
        assert len(result.persisted) == 1
        assert result.persisted[0].rerouted is True
        assert result.persisted[0].reroute_outcome == 'tightened-synth'
        assert result.persisted[0].verification.breadth_warning is False

        store = load_store(tmp_path / 'mv')
        assert store.attributes[0].name == 'counter-to-mana-tight'
        assert not store.attributes[0].provenance.breadth_warning

    def test_reauthor_returns_none_accepts_with_flag(self, tmp_path: Path) -> None:
        """reauthor returns None: original persisted WITH breadth_warning flag."""
        original = self._broad_synth_edge()

        def stub_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> None:
            return None

        client = make_fixture_search_client(self._broad_pool())

        result = gate_and_persist(
            [original],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=stub_reauthor,
        )
        # Option A: accepted with flag, not dropped.
        assert len(result.persisted) == 1
        assert len(result.rejected) == 0
        assert result.persisted[0].rerouted is True
        assert result.persisted[0].reroute_outcome == 'accepted-with-flag'

        # Provenance carries the breadth_warning marker.
        store = load_store(tmp_path / 'mv')
        assert store.attributes[0].provenance.breadth_warning is True
        assert store.attributes[0].name == 'broad-counter-synth'

    def test_reauthor_called_exactly_once(self, tmp_path: Path) -> None:
        """reauthor is called at most once per edge (bounded, no loop)."""
        call_count = 0

        def counting_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> None:
            nonlocal call_count
            call_count += 1
            return None  # accept-with-flag path

        original = self._broad_synth_edge()
        client = make_fixture_search_client(self._broad_pool())

        gate_and_persist(
            [original],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=counting_reauthor,
        )
        assert call_count == 1

    def test_edge_decision_untouched_by_reroute(self, tmp_path: Path) -> None:
        """An edge decision (decision='edge') never triggers the reroute path,
        even with a reauthor callback provided.

        This is the regression guard: the edge path is byte-identical to
        pre-breadth-warning behavior.
        """
        call_count = 0

        def fail_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> AuthoredEdge:
            nonlocal call_count
            call_count += 1
            raise AssertionError('reauthor should never be called for edge decisions')

        # Build a good edge that passes corroboration.
        edge = _good_edges()[0]  # Vilis: self-loss -> draw, decision='edge'
        client = make_fixture_search_client(_search_pool())

        result = gate_and_persist(
            [edge],
            deck_context='test',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=fail_reauthor,
        )
        assert len(result.persisted) == 1
        assert call_count == 0  # reauthor never called
        assert result.persisted[0].rerouted is False


# =====================================================================
# TestSynthBreadthEndToEnd — canned demo of the reroute pipeline
# =====================================================================


class TestSynthBreadthEndToEnd:
    """Canned end-to-end: broad oracle:'counter' -> breadth_warning ->
    reauthor tightens to oracle:'number of counters on' -> passes.

    Demonstrates the full reroute pipeline with fixture data.
    """

    def test_broad_counter_rerouted_to_tight(self, tmp_path: Path) -> None:
        """The full reroute demo: broad -> warn -> tighten -> pass."""
        from pipeline.transforms.strategy.engine_hydration import verify as _verify

        # Step 1: Build a card pool where oracle:'counter' is VERY broad
        # (>100 synthetic noise cards all containing 'counter') but
        # oracle:'number of counters on' is narrow (2-3 cards).
        noise_cards = [_card(f'Noise {i}', 'Put a counter on target creature.', []) for i in range(120)]
        source_card = _card(
            'Jadzia Dax, Living Legacy',
            'At the beginning of your first main phase, put a +1/+1 counter on '
            'Jadzia Dax, then add an amount of {G} equal to the number of '
            'counters on her.',
            [],
        )
        narrow_card = _card(
            'Fungal Behemoth',
            'power and toughness are each equal to the number of counters on it.',
            [],
        )
        all_cards = [source_card, narrow_card, *noise_cards]
        client = make_fixture_search_client(all_cards)

        # Step 2: Verify the broad query — should trigger breadth_warning.
        broad_vr = _verify(
            "oracle:'counter'",
            ('counter-placed', 'mana-from-counters'),
            expected_card_names=['Jadzia Dax, Living Legacy'],
            search_client=client,
            decision='synth',
        )
        assert broad_vr.passed is False
        assert broad_vr.breadth_warning is True
        assert broad_vr.hit_count > SYNTH_BREADTH_WARN

        # Step 3: Build the reroute: tighten to 'number of counters on'.
        broad_edge = AuthoredEdge(
            source_card_name='Jadzia Dax, Living Legacy',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name='counter-mana-broad',
            query="oracle:'counter'",
            trigger_event='counter-placed',
            produced_event='mana-from-counters',
            decision='synth',
        )
        tightened_edge = AuthoredEdge(
            source_card_name='Jadzia Dax, Living Legacy',
            source_card_oracle_id='jadzia-oracle-id',
            attr_name='counter-mana-tight',
            query="oracle:'number of counters on'",
            trigger_event='counter-placed',
            produced_event='mana-from-counters',
            decision='synth',
        )

        def demo_reauthor(edge: AuthoredEdge, vr: VerificationResult) -> AuthoredEdge:
            """Simulates haiku tightening the query."""
            assert vr.breadth_warning is True
            return tightened_edge

        # Step 4: Gate with the reroute callback.
        result = gate_and_persist(
            [broad_edge],
            deck_context='jadzia-demo',
            store_dir=tmp_path / 'mv',
            search_client=client,
            reauthor=demo_reauthor,
        )

        # Step 5: Verify the tightened synth passed.
        assert len(result.persisted) == 1
        assert len(result.rejected) == 0
        gr = result.persisted[0]
        assert gr.rerouted is True
        assert gr.reroute_outcome == 'tightened-synth'
        assert gr.verification.passed is True
        assert gr.verification.breadth_warning is False
        assert gr.verification.hit_count <= SYNTH_BREADTH_WARN

        # The store contains the tightened query.
        store = load_store(tmp_path / 'mv')
        assert not store.is_empty
        attr = store.attributes[0]
        assert attr.name == 'counter-mana-tight'
        assert attr.query == "oracle:'number of counters on'"
        assert not attr.provenance.breadth_warning


# =====================================================================
# TestDefaultPathParity
# =====================================================================


@_skip_no_lake
class TestDefaultPathParity:
    """Importing skill_authoring does NOT shift the curated SHAs."""

    def test_engine_isolation_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_engine_isolation_baselines,
        )

        baselines = compute_engine_isolation_baselines()
        sha = composite_sha(baselines)
        expected = '7a83891a6c191f2b4cb1197261e21f55110456d33091f0f32e8dadab18cd4d37'
        assert sha == expected, f'engine SHA shifted: {sha}'

    def test_resolve_sha_unchanged(self) -> None:
        from pipeline.transforms.strategy.tests.engine_parity_oracle import (
            composite_sha,
            compute_resolve_baselines,
        )

        baselines = compute_resolve_baselines()
        sha = composite_sha(baselines)
        expected = 'a63f268b0884a47432659a9053a20ab7e8ec3b85ab227cf473049581586bd2cd'
        assert sha == expected, f'resolve SHA shifted: {sha}'
