"""Skill-layer authoring glue -- Phase PD.

The bridge between a dispatched haiku subagent's output and the deterministic
pipeline.  The haiku subagent follows the decomposition prompt
(``prompts/decompose_within_card_edge.md``) and returns one or more
:class:`AuthoredEdge` proposals.  This module **gates** each proposal through
the verification pipeline and **persists** the ones that pass.

Skill-Layer Flow (documented here; the dispatch happens at the skill surface)
-----------------------------------------------------------------------------

1. **Frontier** -- ``engine_hydration.frontier(deck_cards, authored_edges)``
   computes which engine-relevant cards lack an authored within-card edge.

2. **Dispatch haiku subagent** -- the skill surface dispatches a haiku
   subagent with the decomposition prompt + each frontier card's name, oracle
   text, and otags.  (NOT done here -- the pipeline stays LLM-free.)

3. **Gate and persist** -- :func:`gate_and_persist` receives the haiku
   subagent's authored edges, runs each through
   :func:`~.engine_hydration.verify`, and persists passing edges via
   :func:`~.machine_vocab.persist`.  Failures are collected with reasons.

4. **Re-walk** -- :func:`~.engine_report.identify_engines` re-runs with the
   now-enriched machine store; the honesty label may flip from PROVISIONAL to
   RESOLVED if the frontier is fully covered.

No LLM call happens in this module.  Every function is deterministic given
the same inputs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from pipeline.transforms.strategy.engine_hydration import (
    SearchClient,
    VerificationResult,
    verify,
)
from pipeline.transforms.strategy.machine_vocab import (
    CardAttribute,
    Provenance,
    persist,
)

# ---------------------------------------------------------------------------
# Prompt location constant
# ---------------------------------------------------------------------------

#: Path to the decomposition prompt the haiku subagent follows.
DECOMPOSE_PROMPT_PATH: Path = Path(__file__).resolve().parent / 'prompts' / 'decompose_within_card_edge.md'


# ---------------------------------------------------------------------------
# AuthoredEdge -- the shape of haiku subagent output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthoredEdge:
    """A single within-card edge proposed by the haiku subagent.

    This is the **input** to :func:`gate_and_persist`: the subagent's
    proposal, not yet verified or persisted.

    Parameters
    ----------
    source_card_name
        The card that prompted the edge (e.g. ``'Queza, Augur of Agonies'``).
    source_card_oracle_id
        The card's ``oracle_id`` (for deduplication / compounding).
    attr_name
        Proposed attribute name (e.g. ``'draw-to-drain'``).
    query
        Scryfall-pasteable query string.
    trigger_event
        Event-catalog slug for the trigger side.
    produced_event
        Event-catalog slug for the produced side (was ``effect_event``).
    model_id
        The model that authored this edge (e.g. ``'claude-3-5-haiku-20241022'``).
    reasoning
        The subagent's one-line explanation (stored in provenance, not evaluated).
    """

    source_card_name: str
    source_card_oracle_id: str
    attr_name: str
    query: str
    trigger_event: str
    produced_event: str
    model_id: str = 'claude-3-5-haiku-20241022'
    reasoning: str = ''
    decision: str = 'edge'


# ---------------------------------------------------------------------------
# GateResult -- per-edge outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """Outcome of gating a single :class:`AuthoredEdge`.

    Attributes
    ----------
    edge
        The original authored edge (or the rerouted edge if a reroute
        produced a new one that was accepted).
    accepted
        ``True`` if the edge passed verification and was persisted.
    verification
        The full :class:`~.engine_hydration.VerificationResult`.
    reason
        Human-readable rejection reason (empty string if accepted).
    rerouted
        ``True`` if a breadth-warning reroute was attempted.
    reroute_outcome
        What happened during reroute: ``'promoted-to-edge'``,
        ``'tightened-synth'``, ``'accepted-with-flag'``, or empty.
    """

    edge: AuthoredEdge
    accepted: bool
    verification: VerificationResult
    reason: str = ''
    rerouted: bool = False
    reroute_outcome: str = ''


# ---------------------------------------------------------------------------
# gate_and_persist -- the PD crux
# ---------------------------------------------------------------------------


@dataclass
class GateAndPersistResult:
    """Aggregate result of :func:`gate_and_persist`.

    Attributes
    ----------
    persisted
        Edges that passed verification and were persisted to the store.
    rejected
        Edges that failed verification, each with reason.
    """

    persisted: list[GateResult] = field(default_factory=list)
    rejected: list[GateResult] = field(default_factory=list)


#: Type alias for the optional reauthor callback injected by the skill layer.
#:
#: Signature: ``(edge, verification) -> AuthoredEdge | None``.
#: - Returns a NEW :class:`AuthoredEdge` (tightened synth or promoted edge).
#: - Returns ``None`` to signal "genuinely broad, no tighter query exists" —
#:   the original synth is accepted with a ``breadth_warning`` flag in
#:   provenance (option A).
#:
#: The callback is the injection seam for the haiku subagent; this module
#: never calls a model.
ReauthorCallback = Callable[['AuthoredEdge', VerificationResult], 'AuthoredEdge | None']


def _build_and_persist(
    edge: AuthoredEdge,
    vr: VerificationResult,
    *,
    deck_context: str,
    store_dir: Path | str | None,
    now_iso: str,
    breadth_warning: bool = False,
) -> None:
    """Build a CardAttribute with provenance and persist it.

    Shared helper for the normal persist path and reroute outcomes.
    """
    provenance = Provenance(
        source='haiku',
        model_id=edge.model_id,
        authored_at=now_iso,
        source_card_name=edge.source_card_name,
        source_card_oracle_id=edge.source_card_oracle_id,
        deck_context=deck_context,
        scryfall_query=edge.query,
        scryfall_resolved_count=vr.hit_count,
        verified_at=now_iso,
        breadth_warning=breadth_warning,
    )
    attr = CardAttribute(
        name=edge.attr_name,
        query=edge.query,
        trigger_event=edge.trigger_event,
        produced_event=edge.produced_event,
        provenance=provenance,
    )
    persist(attr, store_dir=store_dir)


def _handle_reroute(
    *,
    original_edge: AuthoredEdge,
    original_vr: VerificationResult,
    rerouted_edge: AuthoredEdge | None,
    deck_context: str,
    store_dir: Path | str | None,
    search_client: SearchClient | None,
    now_iso: str,
) -> GateResult:
    """Handle a single reroute attempt for a breadth-warned synth.

    Three terminal outcomes:

    1. ``rerouted_edge`` is ``None`` — accept the original with a
       ``breadth_warning`` flag in provenance (option A).
    2. ``rerouted_edge.decision == 'edge'`` — re-verify through the full
       edge path (corroboration applies).
    3. ``rerouted_edge.decision == 'synth'`` — re-verify as a tightened
       synth; accept only if ``breadth_warning=False``.
    """
    if rerouted_edge is None:
        # Terminal case: accept-with-flag (option A).
        _build_and_persist(
            original_edge,
            original_vr,
            deck_context=deck_context,
            store_dir=store_dir,
            now_iso=now_iso,
            breadth_warning=True,
        )
        return GateResult(
            edge=original_edge,
            accepted=True,
            verification=original_vr,
            rerouted=True,
            reroute_outcome='accepted-with-flag',
        )

    # Re-verify the rerouted edge.
    vr2 = verify(
        rerouted_edge.query,
        (rerouted_edge.trigger_event, rerouted_edge.produced_event),
        expected_card_names=[rerouted_edge.source_card_name],
        search_client=search_client,
        decision=rerouted_edge.decision,
    )

    if rerouted_edge.decision == 'edge':
        # Promoted to edge: must pass the full corroboration gate.
        if vr2.passed:
            _build_and_persist(
                rerouted_edge,
                vr2,
                deck_context=deck_context,
                store_dir=store_dir,
                now_iso=now_iso,
            )
            return GateResult(
                edge=rerouted_edge,
                accepted=True,
                verification=vr2,
                rerouted=True,
                reroute_outcome='promoted-to-edge',
            )
        return GateResult(
            edge=rerouted_edge,
            accepted=False,
            verification=vr2,
            reason=(f'rerouted edge failed verification: {vr2.error or "corroboration below threshold"}'),
            rerouted=True,
            reroute_outcome='promoted-to-edge',
        )

    # Tightened synth: accept only if it now passes clean.
    if vr2.passed and not vr2.breadth_warning:
        _build_and_persist(
            rerouted_edge,
            vr2,
            deck_context=deck_context,
            store_dir=store_dir,
            now_iso=now_iso,
        )
        return GateResult(
            edge=rerouted_edge,
            accepted=True,
            verification=vr2,
            rerouted=True,
            reroute_outcome='tightened-synth',
        )
    return GateResult(
        edge=rerouted_edge,
        accepted=False,
        verification=vr2,
        reason=(f'rerouted synth still too broad or failed: {vr2.error or "breadth_warning persists"}'),
        rerouted=True,
        reroute_outcome='tightened-synth',
    )


def gate_and_persist(
    authored: list[AuthoredEdge],
    *,
    deck_context: str,
    store_dir: Path | str | None = None,
    search_client: SearchClient | None = None,
    reauthor: ReauthorCallback | None = None,
) -> GateAndPersistResult:
    """Gate each haiku-authored edge through the verification pipeline and
    persist passing ones to the machine store.

    For each :class:`AuthoredEdge`:

    1. Run :func:`~.engine_hydration.verify` (the three-check gate:
       pasteability, resolution + expected-hit, over-capture corroboration).
    2. If it **passes** clean, build a :class:`~.machine_vocab.CardAttribute`
       with full provenance and persist via :func:`~.machine_vocab.persist`.
    3. If it **fails** with ``breadth_warning=True`` (synth too broad) and a
       *reauthor* callback is provided, attempt a **single** reroute:
       - Callback returns a new edge (promoted edge or tightened synth)
         -> re-verify through the appropriate path.
       - Callback returns ``None`` -> accept the original with a
         ``breadth_warning`` marker in provenance (option A).
    4. If it **fails** without a breadth warning, or with one but no callback,
       collect the edge + reason in the rejected list.

    **No LLM call here** -- this consumes already-authored output.  The
    *reauthor* callback is the injection seam for the skill layer's haiku
    subagent; the pipeline never calls a model.

    Parameters
    ----------
    authored
        Edges proposed by the haiku subagent.
    deck_context
        Deck name/slug for provenance (e.g. ``'queza-esper-drain'``).
    store_dir
        Where to persist passing edges.  Accepts ``str`` or ``Path``.
        Defaults to the machine-vocab default store directory.
    search_client
        Injectable search client for verification.  Tests inject
        ``make_fixture_search_client``; production uses the lake or Scryfall.
    reauthor
        Optional callback the skill layer wires to a haiku subagent.
        Called at most once per breadth-warned synth edge.  See
        :data:`ReauthorCallback` for the signature and terminal outcomes.
    """
    result = GateAndPersistResult()
    now_iso = datetime.now(UTC).isoformat(timespec='seconds')

    for edge in authored:
        # --- Run the verification gate -----------------------------------
        vr = verify(
            edge.query,
            (edge.trigger_event, edge.produced_event),
            expected_card_names=[edge.source_card_name],
            search_client=search_client,
            decision=edge.decision,
        )

        if not vr.passed:
            # --- Breadth-warning reroute for synth decisions --------------
            if vr.breadth_warning:
                if reauthor is not None:
                    gr = _handle_reroute(
                        original_edge=edge,
                        original_vr=vr,
                        rerouted_edge=reauthor(edge, vr),
                        deck_context=deck_context,
                        store_dir=store_dir,
                        search_client=search_client,
                        now_iso=now_iso,
                    )
                    if gr.accepted:
                        result.persisted.append(gr)
                    else:
                        result.rejected.append(gr)
                    continue

                # No reauthor callback — not persisted (left for caller).
                result.rejected.append(
                    GateResult(
                        edge=edge,
                        accepted=False,
                        verification=vr,
                        reason=(
                            f'breadth_warning: synth query captures '
                            f'{vr.hit_count} cards (> threshold), '
                            f'no reauthor callback provided'
                        ),
                    )
                )
                continue

            # --- Normal rejection ----------------------------------------
            reasons: list[str] = []
            if not vr.pasteability_ok:
                reasons.append(f'pasteability: {vr.error}')
            if not vr.resolves:
                reasons.append(f'resolution: {vr.error or "0 cards captured"}')
            if vr.resolves and not vr.expected_cards_hit:
                reasons.append(
                    f'expected-hit: source card {edge.source_card_name!r} not in captured set ({vr.hit_count} cards)'
                )
            if vr.resolves and vr.expected_cards_hit:
                reasons.append(
                    f'over-capture: corroboration {vr.corroboration_rate:.2f} '
                    f'< threshold {vr.threshold:.2f} '
                    f'(failing: {", ".join(vr.failing_cards[:5])})'
                )
            reason = '; '.join(reasons) if reasons else (vr.error or 'unknown')

            result.rejected.append(
                GateResult(
                    edge=edge,
                    accepted=False,
                    verification=vr,
                    reason=reason,
                )
            )
            continue

        # --- Build CardAttribute with full provenance -----------------
        _build_and_persist(
            edge,
            vr,
            deck_context=deck_context,
            store_dir=store_dir,
            now_iso=now_iso,
        )

        result.persisted.append(
            GateResult(
                edge=edge,
                accepted=True,
                verification=vr,
            )
        )

    return result
