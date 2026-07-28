"""Pydantic v2 boundary models — the edge contracts.

Per the data-architecture decision, Pydantic lives ONLY at the edges: these are
the objects a skill / MCP tool / future UI receives (object count ~1). The
pipeline middle stays columnar (DuckDB/Parquet), never materialized as Pydantic.

`model_json_schema()` over these models is the single source of truth for a
language-agnostic contract (MCP `inputSchema`/`outputSchema`, TS types,
auto-forms) — generate it on demand when a consumer needs it.

Design notes:
    - `extra="forbid"` on every model: a boundary contract should reject unknown
      keys loudly rather than silently accept typos / schema drift.
    - The `Card` hierarchy (`OwnedCard` / `ChaseCard` / `DeckCard`) is deliberately
      NOT a mirror of Airtable's schema: a base `Card` carries printing-independent
      identity + Scryfall enrichment (all enrichment NULLABLE so an unresolved
      pre-release card — name only — is representable), and each subclass adds the
      facts for one relationship to a card (ownership / intent / deck membership).
      `Trade` stands alone (a movement event, not a card).
    - FactSheet mirrors scripts/deck_factsheet.py build_factsheet() output KEYS
      EXACTLY, plus OPTIONAL fields (otag_buckets, susceptibility) defaulted
      empty so a caller that omits the otag layer still produces a valid
      contract.
    - Persistence vs. contract: the local YAML store persists only the
      non-derivable facts (ownership/intent/membership + card ref) and HYDRATES the
      base-`Card` enrichment on read via a `CardResolver`. Unresolved cards read
      back name-only with null enrichment.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Card hierarchy — base identity + the three relationships to "a card"
# --------------------------------------------------------------------------- #


class Card(BaseModel):
    """Printing-independent oracle identity + Scryfall enrichment (enrichment
    NULLABLE).

    Grain: one printing-independent oracle card. `oracle_id` is the durable
    external join key used to upsert across surfaces. Every enrichment field is
    nullable / defaulted so an UNRESOLVED card (pre-release, not yet in the
    Scryfall catalog) is representable with `name` as the only known field.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Card name (Scryfall `name`). The only always-known field.')
    oracle_id: str | None = Field(
        default=None,
        description='Scryfall oracle_id (durable, printing-independent join key); None if unresolved.',
    )
    mana_value: float | None = Field(
        default=None,
        description='Converted mana cost / mana value (Scryfall `cmc`). Lands = 0. None if unresolved.',
    )
    mana_cost: str | None = Field(
        default=None,
        description='Raw mana cost string (Scryfall `mana_cost`), e.g. `{2}{G}{G}`; None if unresolved.',
    )
    type_line: str | None = Field(
        default=None,
        description='Full type line (Scryfall `type_line`); None if unresolved.',
    )
    colors: list[str] = Field(
        default_factory=list,
        description="Colors of the card's mana cost (Scryfall `colors`), e.g. ['U','R'].",
    )
    color_identity: list[str] = Field(
        default_factory=list,
        description="Color identity (Scryfall `color_identity`), e.g. ['B','G'].",
    )
    produced_mana: list[str] = Field(
        default_factory=list,
        description='Mana this card can produce (Scryfall `produced_mana`).',
    )
    keywords: list[str] = Field(
        default_factory=list,
        description='Scryfall structured `keywords` (e.g. Flash, Flying, Prowess).',
    )
    oracle_text: str | None = Field(
        default=None,
        description='Oracle rules text (Scryfall `oracle_text`); None if unavailable.',
    )
    # --- #5 card-dim additions (presentation + functional tags), all nullable/defaulted --- #
    power: str | None = Field(
        default=None,
        description='Creature power (Scryfall `power`, a string — may be `*`); None if non-creature/unresolved.',
    )
    toughness: str | None = Field(
        default=None,
        description='Creature toughness (Scryfall `toughness`, a string — may be `*`); None if non-creature.',
    )
    art_crop: str | None = Field(
        default=None,
        description='Art-crop image URL (Scryfall `image_uris.art_crop`); None if unresolved.',
    )
    scryfall_uri: str | None = Field(
        default=None,
        description='Canonical Scryfall page URL for the card (Scryfall `scryfall_uri`); None if unresolved.',
    )
    set_name: str | None = Field(
        default=None,
        description="The card's set name (Scryfall `set_name`); distinct from OwnedCard.sets (printings owned).",
    )
    otag_buckets: list[str] = Field(
        default_factory=list,
        description='Crosswalked functional buckets from the card dim (empty if the otag layer is unavailable).',
    )
    otags: list[str] = Field(
        default_factory=list,
        description='Raw rolled-up oracle-tag slugs from the card dim (empty if the otag layer is unavailable).',
    )


class OwnedCard(Card):
    """A `Card` + your ownership facts — the "inventory item".

    Supersedes the flat `InventoryRow`: enrichment is inherited from `Card`
    (hydrated on read), and only the owned-facts below are persisted locally.
    """

    owned: int = Field(default=0, description='Number of copies owned.')
    foil: int = Field(default=0, description='Number of foil copies owned.')
    condition: list[str] = Field(default_factory=list, description='Condition grades, e.g. ["NM", "LP"].')
    sets: list[str] = Field(default_factory=list, description='Printings owned (set codes / names).')
    sources: list[str] = Field(default_factory=list, description='Acquisition provenance.')
    airtable_record_id: str | None = Field(default=None, description='Airtable record id (rec…), a durable join key.')


class ChaseCard(Card):
    """A `Card` + acquisition intent (wanted / pre-release)."""

    priority: int | None = Field(default=None, description='Acquisition priority (lower = more urgent).')
    for_decks: list[str] = Field(default_factory=list, description='Target deck names this card is wanted for.')
    status: str | None = Field(default=None, description='Acquisition status: wanted / pre-release / ordered.')
    target_price: float | None = Field(default=None, description='Target acquisition price.')
    airtable_record_id: str | None = Field(default=None, description='Airtable record id (rec…), a durable join key.')


class DeckCard(Card):
    """A `Card` as it sits in a deck — how it participates in this deck.

    Supersedes `DeckLine` (now inherits `Card`, carries `role`).
    """

    quantity: int = Field(default=1, description='Number of copies of this card in the deck.')
    role: str | None = Field(
        default=None,
        description='Deck role, e.g. "commander"; None = maindeck. (`str` now; a Literal can come later.)',
    )


class Deck(BaseModel):
    """The whole deck — has-many `DeckCard`.

    `commanders` is a DERIVED property (the `DeckCard`s whose `role == "commander"`)
    — a single source of truth, so there is no separate commanders list field.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Deck name (Airtable Decks primary field).')
    strategy: str | None = Field(
        default=None,
        description='Free-text strategy (Airtable Decks.Strategy; see strategy-schema.md).',
    )
    assessment: str | None = Field(
        default=None,
        description=(
            'Reasoning-authored reality synthesis (Airtable Decks.Assessment; the Quadrant '
            "pre-mortem — what the deck ACTUALLY is, isn't, and needs). Distinct from `strategy` "
            '(prose aim) and `focus_otags` (declared functional identity). See quadrant-theory.md.'
        ),
    )
    focus_otags: list[str] = Field(
        default_factory=list,
        description=(
            "The deck's declared NARROW focus set (Airtable Decks.Focus Otags): the buckets/otag "
            'slugs the deck CARES about — a curated subset, skill/reasoning-authored by '
            'building-decks. The deterministic pipeline READS it but never writes it.'
        ),
    )
    cards: list[DeckCard] = Field(
        default_factory=list,
        description='Every card in the deck (commanders included, marked via role).',
    )
    airtable_record_id: str | None = Field(
        default=None,
        description='Airtable Decks record id (rec…), a durable join key.',
    )

    @property
    def commanders(self) -> list[DeckCard]:
        """The deck's commander cards — derived from `DeckCard.role == "commander"`."""
        return [c for c in self.cards if c.role == 'commander']


# --------------------------------------------------------------------------- #
# FactSheet — mirrors scripts/deck_factsheet.py build_factsheet() output.
# The nested sub-models match each nested dict emitted there, key-for-key.
# --------------------------------------------------------------------------- #


class FactSheetShape(BaseModel):
    """The `shape` block: nonland/land counts, CMC histogram, avg + top-end."""

    model_config = ConfigDict(extra='forbid')

    nonland_count: int = Field(description='Number of nonland cards.')
    land_count: int = Field(description='Number of land cards (front-face land).')
    cmc_histogram: dict[str, int] = Field(description="CMC bucket -> count over nonland cards; buckets '0'..'6','7+'.")
    avg_cmc: float = Field(description='Average CMC over nonland cards (2 dp).')
    top_end_count: int = Field(description='Nonland cards with CMC >= 6.')


class FactSheetMana(BaseModel):
    """The `mana` block: ramp/fixing source counts and nonland pip distribution."""

    model_config = ConfigDict(extra='forbid')

    ramp_sources: int = Field(description='Nonland cards that ramp (produce mana / fetch land).')
    fixing_sources: int = Field(description='Ramp sources producing >1 distinct color.')
    pip_counts: dict[str, int] = Field(description='Symbol -> pip count across nonland mana costs; keys W/U/B/R/G/C.')


class FactSheetInteraction(BaseModel):
    """The `interaction` block: precision-first interaction census by type/speed."""

    model_config = ConfigDict(extra='forbid')

    board_wipes: int = Field(description='Mass removal / sweepers.')
    spot_removal: int = Field(description='Targeted destroy/exile of a permanent.')
    counterspells: int = Field(description="'Counter target …' effects.")
    protection: int = Field(description='Hexproof/indestructible/ward/shroud/protection/phase-out.')
    instant_speed: int = Field(description='Instants or Flash cards.')


class FactSheetCardAdvantage(BaseModel):
    """The `card_advantage` block: repeatable-draw engines vs one-shot draw."""

    model_config = ConfigDict(extra='forbid')

    repeatable_draw: int = Field(description='Recurring/triggered draw engines.')
    one_shot_draw: int = Field(description="One-shot 'draw N cards' (non-repeatable).")


class FactSheetStructural(BaseModel):
    """The `structural` block: ETB-creature count + graveyard-recursion presence."""

    model_config = ConfigDict(extra='forbid')

    etb_creatures: int = Field(description="Creatures with a 'when(ever) ~ enters' trigger.")
    graveyard_recursion_present: bool = Field(
        description='Any card returns something from a graveyard to the battlefield.'
    )


class FactSheetCoverage(BaseModel):
    """The `coverage` block: categorized vs uncategorized nonland %, plus the
    uncategorized card names (the synergy tell)."""

    model_config = ConfigDict(extra='forbid')

    categorized_pct: float = Field(description='% of nonland cards matched to a census category.')
    uncategorized_pct: float = Field(description='% of nonland cards left uncategorized.')
    uncategorized_cards: list[str] = Field(
        default_factory=list, description='Names of the uncategorized nonland cards.'
    )


class FactSheetCard(BaseModel):
    """One entry of the `cards` block — raw per-card facts (`_card_record`)."""

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Card name.')
    cmc: float | None = Field(default=None, description='Card CMC (may be null).')
    type_line: str = Field(description='Full type line.')
    keywords: list[str] = Field(default_factory=list, description='Scryfall keywords.')
    produced_mana: list[str] | None = Field(default=None, description='Scryfall produced_mana (may be null).')
    is_land: bool = Field(description='Whether the front face is a land.')
    oracle_text: str = Field(default='', description='Oracle rules text.')


class FactSheetFocusRelative(BaseModel):
    """The `focus_relative` block: the deck's ACTUAL card tags measured against
    its NARROW, skill-authored focus set (``Focus Otags``).

    The deterministic engine only READS the focus (it never authors it) and
    reports three signals. Defaulted empty so a deck with no declared focus emits
    the same shape with all three empty.
    """

    model_config = ConfigDict(extra='forbid')

    coverage_of_focus: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Focus entry -> count of the deck's cards that support it (carry that "
            'bucket or otag slug). Surfaces focus items that ARE well-supported. '
            'Every declared focus entry is echoed, including zero-support ones.'
        ),
    )
    thin_focus: list[str] = Field(
        default_factory=list,
        description=(
            'Focus entries with weak/no support (below a small card threshold) — '
            "the 'you care about X but have little of it' susceptibility tell."
        ),
    )
    off_focus: list[str] = Field(
        default_factory=list,
        description=(
            'Prominent card buckets/tags NOT in the focus set — incidental/noise '
            "('your cards do a lot of Y you didn't declare')."
        ),
    )


class FactSheet(BaseModel):
    """The neutral deck fact sheet — MIRRORS build_factsheet() output KEYS EXACTLY.

    Core keys (deck/shape/mana/keywords/interaction/card_advantage/
    structural/coverage/cards/missing) MUST NOT be renamed or removed. Two
    OPTIONAL fields (otag_buckets, susceptibility), defaulted empty, carry the
    otag-derived facts; a caller that omits the otag layer still produces a
    valid contract.

    Focus-relative fields (``focus`` + ``focus_relative``) are additional OPTIONAL
    fields, also defaulted empty: they echo the deck's declared focus set and the
    three signals measuring actual card tags against it. Empty when the deck
    declares no focus, so no-focus output stays byte-identical.
    """

    model_config = ConfigDict(extra='forbid')

    deck: str | None = Field(default=None, description='Deck name, if known.')
    shape: FactSheetShape = Field(description='Curve / count shape facts.')
    mana: FactSheetMana = Field(description='Ramp / fixing / pip facts.')
    keywords: dict[str, int] = Field(default_factory=dict, description='Scryfall keyword census (nonzero only).')
    interaction: FactSheetInteraction = Field(description='Interaction census.')
    card_advantage: FactSheetCardAdvantage = Field(description='Card-advantage census.')
    structural: FactSheetStructural = Field(description='Structural flags.')
    coverage: FactSheetCoverage = Field(description='Categorized vs uncategorized coverage.')
    cards: list[FactSheetCard] = Field(default_factory=list, description='Raw per-card facts.')
    missing: list[str] = Field(
        default_factory=list,
        description='Decklist names that did not resolve to a card.',
    )

    # --- otag-derived, defaulted empty so the no-otag shape still validates -----
    otag_buckets: dict[str, int] = Field(
        default_factory=dict,
        description='Oracle-tag bucket -> card count (empty if the otag layer is unavailable).',
    )
    susceptibility: list[str] = Field(
        default_factory=list,
        description='Susceptibility signals / resilience gaps (empty if the otag layer is unavailable).',
    )

    # --- focus-relative (additive, OPTIONAL), defaulted empty ----------------- #
    focus: list[str] = Field(
        default_factory=list,
        description=(
            "The deck's declared NARROW focus set (skill/reasoning-authored "
            '`Focus Otags`), echoed. Empty when no focus was supplied.'
        ),
    )
    focus_relative: FactSheetFocusRelative = Field(
        default_factory=FactSheetFocusRelative,
        description='Focus-relative signals: actual card tags measured vs the focus set.',
    )


# --------------------------------------------------------------------------- #
# Trade — a movement event; stands alone (not a card). See airtable-schema.md.
# --------------------------------------------------------------------------- #


class Trade(BaseModel):
    """A card movement event.

    Source/Destination are categories (Library/Deck/Store/Person); the *_deck
    fields add specificity when the category is "Deck". (Formerly `TradeRow`;
    the Airtable-ish `Row` suffix is dropped, fields unchanged.)
    """

    model_config = ConfigDict(extra='forbid')

    date: str | None = Field(default=None, description='Trade Date (ISO date).')
    from_source: str = Field(description='From (Source) category: Library / Deck / Store / Person.')
    to_destination: str = Field(description='To (Destination) category: Library / Deck / Store / Person.')
    from_deck: str | None = Field(default=None, description="From (Deck) — specificity when Source = 'Deck'.")
    to_deck: str | None = Field(default=None, description="To (Deck) — specificity when Destination = 'Deck'.")
    cards_in: list[str] = Field(default_factory=list, description='Cards into Destination (card names).')
    cards_out: list[str] = Field(default_factory=list, description='Cards out of Destination (card names).')
    status: str | None = Field(default=None, description='Status: Draft / Planned / Completed.')
    completed_date: str | None = Field(default=None, description='Completed Date (ISO date).')
    notes: str | None = Field(default=None, description='Reason / Notes.')
    airtable_record_id: str | None = Field(default=None, description='Airtable record id (rec…), a durable join key.')


# --------------------------------------------------------------------------- #
# Spoiler — a reconciled preview row (MythicSpoiler <-> Scryfall). Mirrors the
# prior spoiler_cache.db row shape so chasing-cards' status/list output is
# unchanged; oracle_id/confirmed carry the MythicSpoiler->Scryfall reconciliation.
# --------------------------------------------------------------------------- #


class Spoiler(BaseModel):
    """A reconciled spoiler/preview card.

    Carries a preview seen on MythicSpoiler or Scryfall through to a confirmed
    Scryfall identity. `oracle_id` is null until Scryfall-confirmed (`confirmed`);
    `first_seen_cursor` is the lake cursor at which the row first appeared,
    replacing the old SQLite `meta` watermark.
    """

    model_config = ConfigDict(extra='forbid')

    slug: str = Field(description='Stable preview slug (MythicSpoiler slug); the durable dedup key.')
    set_code: str = Field(description='Set code the preview belongs to, e.g. `EOE`.')
    name: str = Field(description='Card name as previewed.')
    oracle_id: str | None = Field(
        default=None,
        description='Scryfall oracle_id once reconciled; None until Scryfall-confirmed.',
    )
    source: str = Field(description='Where the preview was seen: `mythicspoiler` or `scryfall`.')
    first_seen_cursor: str | None = Field(
        default=None,
        description='Lake cursor at which this preview first appeared (replaces the SQLite meta watermark).',
    )
    confirmed: bool = Field(
        default=False,
        description='Whether the preview has been reconciled to a Scryfall identity.',
    )
