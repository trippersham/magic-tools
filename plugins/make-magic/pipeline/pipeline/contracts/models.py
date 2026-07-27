"""Pydantic v2 boundary models — the edge contracts.

Per the data-architecture decision, Pydantic lives ONLY at the edges: these are
the objects a skill / MCP tool / future UI receives (object count ~1). The
pipeline middle stays columnar (DuckDB/Parquet), never materialized as Pydantic.

`model_json_schema()` over these models is the single source of truth for MCP
`inputSchema`/`outputSchema` and future auto-forms — so export_schemas.py commits
the generated JSON Schema and a test guards against drift.

Design notes:
    - `extra="forbid"` on every model: a boundary contract should reject unknown
      keys loudly rather than silently accept typos / schema drift.
    - Fields align with Scryfall's oracle-card shape (Card) and the Airtable
      field vocabulary (InventoryRow / TradeRow / Deck) — see
      skills/building-decks/references/airtable-schema.md.
    - FactSheet mirrors scripts/deck_factsheet.py build_factsheet() output KEYS
      EXACTLY, plus forward-looking OPTIONAL fields (otag_buckets,
      susceptibility) defaulted empty so Phase 4 can populate them without
      breaking the contract.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Card / Deck boundary models
# --------------------------------------------------------------------------- #


class Card(BaseModel):
    """A resolved card — a NARROW boundary object modeled on Scryfall's
    oracle-card shape (NOT the full Scryfall blob).

    Grain: one printing-independent oracle card. `oracle_id` is the durable
    external join key used to upsert across surfaces.
    """

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Card name (Scryfall `name`).')
    oracle_id: str = Field(description='Scryfall oracle_id (durable, printing-independent join key).')
    mana_value: float = Field(description='Converted mana cost / mana value (Scryfall `cmc`). Lands = 0.')
    type_line: str = Field(description='Full type line (Scryfall `type_line`).')
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


class DeckLine(BaseModel):
    """One decklist entry: a card name, a quantity, and an optional resolved
    oracle_id (populated once the name is looked up against the card table)."""

    model_config = ConfigDict(extra='forbid')

    card_name: str = Field(description='Card name as written in the decklist.')
    quantity: int = Field(description='Number of copies of this card in the deck.')
    oracle_id: str | None = Field(
        default=None,
        description='Resolved Scryfall oracle_id, if the name has been matched.',
    )


class Deck(BaseModel):
    """A deck boundary object: name, commander(s), optional strategy text, the
    decklist lines, and an optional Airtable record id used as a join key."""

    model_config = ConfigDict(extra='forbid')

    name: str = Field(description='Deck name (Airtable Decks primary field).')
    commanders: list[str] = Field(
        default_factory=list,
        description='Commander card name(s); empty for non-Commander formats.',
    )
    strategy: str | None = Field(
        default=None,
        description='Free-text strategy (Airtable Decks.Strategy; see strategy-schema.md).',
    )
    lines: list[DeckLine] = Field(
        default_factory=list,
        description='Decklist entries (non-commander cards, with quantities).',
    )
    airtable_record_id: str | None = Field(
        default=None,
        description='Airtable Decks record id (rec…), a durable join key.',
    )


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

    Existing keys (deck/shape/mana/keywords/interaction/card_advantage/
    structural/coverage/cards/missing) are preserved and MUST NOT be renamed or
    removed. Two forward-looking OPTIONAL fields, defaulted empty, let Phase 4
    populate otag-derived facts without breaking this contract.

    Focus-relative fields (``focus`` + ``focus_relative``) are additional OPTIONAL
    fields, also defaulted empty: they echo the deck's declared focus set and the
    three signals measuring actual card tags against it. Empty when the deck
    declares no focus, so existing (no-focus) output stays byte-identical.
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

    # --- forward-looking (Phase 4), defaulted empty so the current shape holds --
    otag_buckets: dict[str, int] = Field(
        default_factory=dict,
        description='Oracle-tag bucket -> card count (populated in Phase 4).',
    )
    susceptibility: list[str] = Field(
        default_factory=list,
        description='Susceptibility signals / resilience gaps (populated in Phase 4).',
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
# Airtable row boundary models — see airtable-schema.md.
# --------------------------------------------------------------------------- #


class InventoryRow(BaseModel):
    """An Airtable "Cards" table row (`tbl3UgZZPJGQhEFo8`): normalized 1-row-per
    -title inventory. Fields mirror the human-relevant columns; formula/rollup
    fields (Number in Decks/Library, Is Land/Creature) are DERIVED and excluded.
    """

    model_config = ConfigDict(extra='forbid')

    card_name: str = Field(description='Card Name (primary singleLineText).')
    number_owned: int = Field(default=0, description='Number Owned.')
    foil_count: int = Field(default=0, description='Foil Count.')
    condition: list[str] = Field(default_factory=list, description='Condition (multipleSelects).')
    sets: list[str] = Field(default_factory=list, description='Sets (multipleSelects).')
    sources: list[str] = Field(default_factory=list, description='Sources (multipleSelects).')
    card_type: str | None = Field(default=None, description='Card Type (Scryfall type_line).')
    mana_cost: str | None = Field(default=None, description="Mana Cost, e.g. '{2}{W}{U}'.")
    cmc: float | None = Field(default=None, description='CMC (Scryfall cmc). Lands = 0.')
    power_toughness: str | None = Field(default=None, description="Power / Toughness, e.g. '2/4'.")
    oracle_text: str | None = Field(default=None, description='Oracle Text (multilineText).')
    color_identity: list[str] = Field(
        default_factory=list,
        description='Color Identity (multipleSelects): W/U/B/R/G/Colorless.',
    )
    price_tcgplayer: float | None = Field(default=None, description='Price (TCGPlayer) currency; Scryfall prices.usd.')
    scryfall_url: str | None = Field(default=None, description='Scryfall URL.')
    airtable_record_id: str | None = Field(default=None, description='Airtable record id (rec…), a durable join key.')


class TradeRow(BaseModel):
    """An Airtable "Trades" table row (`tblgqqIvTuz0l5SZM`): card movement.

    Source/Destination are categories (Library/Deck/Store/Person); the *_deck
    fields add specificity when the category is "Deck".
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
