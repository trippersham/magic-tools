"""Engine-synthesizer (L5) -- backend, prompt, reader.

This module is the production plumbing that resolves a deck's deterministic
strategy-graph outputs into the ONE core Engine, with NO separate API key and NO
human-agent-in-the-loop: the synthesis backend is **headless Claude Code**
(``claude -p``) running under ambient auth, and the result is materialised to the
:mod:`.engine_cache` store for determinism + amortisation.

Layers
------
* :func:`build_synthesis_input` -- the deterministic engine-synthesizer *input* for one deck
  (``compute_facts`` + ``name_deck``, no LLM).  This is the single source the
  ``scripts/extract_roleb_inputs.py`` tool delegates to.
* :func:`build_prompt` -- the vendored synthesis rules (``roleb_prompt.md``) +
  one deck's input, asking for a strict single-line Engine JSON.
* :class:`SynthBackend` / :class:`ClaudeCliBackend` / :class:`MockBackend` --
  the pluggable synthesis backend.  ``ClaudeCliBackend`` shells ``claude -p``;
  the mock is for tests and offline runs.
* :func:`parse_engine_response` / :func:`validate_engine` -- parse the model's
  text to a strict ``{strategy, win_condition, key_pieces, confidence}`` dict and
  verify well-formedness + that ``key_pieces`` are in-deck.
* :func:`resolve_engine` -- the production reader: cache-hit returns the stored
  Engine; miss triggers synthesis (``claude -p`` -> validate -> cache -> return).

Tier-1 (deterministic ``resolve(deck)`` archetype identity) is ALWAYS available
and independent of this module; the Engine here is the Tier-2 enrichment.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Protocol

from pipeline.transforms.strategy import engine_cache
from pipeline.transforms.strategy.assets import StrategyAssets, load_assets
from pipeline.transforms.strategy.config import StrategyConfig
from pipeline.transforms.strategy.engine_cache import ResolvedEngine

# ---------------------------------------------------------------------------
# Vendored synthesis prompt (self-contained; no plan-tree dependency)
# ---------------------------------------------------------------------------

_PROMPT_PATH = Path(__file__).resolve().parent / 'roleb_prompt.md'

#: Default headless Claude model for synthesis.  Explicit id so the pin is
#: honoured (the ``sonnet`` alias is not recognised by every CLI build).
DEFAULT_MODEL = 'claude-sonnet-4-5'


def load_prompt_rules() -> str:
    """Return the vendored engine-synthesizer rules (``roleb_prompt.md``)."""
    return _PROMPT_PATH.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# Deterministic engine-synthesizer input builder (no LLM) -- single source of truth
# ---------------------------------------------------------------------------


def build_synthesis_input(
    deck: dict,
    combos: list[dict] | None = None,
    *,
    slug: str | None = None,
    cfg: StrategyConfig | None = None,
    assets: StrategyAssets | None = None,
) -> dict:
    """Build the engine-synthesizer input for one deck (deterministic, no LLM).

    Runs ``compute_facts`` + ``name_deck`` on the CURRENT engine and extracts the
    commanders, elected engine candidates, archetype identity, detected combos,
    commander-loop probe, and the naming-layer resolution.  This is exactly the
    input the synthesiser sees, and the thing the cache's staleness key hashes.
    """
    from pipeline.transforms.strategy.naming import name_deck
    from pipeline.transforms.strategy.strategy_graph import compute_facts

    if assets is None:
        assets = load_assets(cfg)
    elif cfg is not None and assets.cfg is not cfg:
        raise ValueError('cfg and assets.cfg disagree; pass only one')

    ctx = compute_facts(deck, combos, assets)
    named = name_deck(ctx)

    commanders = [
        {'name': c['name'], 'oracle_text': (c.get('oracle_text') or '')[:500]}
        for c in ctx.cards
        if c.get('role') == 'commander'
    ]

    iso = ctx.facts['engines']['engine_isolation']
    elected = iso.get('elected', [])

    archetype_identity = named['archetype_identity']

    detected_combos = []
    for c in ctx.facts['core_combos']:
        detected_combos.append(
            {
                'variant_id': c['variant_id'],
                'label': c['label'],
                'result': c['result'],
                'requires': c['requires'],
                'n_pieces': c['n_pieces'],
                'win': c['win'],
                'win_kind': c['win_kind'],
            }
        )

    cmd_loop = iso.get('commander_loop')

    naming_resolution = {
        'named_strategy': named['named_strategy'],
        'combo_override_applied': named['combo_override_applied'],
        'combo_identity': named['combo_identity'],
        'pre_combo_name': named['pre_combo_name'],
        'confidence': named['confidence'],
        'combo_override_f4_blocked': named['combo_override_f4_blocked'],
    }

    # In-deck card roster (canonical names, non-basic-land) so the synthesiser
    # can ground ``key_pieces`` in cards that are ACTUALLY present (R5).  Without
    # this the model can only safely cite commanders + combo pieces and tends to
    # name plausible archetype staples that aren't in this particular list.
    in_deck_cards = sorted(
        {_canonical_card_name(c['name']) for c in ctx.cards if c.get('name') and not _is_basic_land(c)}
    )

    return {
        'slug': slug if slug is not None else deck.get('name', 'deck'),
        'deck_name': deck.get('name', slug or 'deck'),
        'commanders': commanders,
        'elected': elected,
        'archetype_identity': archetype_identity,
        'detected_combos': detected_combos,
        'commander_loop': cmd_loop,
        'naming_resolution': naming_resolution,
        'in_deck_cards': in_deck_cards,
        'n_nonland': ctx.n_nonland,
        'n_cards': len(ctx.cards),
    }


def _is_basic_land(card: dict) -> bool:
    """True for a basic land (excluded from the key-piece roster)."""
    name = _canonical_card_name(card.get('name', ''))
    if name in {'Plains', 'Island', 'Swamp', 'Mountain', 'Forest', 'Wastes'}:
        return True
    type_line = card.get('type_line') or card.get('type') or ''
    return 'Basic' in type_line and 'Land' in type_line


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_prompt(roleb_input: dict) -> str:
    """Assemble the synthesis prompt: vendored rules + one deck's input.

    Internal ``_``-prefixed keys are stripped so cosmetic flags never leak into
    the prompt.  The model is asked for EXACTLY one single-line JSON object.
    """
    clean = {k: v for k, v in roleb_input.items() if not k.startswith('_')}
    rules = load_prompt_rules()
    return (
        rules
        + '\n\n---\n\n## Deck to process (ONE deck)\n\n```json\n'
        + json.dumps([clean], indent=2, ensure_ascii=False)
        + '\n```\n\nOutput EXACTLY one JSON object on one line for this deck in '
        'the format `{"slug": "...", "engine": {...}}`. No other text.\n'
    )


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class SynthError(RuntimeError):
    """Raised when the synthesis backend fails or returns unusable output."""


class SynthBackend(Protocol):
    """A synthesis backend: prompt text in, raw model text out."""

    def synthesize(self, prompt: str) -> str:  # pragma: no cover - protocol
        ...


class ClaudeCliBackend:
    """Headless Claude Code backend (``claude -p``) under ambient auth.

    No ANTHROPIC_API_KEY is required or used -- ``claude -p`` authenticates
    ambiently.  The CLI is invoked with ``--output-format json``; the model's
    text is read from the ``.result`` field of the JSON envelope.  The model is
    pinned via ``--model`` (default :data:`DEFAULT_MODEL`).

    NOTE: nested ``claude -p`` works when the caller is itself a Claude Code
    session.  Calls are strictly sequential -- never fan this out in parallel.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        claude_bin: str = 'claude',
        timeout: int = 240,
    ) -> None:
        self.model = model
        self.claude_bin = claude_bin
        self.timeout = timeout

    def synthesize(self, prompt: str) -> str:
        cmd = [
            self.claude_bin,
            '-p',
            prompt,
            '--output-format',
            'json',
            '--model',
            self.model,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SynthError(f'claude -p invocation failed: {exc}') from exc

        if proc.returncode != 0:
            raise SynthError(f'claude -p exited {proc.returncode}: {proc.stderr.strip()[:500]}')

        try:
            envelope = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            raise SynthError(f'claude -p output was not JSON: {exc}; head={proc.stdout[:300]!r}') from exc

        if envelope.get('is_error'):
            raise SynthError(f'claude -p reported error: {envelope.get("result", "")[:300]}')

        result = envelope.get('result')
        if not isinstance(result, str):
            raise SynthError(f'claude -p envelope had no string .result: {envelope.keys()}')
        return result


class MockBackend:
    """Deterministic in-process backend for tests / offline runs.

    Supply either a ``responses`` dict (slug -> engine dict) or a callable
    ``fn(roleb_input_slug, prompt) -> engine dict``.  The backend wraps the
    engine in the ``{"slug": ..., "engine": ...}`` envelope the parser expects.
    """

    def __init__(
        self,
        responses: dict[str, dict] | None = None,
        *,
        fn=None,
        default_engine: dict | None = None,
    ) -> None:
        self.responses = responses or {}
        self.fn = fn
        self.default_engine = default_engine
        self.calls: list[str] = []

    def synthesize(self, prompt: str) -> str:
        self.calls.append(prompt)
        slug = _slug_from_prompt(prompt)
        if self.fn is not None:
            engine = self.fn(slug, prompt)
        elif slug in self.responses:
            engine = self.responses[slug]
        elif self.default_engine is not None:
            engine = self.default_engine
        else:
            raise SynthError(f'MockBackend has no response for slug {slug!r}')
        return json.dumps({'slug': slug, 'engine': engine})


def _slug_from_prompt(prompt: str) -> str:
    """Best-effort extraction of the deck slug from an assembled prompt."""
    m = re.search(r'"slug":\s*"([^"]+)"', prompt)
    return m.group(1) if m else ''


# ---------------------------------------------------------------------------
# Parse + validate
# ---------------------------------------------------------------------------

_ENGINE_KEYS = {'strategy', 'win_condition', 'key_pieces', 'confidence'}
_VALID_CONFIDENCE = {'HIGH', 'MED', 'LOW'}


def parse_engine_response(raw: str) -> dict:
    """Parse the model's raw text into a ``{slug, engine}`` dict.

    Tolerates ```` ```json ```` fences and leading/trailing prose: the first
    balanced top-level JSON object is extracted.  Raises :class:`SynthError` on
    unparseable output.
    """
    text = raw.strip()
    # Strip code fences if present.
    fence = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', text, re.DOTALL)
    candidate = fence.group(1) if fence else text
    # Fall back to the first {...} span.
    if not candidate.lstrip().startswith('{'):
        m = re.search(r'\{.*\}', candidate, re.DOTALL)
        if not m:
            raise SynthError(f'no JSON object found in response: {raw[:300]!r}')
        candidate = m.group(0)
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise SynthError(f'engine JSON parse failed: {exc}; text={candidate[:300]!r}') from exc


def validate_engine(engine: dict, deck_card_names: set[str] | None = None) -> list[str]:
    """Return a list of validation problems with an engine dict (empty == OK).

    Checks:
      * exactly the four required keys, all non-empty (``key_pieces`` non-empty
        list, ``confidence`` one of HIGH/MED/LOW);
      * every ``key_pieces`` card is in ``deck_card_names`` (when provided) --
        the R5 in-deck guarantee.
    """
    problems: list[str] = []

    missing = _ENGINE_KEYS - set(engine.keys())
    extra = set(engine.keys()) - _ENGINE_KEYS
    if missing:
        problems.append(f'missing keys: {sorted(missing)}')
    if extra:
        problems.append(f'unexpected keys: {sorted(extra)}')

    if not (engine.get('strategy') or '').strip():
        problems.append('empty strategy (R4 never-empty)')

    if not (engine.get('win_condition') or '').strip():
        problems.append('empty win_condition')

    kp = engine.get('key_pieces')
    if not isinstance(kp, list) or not kp:
        problems.append('key_pieces must be a non-empty list')
        kp = []

    conf = engine.get('confidence')
    if conf not in _VALID_CONFIDENCE:
        problems.append(f'confidence must be one of {sorted(_VALID_CONFIDENCE)}, got {conf!r}')

    if deck_card_names is not None and kp:
        # Canonicalise both sides: lake/dck-built decks carry a
        # ``name|SET|collector`` suffix, while the model names cards plainly.
        allowed = {_canonical_card_name(n) for n in deck_card_names}
        out_of_deck = [c for c in kp if _canonical_card_name(c) not in allowed]
        if out_of_deck:
            problems.append(f'key_pieces not in deck (R5): {out_of_deck}')

    return problems


def _canonical_card_name(name: str) -> str:
    """Strip the ``|SET|collector`` adapter suffix and surrounding whitespace.

    Lake/dck-built decks name cards ``Gonti, Canny Acquisitor|OTC|1``; curated
    fixtures and the synthesiser use the plain ``Gonti, Canny Acquisitor``.
    Canonicalising both sides makes the R5 in-deck check robust to the format.
    """
    return (name or '').split('|', 1)[0].strip()


def deck_card_names(deck: dict) -> set[str]:
    """Collect every card name in a deck (for the R5 in-deck check).

    Names are kept in their raw form here; :func:`validate_engine` canonicalises
    on comparison, so both the suffixed (lake) and plain (fixture) forms work.
    """
    names: set[str] = set()
    for c in deck.get('cards', []) or []:
        n = c.get('name')
        if n:
            names.add(n)
    return names


# ---------------------------------------------------------------------------
# Synthesis (one deck)
# ---------------------------------------------------------------------------


def synthesize_engine(
    roleb_input: dict,
    backend: SynthBackend,
    *,
    card_names: set[str] | None = None,
) -> ResolvedEngine:
    """Synthesise one deck's Engine via ``backend`` and validate it.

    Raises :class:`SynthError` if the backend output is malformed or fails
    validation (e.g. out-of-deck key_pieces).  The caller logs + skips on error.
    """
    prompt = build_prompt(roleb_input)
    raw = backend.synthesize(prompt)
    parsed = parse_engine_response(raw)
    engine = parsed.get('engine', parsed)  # accept bare engine or enveloped

    problems = validate_engine(engine, card_names)
    if problems:
        raise SynthError(f'engine failed validation: {problems}; engine={engine}')

    return ResolvedEngine.from_dict(engine)


# ---------------------------------------------------------------------------
# Production reader
# ---------------------------------------------------------------------------


def resolve_engine(
    deck: dict,
    combos: list[dict] | None = None,
    *,
    slug: str | None = None,
    backend: SynthBackend | None = None,
    cache_dir: Path | str | None = None,
    cfg: StrategyConfig | None = None,
    assets: StrategyAssets | None = None,
    allow_synth: bool = True,
    persist: bool = True,
) -> tuple[ResolvedEngine | None, dict]:
    """Return a deck's core Engine: cache-hit, or synthesise on miss.

    The production Tier-2 entry point.  Tier-1 (``resolve(deck)`` archetype
    identity) is always available and does not depend on this.

    Returns ``(engine, meta)`` where ``meta`` describes what happened::

        {'status': 'hit'|'synthesized'|'miss'|'error',
         'slug': ..., 'input_hash': ..., 'model_id': ..., 'problems': [...]}

    * ``hit``          -- fresh cached Engine returned (no LLM call).
    * ``synthesized``  -- cache miss, ``backend`` produced a valid Engine (cached
                          when ``persist``).
    * ``miss``         -- cache miss and ``allow_synth`` is False or no backend;
                          ``engine`` is ``None``.
    * ``error``        -- synthesis attempted but failed; ``engine`` is ``None``.
    """
    roleb_input = build_synthesis_input(
        deck,
        combos,
        slug=slug,
        cfg=cfg,
        assets=assets,
    )
    resolved_slug = roleb_input['slug']
    ih = engine_cache.input_hash(roleb_input)

    cache = engine_cache.load_cache(cache_dir)
    if cache.is_fresh(resolved_slug, ih):
        rec = cache.get(resolved_slug)
        assert rec is not None
        return rec.engine, {
            'status': 'hit',
            'slug': resolved_slug,
            'input_hash': ih,
            'model_id': rec.model_id,
            'problems': [],
        }

    if not allow_synth or backend is None:
        return None, {
            'status': 'miss',
            'slug': resolved_slug,
            'input_hash': ih,
            'model_id': '',
            'problems': [],
        }

    try:
        engine = synthesize_engine(
            roleb_input,
            backend,
            card_names=deck_card_names(deck),
        )
    except SynthError as exc:
        return None, {
            'status': 'error',
            'slug': resolved_slug,
            'input_hash': ih,
            'model_id': getattr(backend, 'model', ''),
            'problems': [str(exc)],
        }

    if persist:
        record = engine_cache.make_record(
            resolved_slug,
            roleb_input.get('deck_name', resolved_slug),
            roleb_input,
            engine,
            model_id=getattr(backend, 'model', ''),
        )
        engine_cache.persist(record, cache_dir)

    return engine, {
        'status': 'synthesized',
        'slug': resolved_slug,
        'input_hash': ih,
        'model_id': getattr(backend, 'model', ''),
        'problems': [],
    }
