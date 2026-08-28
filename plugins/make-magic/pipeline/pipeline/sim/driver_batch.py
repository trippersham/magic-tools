"""Restartable author-batch harness — the corpus combo-litmus classifier (Phase 3.1/3.2).

Iterates the WHOLE corpus (rule 7 subjects: the user's inventory AND the packaged
gauntlet) and classifies every deck per the locked combo-litmus rules, writing a
restartable per-deck **ledger** and a **corpus classification manifest**.

Only the CHEAP ``classify`` stage runs here — a pure lookup pass over the combo lake
(:mod:`pipeline.transforms.combo_detect`), NO XMage/ECJ/Forge. The heavier
``author`` -> ``compile`` -> ``gate`` stages are DESIGNED into the same ledger (see
:data:`STAGES` / :func:`stage_rank`) but are RUN LATER, under a resource monitor, by a
separate staged invocation — they are stubbed here (:func:`run_author` et al.).

The locked classify rules implemented (see the design doc §5):

* **Rule 1 (drive/thin)** — DRIVE iff :func:`~pipeline.transforms.combo_detect.win_combos_in_deck`
  returns ≥1 game-WIN combo whose pieces are all in the 99; else THIN.
* **Rule 2 (tiebreak)** — among qualifying win-combos choose the FEWEST-piece one,
  preferring one the COMMANDER participates in; ≥2 equally-central lines ⇒ deterministic
  first pick + ``multi-combo`` flag.
* **Rule 4 (Φ mode)** — a DRIVE deck is ``drive-dedicated`` iff (commander is a combo
  piece) OR (≥3 dedicated tutors present) OR (Strategy names the combo); else
  ``drive-capable``. Borderline/unknown (no Strategy, indeterminate tutors) defaults to
  the SAFE ``drive-capable`` + a ``phi-mode-defaulted`` flag. THIN ⇒ ``thin``.

``wincon_style`` is a STATIC proxy (no sim is run here): a DRIVE deck wins by its combo
(:data:`DRIVE_WINCON_STYLE`), a THIN CP7 goldfish wins by combat (:data:`THIN_WINCON_STYLE`).
The real telemetry ``wincon`` (combat/burn/mill/other — see :mod:`pipeline.sim.telemetry`)
is only observable once the AUTHOR→GATE stages run games, so classify uses the proxy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pipeline.transforms.combo_detect import (
    Combo,
    _norm_name,
    analyze_deck_win_combos,
    is_game_win_result,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from pipeline.contracts.models import Deck

log = logging.getLogger('make_magic.sim.driver_batch')

# --------------------------------------------------------------------------- #
# Stages (the shared ledger vocabulary; only `classified` runs here).
# --------------------------------------------------------------------------- #

#: The ordered ledger stages. A deck at/after the target stage is skipped on re-run.
STAGES: tuple[str, ...] = ('classified', 'authored', 'compiled', 'gated')
_STAGE_RANK = {name: i for i, name in enumerate(STAGES)}


def stage_rank(stage: str) -> int:
    """The ordinal of a ledger ``stage`` (``classified`` < ``authored`` < ``compiled`` < ``gated``)."""
    return _STAGE_RANK[stage]


#: Static wincon-style proxies (no sim run here — see the module docstring).
DRIVE_WINCON_STYLE = 'combo'
THIN_WINCON_STYLE = 'combat'

#: A light rule-4 tutor heuristic: a card whose normalized name is in this set (or whose
#: name contains "tutor") counts as a "dedicated tutor" for the combo pieces. This is a
#: deliberately coarse, ALWAYS-AVAILABLE signal (no oracle text needed): the strong,
#: precise signals for `drive-dedicated` are commander-is-a-piece and the Strategy naming
#: the combo; tutor-count is the fallback. Curated Forge staple gauntlet decks carry ~no
#: tutors, so this rarely fires there (they lean on commander-is-piece / default to capable).
_KNOWN_TUTORS: frozenset[str] = frozenset(
    _norm_name(n)
    for n in (
        'Demonic Tutor',
        'Vampiric Tutor',
        'Diabolic Intent',
        'Imperial Seal',
        'Grim Tutor',
        'Diabolic Tutor',
        'Enlightened Tutor',
        'Mystical Tutor',
        'Worldly Tutor',
        'Personal Tutor',
        'Gamble',
        'Idyllic Tutor',
        'Wishclaw Talisman',
        'Scheming Symmetry',
        'Finale of Devastation',
        'Chord of Calling',
        'Green Sun\'s Zenith',
        'Eladamri\'s Call',
        'Signal the Clans',
        'Fabricate',
        'Whir of Invention',
        'Tinker',
        'Muddle the Mixture',
        'Merchant Scroll',
        'Spellseeker',
    )
)


def _is_tutor(name: str) -> bool:
    key = _norm_name(name)
    return key in _KNOWN_TUTORS or 'tutor' in key


# --------------------------------------------------------------------------- #
# Corpus subject.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CorpusDeck:
    """One corpus subject: the static facts the classify stage needs (no sim state).

    ``card_names`` is the full 99/deck identity (commanders INCLUDED). ``source`` is
    ``inventory`` | ``gauntlet``; ``fmt`` is ``commander`` | ``constructed``.
    """

    deck_id: str
    name: str
    source: str
    fmt: str
    card_names: tuple[str, ...]
    commander_names: tuple[str, ...] = ()
    strategy: str | None = None


# --------------------------------------------------------------------------- #
# Classify (rules 1/2/4) — pure, testable with synthetic combos.
# --------------------------------------------------------------------------- #


def _combo_dict(combo: Combo) -> dict[str, Any]:
    return {
        'variant_id': combo.variant_id,
        'card_names': list(combo.card_names),
        'result': combo.result,
    }


def _commander_participates(combo: Combo, commander_names: Sequence[str]) -> bool:
    cmd = {_norm_name(c) for c in commander_names}
    return any(_norm_name(n) in cmd for n in combo.card_names)


def _choose_combo(
    win_combos: list[Combo],
    commander_names: Sequence[str],
) -> tuple[Combo, bool]:
    """Rule-2 tiebreak: fewest pieces, commander-participation preferred, deterministic.

    Returns ``(chosen, multi_combo)`` where ``multi_combo`` is True iff ≥2 win lines are
    EQUALLY CENTRAL (share the winning ``(piece-count, commander-participation)`` key).
    """

    def key(c: Combo) -> tuple[int, int, str]:
        # fewest pieces first; commander-participation preferred (0 before 1); then
        # variant_id for a total, input-order-independent deterministic order.
        return (len(c.card_names), 0 if _commander_participates(c, commander_names) else 1, c.variant_id)

    ordered = sorted(win_combos, key=key)
    chosen = ordered[0]
    top = key(chosen)[:2]
    equally_central = sum(1 for c in ordered if key(c)[:2] == top)
    return chosen, equally_central >= 2


def _strategy_names_combo(strategy: str | None, combo: Combo) -> bool:
    """Rule-4 signal: the Strategy text names the combo as the win (a coarse heuristic).

    True when the Strategy is present and mentions at least ``min(2, len(pieces))`` of the
    chosen combo's pieces by name — enough to say the deck DECLARES this line as its plan.
    """
    if not strategy or not strategy.strip():
        return False
    text = strategy.casefold()
    named = sum(1 for n in combo.card_names if _norm_name(n) in text)
    return named >= min(2, len(combo.card_names))


def _phi_mode(
    deck: CorpusDeck,
    chosen: Combo,
) -> tuple[str, bool]:
    """Rule-4 archetype for a DRIVE deck. Returns ``(archetype, defaulted)``.

    ``defaulted`` is True when we fell back to the SAFE ``drive-capable`` on a
    borderline/unknown deck (no Strategy, not commander-piece, tutors indeterminate) —
    the ``phi-mode-defaulted`` flag.
    """
    if _commander_participates(chosen, deck.commander_names):
        return 'drive-dedicated', False
    tutor_count = sum(1 for n in deck.card_names if _is_tutor(n))
    if tutor_count >= 3:
        return 'drive-dedicated', False
    if _strategy_names_combo(deck.strategy, chosen):
        return 'drive-dedicated', False
    # Default to the thin-Φ safe side; flag it as borderline when we truly lacked signal
    # (no Strategy doc to consult — e.g. a raw gauntlet .dck).
    defaulted = not (deck.strategy and deck.strategy.strip())
    return 'drive-capable', defaulted


def classify_deck(deck: CorpusDeck, combos: list[Combo]) -> dict[str, Any]:
    """Classify one corpus deck per rules 1/2/4. Returns a JSON-serializable ledger row body.

    Keys: ``drive`` (bool), ``archetype`` (drive-dedicated|drive-capable|thin),
    ``detected_win_combos`` (list of {variant_id, card_names, result}), ``chosen_combo``
    (one of them, or None for THIN), ``wincon_style``, ``flags`` (multi-combo,
    phi-mode-defaulted, ...).
    """
    identity = {*deck.card_names, *deck.commander_names}
    analysis = analyze_deck_win_combos(identity, combos)
    win_combos = analysis.wins
    flags: list[str] = []

    if not win_combos:
        # THIN. If the deck holds an infinite-mana combo with no recognized sink, FLAG it
        # (record the borderline combo) so the orchestrator can add it to the review batch.
        body: dict[str, Any] = {
            'drive': False,
            'archetype': 'thin',
            'detected_win_combos': [],
            'chosen_combo': None,
            'wincon_style': THIN_WINCON_STYLE,
            'flags': flags,
        }
        if analysis.flagged_mana_combos:
            flags.append('infinite-mana-no-sink')
            body['flagged_mana_combos'] = [_combo_dict(c) for c in analysis.flagged_mana_combos]
        return body

    chosen, multi = _choose_combo(win_combos, deck.commander_names)
    if multi:
        flags.append('multi-combo')
    # Record HOW the chosen combo was promoted: the widened result predicate, or the
    # deck-aware mana-sink rule (bare infinite mana + an in-deck lethal sink).
    promotion = 'predicate' if is_game_win_result(chosen.result) else 'mana-sink'
    archetype, defaulted = _phi_mode(deck, chosen)
    if defaulted:
        flags.append('phi-mode-defaulted')

    result_body: dict[str, Any] = {
        'drive': True,
        'archetype': archetype,
        'detected_win_combos': [_combo_dict(c) for c in win_combos],
        'chosen_combo': _combo_dict(chosen),
        'promotion_rule': promotion,
        'wincon_style': DRIVE_WINCON_STYLE,
        'flags': flags,
    }
    if analysis.mana_sink_wins:
        result_body['mana_sinks_present'] = analysis.mana_sinks_present
    return result_body


# --------------------------------------------------------------------------- #
# The restartable per-deck ledger (atomic temp+replace; last write per deck wins).
# --------------------------------------------------------------------------- #


@dataclass
class Ledger:
    """A restartable per-deck ledger backed by a JSONL file (one row per deck_id).

    Rows are keyed by ``deck_id``; the LAST recorded row for a deck wins (a later stage
    supersedes an earlier one). Every :meth:`record` rewrites the whole file atomically
    (write temp + ``os.replace``), so a crash mid-write never corrupts the ledger.
    """

    path: Path
    _rows: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.is_file():
            for line in self.path.read_text(encoding='utf-8').splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                self._rows[row['deck_id']] = row

    def stage_of(self, deck_id: str) -> str | None:
        row = self._rows.get(deck_id)
        return row.get('stage') if row else None

    def row(self, deck_id: str) -> dict[str, Any]:
        return self._rows[deck_id]

    def rows(self) -> list[dict[str, Any]]:
        return list(self._rows.values())

    def record(self, row: dict[str, Any]) -> None:
        """Upsert ``row`` (keyed by ``row['deck_id']``) and atomically rewrite the file."""
        self._rows[row['deck_id']] = row
        self._flush()

    def _flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = '\n'.join(json.dumps(r, sort_keys=True) for r in self._rows.values())
        if payload:
            payload += '\n'
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix='.ledger.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


# --------------------------------------------------------------------------- #
# The classify stage runner (restartable).
# --------------------------------------------------------------------------- #


def default_ledger_path(data_dir: str | os.PathLike[str] | None = None) -> Path:
    """The default ledger path: ``<data_dir>/sim/driver_batch/ledger.jsonl``."""
    from pipeline import store

    root = Path(data_dir) if data_dir is not None else store.StorePaths.resolve().data_dir
    return root / 'sim' / 'driver_batch' / 'ledger.jsonl'


def run_classify(
    decks: Iterable[CorpusDeck],
    combos: list[Combo],
    *,
    ledger_path: str | os.PathLike[str],
    target_stage: str = 'classified',
) -> dict[str, int]:
    """Run the CLASSIFY stage over ``decks``, recording each into a restartable ledger.

    A deck already at/after ``target_stage`` is SKIPPED (restartable). Returns a
    ``{'classified': n, 'skipped': m}`` counter.
    """
    ledger = Ledger(Path(ledger_path))
    target = stage_rank(target_stage)
    classified = skipped = 0
    for deck in decks:
        existing = ledger.stage_of(deck.deck_id)
        if existing is not None and stage_rank(existing) >= target:
            skipped += 1
            continue
        body = classify_deck(deck, combos)
        ledger.record(
            {
                'deck_id': deck.deck_id,
                'name': deck.name,
                'source': deck.source,
                'fmt': deck.fmt,
                'stage': 'classified',
                **body,
            }
        )
        classified += 1
    return {'classified': classified, 'skipped': skipped}


# --------------------------------------------------------------------------- #
# Reserved heavier stages (DESIGNED into the ledger; RUN LATER under a monitor).
# --------------------------------------------------------------------------- #


def _driver_uuid(deck_id: str) -> str:
    """A deterministic, filesystem-safe driver identity for a corpus ``deck_id``.

    Corpus deck_ids are gauntlet-relative paths (``commander/cedh/x.dck``) or
    ``inventory/<uuid>`` — neither is a legal single-segment dir name (slashes) nor a stable
    Java package leaf. Hashing gives BOTH stages (author + compile) an identical, path-safe
    ``deck.uuid`` so :func:`pipeline.sim.drivers.driver_dir` keys a single per-deck dir and the
    emitted FQCN is stable across re-runs.
    """
    return hashlib.sha1(deck_id.encode('utf-8')).hexdigest()


def _combo_from_row(row: dict[str, Any]) -> Combo:
    """Reconstruct the chosen win :class:`Combo` from a classified ledger row.

    The classify stage persisted the chosen combo's identity (``variant_id`` / ``card_names`` /
    ``result``) verbatim, so the seed can be rebuilt without re-loading the combo lake. Oracle
    ids are irrelevant to seeding (:func:`~pipeline.sim.driver_authoring.seed_quad_from_combo`
    reads only names + result), so they are filled with placeholders.
    """
    cc = row.get('chosen_combo') or {}
    names = tuple(cc.get('card_names') or ())
    return Combo(
        variant_id=str(cc.get('variant_id', '')),
        card_names=names,
        card_oracle_ids=('',) * len(names),
        result=str(cc.get('result') or ''),
    )


def _deck_for_row(row: dict[str, Any]) -> Deck:
    """A minimal :class:`~pipeline.contracts.models.Deck` carrying the stable driver identity.

    Only ``uuid`` (the driver dir / FQCN key) and ``name`` are load-bearing for AUTHOR + COMPILE
    (render + ECJ). ``cards`` is empty — the seeded quad's card logic rides in the combo, and no
    stage here runs a sim that would need the 99.
    """
    from pipeline.contracts.models import Deck

    return Deck(name=str(row.get('name', row['deck_id'])), uuid=_driver_uuid(row['deck_id']), cards=[])


def authored_dir(ledger_path: str | os.PathLike[str], deck_id: str) -> Path:
    """``<ledger_dir>/authored/<deck_id>/`` — the per-deck rendered-driver artifact dir (P4 reads it)."""
    return Path(ledger_path).parent / 'authored' / deck_id


def run_author(
    *,
    ledger_path: str | os.PathLike[str],
    target_stage: str = 'authored',
) -> dict[str, int]:
    """Run the AUTHOR stage over every DRIVE ledger row not yet at ``authored`` (restartable).

    For each DRIVE row: reconstruct the chosen :class:`Combo`, seed a
    :class:`~pipeline.sim.driver_authoring.QuadSpec`
    (:func:`~pipeline.sim.driver_authoring.seed_quad_from_combo`) at the row's ``archetype``,
    render ``Driver.java`` (:func:`~pipeline.sim.driver_authoring.render_quad_driver`), persist it
    to ``<ledger_dir>/authored/<deck_id>/Driver.java`` (so P4 can read every driver), and run the
    AC8 static guardrail screen. A CLEAN render advances the row to ``authored`` (recording the
    spec archetype, fqcn, artifact path, driver uuid). A guardrail violation is NOT advanced — the
    artifact is still written for inspection, the issue is recorded, and a ``guardrail-error`` flag
    is set (a per-deck flag, never a batch abort).

    THIN rows (``drive`` false — bare CP7, no in-deck win-combo) are advanced to ``authored`` as a
    documented NO-OP with ``thin: true`` and NO artifact: a thin deck runs pure driverless CP7, so
    there is nothing to render or compile.

    Returns ``{'authored', 'thin', 'skipped', 'guardrail_failed'}`` counters.
    """
    from pipeline.sim.driver_authoring import (
        GuardrailViolation,
        check_quad_guardrails,
        driver_fqcn,
        render_quad_driver,
        seed_quad_from_combo,
    )

    ledger = Ledger(Path(ledger_path))
    target = stage_rank(target_stage)
    authored = thin = skipped = guardrail_failed = 0
    for row in ledger.rows():
        stage = row.get('stage')
        if stage is not None and stage_rank(stage) >= target:
            skipped += 1
            continue

        deck = _deck_for_row(row)
        duid = deck.uuid

        if not row.get('drive'):
            ledger.record({**row, 'stage': 'authored', 'thin': True, 'driver_uuid': duid})
            thin += 1
            continue

        fqcn = driver_fqcn(deck)
        spec = seed_quad_from_combo(_combo_from_row(row), archetype=str(row['archetype']))
        source = render_quad_driver(deck, spec)

        art = authored_dir(ledger_path, str(row['deck_id']))
        art.mkdir(parents=True, exist_ok=True)
        art_path = art / 'Driver.java'
        art_path.write_text(source, encoding='utf-8')

        try:
            check_quad_guardrails(source)
        except GuardrailViolation as exc:
            ledger.record(
                {
                    **row,
                    'driver_uuid': duid,
                    'fqcn': fqcn,
                    'archetype_spec': spec.archetype,
                    'authored_path': str(art_path),
                    'guardrail_ok': False,
                    'guardrail_issue': str(exc),
                    'flags': sorted({*row.get('flags', []), 'guardrail-error'}),
                }
            )
            guardrail_failed += 1
            continue

        ledger.record(
            {
                **row,
                'stage': 'authored',
                'driver_uuid': duid,
                'fqcn': fqcn,
                'archetype_spec': spec.archetype,
                'authored_path': str(art_path),
                'guardrail_ok': True,
            }
        )
        authored += 1
    return {'authored': authored, 'thin': thin, 'skipped': skipped, 'guardrail_failed': guardrail_failed}


def run_compile(
    *,
    ledger_path: str | os.PathLike[str],
    data_dir: str | os.PathLike[str] | None = None,
    target_stage: str = 'compiled',
) -> dict[str, int]:
    """Run the COMPILE stage (ECJ-only) over every DRIVE row at ``authored`` (restartable).

    For each DRIVE row that is authored-or-later but not yet ``compiled``: re-seed the quad and
    ECJ-compile it against the local dist via
    :func:`pipeline.sim.driver_gate.compile_quad_driver` (render + guardrail + ECJ; NO XMage sim).
    Success advances the row to ``compiled`` (recording ``classes_dir`` + ``fqcn``, clearing any
    prior ``compile_error``/flag). An ECJ failure records the STRUCTURED diagnostics in the row,
    sets a ``compile-error`` flag, leaves the row at ``authored``, and CONTINUES — a per-deck
    failure is a flag, never a batch abort.

    Returns ``{'compiled', 'failed', 'skipped'}`` counters. THIN rows carry no driver and are
    skipped silently (not counted).
    """
    from pipeline.sim.driver_authoring import seed_quad_from_combo
    from pipeline.sim.driver_compile import DriverCompileError
    from pipeline.sim.driver_gate import compile_quad_driver

    ledger = Ledger(Path(ledger_path))
    target = stage_rank(target_stage)
    authored_rank = stage_rank('authored')
    compiled = failed = skipped = 0
    for row in ledger.rows():
        if not row.get('drive'):
            continue  # THIN: bare CP7, nothing to compile.
        stage = row.get('stage')
        if stage is None or stage_rank(stage) < authored_rank:
            skipped += 1  # not authored yet — run_author must land it first.
            continue
        if stage_rank(stage) >= target:
            skipped += 1  # already compiled — restartable skip.
            continue

        deck = _deck_for_row(row)
        spec = seed_quad_from_combo(_combo_from_row(row), archetype=str(row['archetype']))
        try:
            classes_dir, fqcn = compile_quad_driver(deck, spec, data_dir=data_dir)
        except DriverCompileError as exc:
            diags = [
                {'file': d.file, 'line': d.line, 'severity': d.severity, 'message': d.message}
                for d in exc.result.diagnostics
            ]
            cleaned = {k: v for k, v in row.items() if k != 'classes_dir'}
            ledger.record(
                {
                    **cleaned,
                    'stage': 'authored',
                    'compile_error': {'diagnostics': diags, 'raw_stderr': exc.result.raw_stderr[:4000]},
                    'flags': sorted({*row.get('flags', []), 'compile-error'}),
                }
            )
            failed += 1
            continue

        cleaned = {k: v for k, v in row.items() if k != 'compile_error'}
        ledger.record(
            {
                **cleaned,
                'stage': 'compiled',
                'classes_dir': classes_dir,
                'fqcn': fqcn,
                'flags': sorted(f for f in row.get('flags', []) if f != 'compile-error'),
            }
        )
        compiled += 1
    return {'compiled': compiled, 'failed': failed, 'skipped': skipped}


def run_gate(*_args: object, **_kw: object) -> None:
    """Reserved GATE stage — goldfish-gate each compiled driver
    (:func:`pipeline.sim.driver_gate.gate_driver`). Run later."""
    raise NotImplementedError('gate stage runs later (staged, resource-monitored) — see module docstring')


# --------------------------------------------------------------------------- #
# Corpus enumeration (rule 7 subjects: inventory AND gauntlet).
# --------------------------------------------------------------------------- #

_COMMANDER = 'commander'


def _parse_dck(dck_text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Parse a Forge ``.dck`` into ``(all_card_names, commander_names)``.

    Walks the ``[Main]`` / ``[Commander]`` / ``[Sideboard]`` sections (case-insensitive,
    pinned-printing tolerant), mirroring :func:`pipeline.sim.run._dck_card_names`. All
    named cards (commanders included) land in the first tuple; the ``[Commander]`` section's
    names also land in the second.
    """
    names: list[str] = []
    commanders: list[str] = []
    section: str | None = None
    for raw in dck_text.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if stripped.startswith('['):
            low = stripped.lower()
            section = low if low in ('[main]', '[commander]', '[sideboard]') else None
            continue
        if section is None:
            continue
        qty, _, name = stripped.partition(' ')
        name = name.split('|', 1)[0].strip()
        if qty.isdigit() and name:
            names.append(name)
            if section == '[commander]':
                commanders.append(name)
    return tuple(names), tuple(commanders)


def enumerate_gauntlet() -> list[CorpusDeck]:
    """Enumerate every packaged gauntlet ``.dck`` (rule 7 gauntlet subjects), recursively.

    Walks ``pipeline/data/gauntlet/**`` — flat curated files AND named bundle sub-dirs
    (guilds, precons, cedh, ...). ``fmt`` is read from the top-level dir
    (``commander`` vs everything-else -> ``constructed``); ``deck_id`` is the gauntlet-root
    relative path (unique across bundles); ``name`` is the file stem.
    """
    import pipeline as _pkg

    root = Path(_pkg.__file__).resolve().parent / 'data' / 'gauntlet'
    decks: list[CorpusDeck] = []
    for path in sorted(root.rglob('*.dck')):
        rel = path.relative_to(root)
        fmt = _COMMANDER if rel.parts and rel.parts[0] == _COMMANDER else 'constructed'
        names, commanders = _parse_dck(path.read_text(encoding='utf-8'))
        decks.append(
            CorpusDeck(
                deck_id=str(rel),
                name=path.stem,
                source='gauntlet',
                fmt=fmt,
                card_names=names,
                commander_names=commanders,
                strategy=None,
            )
        )
    return decks


def enumerate_inventory() -> tuple[list[CorpusDeck], str]:
    """Enumerate the user's own decks (rule 7 inventory subjects). Returns ``(decks, source)``.

    ``source`` is ``offline-local`` (LocalYaml enumerated, possibly empty), ``airtable``, or
    ``unavailable-offline: <reason>`` when the backend needs creds this environment lacks —
    in which case ``decks`` is ``[]`` and the caller proceeds with the gauntlet corpus.

    Enumeration prefers whatever the resolved backend is, but if that backend needs creds
    this environment lacks (e.g. an onboarded-but-key-less Airtable), it FALLS BACK to the
    offline LocalYaml adapter rather than blocking — so a genuinely-empty offline inventory
    reads as ``offline-local`` (0 decks), distinct from a hard ``unavailable-offline``.
    """
    from pipeline.collection.store import get_store, resolve_backend

    try:
        backend = resolve_backend()
        store = get_store()
        deck_objs = store.list_decks()
        label = 'airtable' if backend == 'airtable' else 'offline-local'
    except Exception as primary:  # creds/network are an expected non-blocker in this pass.
        try:
            from pipeline.collection import resolver as resolver_mod
            from pipeline.collection.adapters.local_yaml import LocalYamlStore

            store = LocalYamlStore(resolver=resolver_mod.default_card_resolver())
            deck_objs = store.list_decks()
            label = 'offline-local'
        except Exception as fallback:  # both backends unusable -> non-blocking miss.
            return [], (
                f'unavailable-offline: {type(primary).__name__}: {primary} '
                f'(local fallback: {type(fallback).__name__}: {fallback})'
            )

    decks: list[CorpusDeck] = []
    for deck in deck_objs:
        cards = tuple(c.name for c in deck.cards)
        commanders = tuple(c.name for c in deck.commanders)
        is_commander = bool(commanders) or (deck.format or '').strip().lower() in ('commander', 'edh')
        decks.append(
            CorpusDeck(
                deck_id=f'inventory/{deck.uuid}',
                name=deck.name,
                source='inventory',
                fmt=_COMMANDER if is_commander else 'constructed',
                card_names=cards,
                commander_names=commanders,
                strategy=deck.strategy,
            )
        )
    return decks, label


# --------------------------------------------------------------------------- #
# Manifest (summary header + per-deck rows).
# --------------------------------------------------------------------------- #


def build_manifest(ledger: Ledger, *, inventory_source: str, corpus_counts: dict[str, int]) -> dict[str, Any]:
    """Aggregate the ledger into a corpus classification manifest (header summary + rows)."""
    rows = sorted(ledger.rows(), key=lambda r: (r.get('source', ''), r.get('deck_id', '')))
    drive = [r for r in rows if r.get('drive')]
    thin = [r for r in rows if not r.get('drive')]

    def _dist(key: str, subset: list[dict[str, Any]]) -> dict[str, int]:
        out: dict[str, int] = {}
        for r in subset:
            out[r.get(key, '?')] = out.get(r.get(key, '?'), 0) + 1
        return dict(sorted(out.items()))

    flag_counts: dict[str, int] = {}
    for r in rows:
        for f in r.get('flags', []):
            flag_counts[f] = flag_counts.get(f, 0) + 1

    drive_list = [
        {
            'name': r['name'],
            'deck_id': r['deck_id'],
            'source': r['source'],
            'fmt': r['fmt'],
            'archetype': r['archetype'],
            'chosen_combo_result': (r.get('chosen_combo') or {}).get('result'),
            'chosen_combo_cards': (r.get('chosen_combo') or {}).get('card_names'),
            'flags': r.get('flags', []),
        }
        for r in drive
    ]

    return {
        'header': {
            'stage': 'classified',
            'inventory_source': inventory_source,
            'corpus_counts': corpus_counts,
            'total_decks': len(rows),
            'drive_count': len(drive),
            'thin_count': len(thin),
            'archetype_distribution': _dist('archetype', rows),
            'wincon_style_distribution': _dist('wincon_style', rows),
            'flag_counts': dict(sorted(flag_counts.items())),
        },
        'drive_decks': drive_list,
        'decks': rows,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix='.manifest.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh, indent=2, sort_keys=False)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


#: Where the orchestrator reads the manifest (plan dir) — data-dir copy is written too.
_PLAN_MANIFEST = Path(
    os.path.expanduser(
        '~/.claude/plans/trippersham/magic-tools/2026-08-24-productionize-driver-authoring/'
        'corpus-classification-manifest.json'
    )
)


def run(argv: list[str] | None = None) -> None:
    """Enumerate the corpus, run the CLASSIFY stage, and write the manifest + ledger.

    Idempotent/restartable: re-running skips decks already classified in the ledger.
    """
    import argparse

    from pipeline.transforms.combo_detect import ensure_combo_lake, load_combos

    parser = argparse.ArgumentParser(
        prog='driver-batch',
        description='Corpus combo-litmus classifier (classify stage).',
    )
    parser.add_argument(
        '--ledger', default=None, help='Ledger JSONL path (default: <data_dir>/sim/driver_batch/ledger.jsonl).'
    )
    parser.add_argument(
        '--manifest', default=None, help='Extra manifest path (besides the plan-dir + data-dir copies).'
    )
    parser.add_argument('--gauntlet-only', action='store_true', help='Skip inventory enumeration.')
    args = parser.parse_args([] if argv is None else argv)

    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')

    ledger_path = Path(args.ledger) if args.ledger else default_ledger_path()

    ensure_combo_lake()
    combos = load_combos()

    gauntlet = enumerate_gauntlet()
    if args.gauntlet_only:
        inventory, inv_source = [], 'skipped (--gauntlet-only)'
    else:
        inventory, inv_source = enumerate_inventory()

    corpus = [*inventory, *gauntlet]
    counts = {
        'total': len(corpus),
        'inventory': len(inventory),
        'gauntlet': len(gauntlet),
        'combos_in_lake': len(combos),
    }
    log.info('corpus: %d decks (inventory=%d [%s], gauntlet=%d); %d combos in lake.',
             len(corpus), len(inventory), inv_source, len(gauntlet), len(combos))

    result = run_classify(corpus, combos, ledger_path=ledger_path)
    log.info('classify: %d classified, %d skipped.', result['classified'], result['skipped'])

    ledger = Ledger(ledger_path)
    manifest = build_manifest(ledger, inventory_source=inv_source, corpus_counts=counts)

    data_manifest = ledger_path.parent / 'corpus-classification-manifest.json'
    _write_json(data_manifest, manifest)
    _write_json(_PLAN_MANIFEST, manifest)
    if args.manifest:
        _write_json(Path(args.manifest), manifest)

    h = manifest['header']
    print(json.dumps(h, indent=2))
    print(f'\nledger:   {ledger_path}')
    print(f'manifest: {data_manifest}')
    print(f'manifest: {_PLAN_MANIFEST}')


def main(argv: list[str] | None = None) -> None:
    run(argv)


if __name__ == '__main__':
    main()
