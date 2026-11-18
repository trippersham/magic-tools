"""The declarative identity vocabulary — the single source of truth for strategy distillation.

Two maps, and only these two, decide a deck's strategy identity:

``ATTRIBUTES``
    identity-name -> query string. The ONE place an otag/field's membership in a
    CardAttribute lives. Every query is a literal, Scryfall-pasteable predicate of
    exact ``otag:<slug>`` / field atoms composed with ``AND``/``OR``/``NOT``: an
    identity names the family by its exact otag parent (card otags are
    ancestor-closed, so ``otag:typal`` catches ``typal-lizard`` and the rest) or by
    an explicit exact OR-list where the tag DAG does not parent the family under one
    root. An identity's query is matched against a *pseudo-card* whose only otag is
    the attribute name under test (see :mod:`.query`), so ``otag:typal`` is exactly
    ``name == 'typal'`` and ``otag:changeling`` is ``name == 'changeling'``.

``ARCHETYPE_EDGES``
    identity-name -> strategy-name. The ONE place the identity edge lives. It is
    a pure name->name map: no otag slug ever appears here. Several identities may
    share a strategy (``poison`` and ``mm-counters`` both reach ``-1/-1 counters``),
    but every identity has exactly one edge and every edge key is a defined
    identity.

Nothing lives in both maps' domains for two purposes: membership is ``ATTRIBUTES``,
the edge is ``ARCHETYPE_EDGES``. Distilling an attribute name is: find the identity
whose query matches that name, then take its edge. The identity order below is the
distillation precedence — the first matching identity wins, which is how overlapping
mechanic families (a land that is also graveyard recursion; a treasure that is
also an artifact payoff) resolve to a single strategy. All identity blocks are
unconditionally active; the ``overlay`` parameter of :func:`build_strategy_distiller`
deep-merges user extensions over this pristine base.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pipeline.transforms.strategy.query import CardView, Predicate, compile_query

#: EDHREC new-node identities (always active; the ``edhrec_labels`` knob was removed). Ordered: this
#: block is the highest distillation precedence, matching the engine's tier-1 distiller.
EDHREC_IDENTITIES: list[tuple[str, str]] = [
    ('devotion', 'otag:devotion-signal'),
    (
        'vehicles',
        'otag:synergy-vehicle OR otag:crew OR otag:bring-your-own-crew '
        'OR otag:alternative-crewing OR otag:animate-vehicle OR otag:crewless-vehicle '
        'OR otag:vehicle-type',
    ),
    (
        'energy',
        'otag:energy-generator OR otag:counter-fuel-energy OR otag:energy-increaser '
        'OR otag:synergy-energy OR otag:energy-text',
    ),
    ('extra-turns', 'otag:extra-turn OR otag:extra-upkeep OR otag:extra-turn-text'),
    ('mill', 'otag:mill-opponent OR otag:mill-any OR otag:synergy-mill'),
    (
        'planeswalkers',
        'otag:synergy-planeswalker OR otag:recursion-planeswalker OR otag:protects-planeswalker OR otag:superfriends',
    ),
    (
        'discard-wheels',
        'otag:discard-matters OR otag:self-discard-matters OR otag:wheel '
        'OR otag:wheel-one-sided OR otag:wheel-symmetrical '
        'OR otag:wheel-symmetrical-optional OR otag:miniwheel',
    ),
    (
        'treasure-food',
        'otag:repeatable-treasures OR otag:synergy-treasure OR otag:repeatable-clues '
        'OR otag:synergy-clue OR otag:repeatable-blood OR otag:synergy-blood '
        'OR otag:repeatable-food OR otag:synergy-food OR otag:eat-food OR otag:food',
    ),
    (
        'theft-edhrec',
        'otag:theft OR otag:theft-creature OR otag:theft-artifact OR otag:theft-land '
        'OR otag:theft-planeswalker OR otag:theft-spell OR otag:theft-cast '
        'OR otag:nightveil-theft',
    ),
    (
        'poison',
        'otag:synergy-poison OR otag:poison-opponents OR otag:poison-mechanics '
        'OR otag:poisonous OR otag:synergy-toxic OR otag:synergy-infect',
    ),
    (
        'enchantress',
        'otag:synergy-enchantment OR otag:synergy-aura OR otag:synergy-shrine '
        'OR otag:synergy-enchantment-creature OR otag:enchantmentfall '
        'OR otag:enchantment-engine OR otag:constellation '
        'OR otag:repeatable-enchantment-tokens OR otag:repeatable-aura-tokens '
        'OR otag:affinity-for-enchantments OR otag:creature-type-enchantress '
        'OR otag:quick-enchant OR otag:auto-attach OR otag:cast-enchantment-trigger '
        'OR otag:enchantment-cost-reducer',
    ),
]

#: New-bucket identities (always active; the ``new_buckets`` group toggle was
#: collapsed to always-on as dead config — ADR 0006).
NEWBUCKET_IDENTITIES: list[tuple[str, str]] = [
    ('food', 'otag:repeatable-food OR otag:synergy-food OR otag:eat-food OR otag:food'),
    (
        'mm-counters',
        'otag:gives-mm-counters OR otag:gains-mm-counters OR otag:mm-counters-matter '
        'OR otag:removes-mm-counters-self OR otag:mm_counters',
    ),
    (
        'theft-newbuckets',
        'otag:theft-creature OR otag:theft-artifact OR otag:theft-land '
        'OR otag:theft-planeswalker OR otag:theft-spell OR otag:theft',
    ),
]

#: Always-active identities, in distillation precedence. Every family is named by its
#: exact otag parent (ancestor-closure carries the children) or, where the tag DAG
#: does not parent the family under one root, by an explicit exact OR-list that
#: enumerates the orphan/sibling slugs (``attacking-matters`` + ``-self``/``-any``;
#: ``lifegain`` + ``lifegain-matters``/``-increaser``/``-to-damage``; ``tax`` +
#: ``tax-attack``/``-block``; ``synergy-artifact`` + ``-creature``). The hand-rolled
#: unions (``counters`` pp-counters; ``deathtouch``) are likewise exact OR-lists.
#: A granted-keyword payload slug is included where GRANTING the keyword *is* the
#: strategy: ``gives-pp-counters`` IS a ``counters`` card (handing out +1/+1 counters is
#: a +1/+1-counters plan), symmetric with ``gives-mm-counters`` in ``mm-counters`` and
#: ``gains-deathtouch``/``gives-deathtouch``/``gives-deathtouch-noncreature`` in the
#: ``deathtouch`` identity (a deck that hands out deathtouch — Shelob, the
#: deathtouch-combat payoff producers — is a deathtouch strategy; R5a restored the
#: 314-card deathtouch-granting family after the dead event-name ref ``deathtouch-combat``
#: silently under-covered it). An unlisted ``gives-*`` slug not named here falls through
#: to "no identity". The
#: facts-only ``enchantments`` archetype lists the enchantment-*synergy* members
#: (the removal/hate/disenchant slugs the old broad regex also swept up are not an
#: enchantment strategy); the real synergy payoffs distill to ``enchantress`` first.
#: The later additions are likewise exact OR-lists: ``pillowfort`` (fog /
#: prevent-attack), ``group-hug`` (the two group-hug payoffs), and ``politics``
#: (monarch / vote / the ``synergy-goad`` payoff — ``hate-goad`` is the answer, not
#: the strategy). An identity only fires on slugs the facts layer SURFACES; the
#: non-payoff anchors (``pillowfort``/``fog``/``group-hug``/…) are surfaced by their
#: entries in the committed ``payoff_attributes`` membership set, exactly as the
#: ``*-matters`` / ``synergy-*`` payoffs already are. An unsurfaced slug in a query
#: (``buttstrike``, ``high-flying``) is inert belt-and-suspenders, never a false
#: positive.
BASE_IDENTITIES: list[tuple[str, str]] = [
    ('typal/tribal', 'otag:typal OR otag:changeling'),
    (
        'tokens/go-wide',
        'otag:tokens OR otag:token-increaser OR otag:repeatable-token-generator '
        'OR otag:repeatable-creature-tokens OR otag:repeatable-noncreature-tokens '
        'OR otag:synergy-token',
    ),
    # clones / steal-via-copy (Riku/Lazav/Mimeoplasm/Damia cmdrs): copying a
    # NONTOKEN creature (clone staples, graveyard-steal-via-copy). Keys on the
    # `clones` bucket (copy-creature + clone) ONLY — deliberately NOT copy-token:
    # token-copy/populate (Rhys/Trostani/Second Harvest) is a tokens plan, not a
    # clones identity. `copy-creature` is OVERLOADED (populate carries it too), so
    # over-fire is left to the GENERIC density-rank + ratio cutoff — a lone clone
    # staple in a tokens/populate deck never clears it; no bespoke commander guard.
    ('clones', 'otag:clones'),
    (
        'counters',
        'otag:counters OR otag:gains-pp-counters OR otag:gives-pp-counters '
        'OR otag:pp-counters-matter '
        'OR otag:repeatable-pp-counters OR otag:counters-matter '
        'OR otag:counter-increaser '
        'OR otag:repeatable-proliferate OR otag:pseudo-proliferate OR otag:synergy-proliferate',
    ),
    (
        'sacrifice',
        'otag:sac OR otag:sacrifice-outlet OR otag:your-sacrifice-matters '
        'OR otag:sacrifice-matters OR otag:death-trigger OR otag:death-trigger-self '
        'OR otag:free-sacrifice-outlet OR otag:sacrifice-self',
    ),
    # landfall vs lands-matter: EDHREC treats these as DISTINCT themes, so they are
    # two identities, not one. `landfall` is the ETB-land trigger family (the exact
    # otag parent `landfall` + its sibling `landfall-other`; the only two real
    # landfall slugs in the lake — `islandfall`/`cycle-*-landfall-*` are puns/cycle
    # tags, not the family). `lands-matter` keeps the broader lands-as-resource
    # mechanics: `lands-matter`, `land-count-matters`, `extra-land`, and the two land
    # recursion slugs (`recursion-land`/`reanimate-land` stay here as lands-matter
    # mechanics; a later engine-layer pass handles the land-recursion hub). The two
    # queries are disjoint, so distillation precedence between them is immaterial.
    ('landfall', 'otag:landfall OR otag:landfall-other'),
    (
        'lands-matter',
        'otag:lands-matter OR otag:land-count-matters OR otag:extra-land OR otag:recursion-land OR otag:reanimate-land',
    ),
    (
        'graveyard/reanimator',
        'otag:graveyard OR otag:recursion OR otag:reanimate '
        'OR otag:cards-in-graveyard-matter OR otag:castable-from-graveyard '
        'OR otag:graveyard-fuel OR otag:graveyard-fuel-self '
        'OR otag:activate-from-graveyard OR otag:mill-self',
    ),
    (
        'spellslinger',
        'otag:spellslinger OR otag:cast-trigger-you OR otag:cast-trigger-other '
        'OR otag:second-spell-matters OR otag:storm-count-matters OR otag:synergy-instant '
        'OR otag:synergy-sorcery OR otag:synergy-noncreature OR otag:prowess-like '
        'OR otag:copy-instant OR otag:copy-sorcery',
    ),
    (
        'artifacts',
        'otag:artifacts OR otag:synergy-artifact OR otag:synergy-artifact-creature '
        'OR otag:artifact-count-matters OR otag:affinity-for-artifacts '
        'OR otag:repeatable-treasures OR otag:artifactfall',
    ),
    ('blink/flicker', 'otag:flicker'),
    (
        'voltron/equipment',
        'otag:equipment OR otag:synergy-equipment '
        'OR otag:synergy-equipment-legendary OR otag:synergy-aura '
        'OR otag:quick-equip OR otag:auto-equip',
    ),
    (
        'lifegain',
        'otag:lifegain OR otag:lifegain-matters OR otag:lifegain-increaser '
        'OR otag:lifegain-to-damage OR otag:repeatable-lifegain',
    ),
    ('stax/prison', 'otag:stax OR otag:tax OR otag:tax-attack OR otag:tax-block OR otag:cast-tax'),
    (
        'deathtouch',
        'otag:gains-deathtouch OR otag:gives-deathtouch OR otag:gives-deathtouch-noncreature '
        'OR otag:deathtouch-counter '
        'OR otag:deathtouch-to-planeswalkers '
        'OR otag:hate-deathtouch OR otag:old-blocking-deathtouch '
        'OR otag:old-damage-deathtouch OR otag:removes-deathtouch '
        'OR otag:synergy-deathtouch OR otag:tutor-creature-deathtouch',
    ),
    ('control/permission', 'otag:counterspells OR otag:counterspell'),
    ('anthem/go-wide', 'otag:anthem OR otag:power-boost-to-all OR otag:toughness-boost-to-all'),
    ('extra-combat', 'otag:extra_combat OR otag:extra-combat-phase'),
    ('burn', 'otag:burn OR otag:burn-player OR otag:opponent-loses-life OR otag:drain-life'),
    (
        'combat/aggro',
        'otag:combat OR otag:attacking-matters OR otag:attacking-matters-self '
        'OR otag:attacking-matters-any OR otag:attack-trigger',
    ),
    (
        'toughness-matters',
        'otag:toughness-matters OR otag:toughness-matters-self OR otag:buttstrike '
        'OR otag:turns-off-defender OR otag:turns-off-defender-self '
        'OR otag:synergy-defender',
    ),
    (
        'morph',
        'otag:morph OR otag:synergy-face-down OR otag:face-up-face-down-effects '
        'OR otag:turn-face-down OR otag:synergy-manifest OR otag:synergy-manifest-dread '
        'OR otag:synergy-disguise',
    ),
    # pillowfort / fog (Zedruu/Derevi/Nelly Borca): prevent-combat-damage +
    # can't-attack-you defense. An explicit exact OR-list of the fog/prevent-attack
    # payoffs — NOT the broad `combat` family (first-match order already puts this
    # after combat/aggro, but these slugs are disjoint from it).
    ('pillowfort', 'otag:pillowfort OR otag:fog OR otag:pseudo-fog OR otag:fog-selective OR otag:prevent-attack'),
    # group-hug (Ms. Bumbleflower/Kynaios): symmetric card/mana/token gifting. The
    # two group-hug payoff slugs (the broad `group-hug` parent + the one-sided
    # `selective-group-hug`); no clean single otag parents both.
    ('group-hug', 'otag:group-hug OR otag:selective-group-hug'),
    # politics (Zedruu/monarch/vote/goad decks): monarch-matters, council's-vote,
    # and goad payoffs. Thin (~60 cards) but a distinct identity the political
    # precons need (`political-puppets`). The goad payoff slug is `synergy-goad`
    # (`hate-goad` is the ANSWER, deliberately excluded).
    ('politics', 'otag:monarch-matters OR otag:synergy-vote OR otag:synergy-goad'),
    # Wincon archetypes (ADR 0023 conflation decomposed; ADR 0024 the archetype
    # NAMES the plan while the enabler->win link lives in the event graph). The old
    # `alt-wincon/combo` catch-all (`otag:wincon OR otag:alternate-win-condition`)
    # collapsed all 87 `alternate-win-condition` cards into one identity and dropped
    # every enabler; its second disjunct `otag:alternate-win-condition` was a DEAD REF
    # (no CardAttribute of that name is ever surfaced — the lake slug is surfaced as
    # the attribute NAME `wincon`). It is decomposed into honest per-archetype names:
    #   * `deckout/thoracle` — the self-deplete (empty-your-own-library) win, keyed on
    #     the `deckout-win` synth payoff (`otag:empty-library`, 5 cards). The spec's
    #     coarser `otag:mill-self OR oracle:'no cards in'` disjuncts were REJECTED on
    #     measurement: `mill-self` is a broad graveyard-fuel enabler (drags in Central
    #     Elevator, not a deckout win) and `oracle:'no cards in'` matches "no cards in
    #     HAND" (Barren Glory, The Cheese Stands Alone — empty-board wins, not deckout).
    #   * `domain` — the five-color state-composition win (Coalition Victory + Call the
    #     Spirit Dragons), keyed on the `five-color-win` synth payoff authored in R1.
    # Every OTHER wincon RIDES its existing family: a lifegain-win (Felidar/Test of
    # Endurance) rides `lifegain`, a poison-win rides `poison`, a cast-count win rides
    # `spellslinger` — because those cards already carry their family's otag attribute,
    # and the win itself is an event-edge (`altwin::lifegain`/`altwin::poisoned`/…),
    # not an archetype. The genuine standalone-state singletons (Phage, Barren Glory,
    # coin-flip, Door to Nothingness, …) distill to None: a card's one-off build-around
    # win does not name the deck, and a `misc-wincon` bucket broad enough to catch them
    # would re-bundle unrelated mechanics (the very conflation ADR 0023 forbids). The
    # threshold wincons that key to a tracked event (Darksteel->counters,
    # Hellkite->artifacts, Maze's End->land) are Home-2 event riders, NOT forced here.
    ('deckout/thoracle', 'otag:deckout-win'),
    ('domain', 'otag:five-color-win'),
    (
        'enchantments',
        'otag:synergy-enchantment OR otag:synergy-enchantment-creature '
        'OR otag:synergy-aura OR otag:synergy-shrine OR otag:synergy-saga '
        'OR otag:constellation OR otag:enchantmentfall OR otag:enchantment-engine',
    ),
]

#: identity-name -> strategy-name. Pure name->name; no slug ever appears here.
ARCHETYPE_EDGES: dict[str, str] = {
    # edhrec tier-1
    'devotion': 'devotion',
    'vehicles': 'vehicles',
    'energy': 'energy',
    'extra-turns': 'extra-turns',
    'mill': 'mill',
    'planeswalkers': 'planeswalkers',
    'discard-wheels': 'discard/wheels',
    'treasure-food': 'treasure/food',
    'theft-edhrec': 'theft',
    'poison': '-1/-1 counters',
    'enchantress': 'enchantress',
    # new-bucket
    'food': 'food',
    'mm-counters': '-1/-1 counters',
    'theft-newbuckets': 'theft',
    # base
    'typal/tribal': 'typal/tribal',
    'tokens/go-wide': 'tokens/go-wide',
    'clones': 'clones/steal-via-copy',
    'counters': '+1/+1 counters',
    'sacrifice': 'aristocrats/sacrifice',
    'landfall': 'landfall',
    'lands-matter': 'lands-matter',
    'graveyard/reanimator': 'graveyard/reanimator',
    'spellslinger': 'spellslinger',
    'artifacts': 'artifacts',
    'blink/flicker': 'blink/flicker',
    'voltron/equipment': 'voltron/equipment',
    'lifegain': 'lifegain',
    'stax/prison': 'stax/prison',
    'deathtouch': 'deathtouch',
    'control/permission': 'control/permission',
    'anthem/go-wide': 'anthem/go-wide',
    'extra-combat': 'extra-combat',
    'burn': 'burn',
    'combat/aggro': 'combat/aggro',
    'toughness-matters': 'toughness-matters',
    'morph': 'morph',
    'pillowfort': 'pillowfort',
    'group-hug': 'group-hug',
    'politics': 'politics',
    # wincon archetypes (the `alt-wincon/combo` catch-all decomposed, see BASE_IDENTITIES).
    # `alt-wincon/combo` survives ONLY as the combo-override HEADLINE literal
    # (`naming.ALT_WINCON_COMBO`) — a separate axis from `archetype_identity`, not a
    # distill edge.
    'deckout/thoracle': 'deckout/thoracle',
    'domain': 'domain',
    'enchantments': 'enchantments',
}

#: Synthesized attributes: a card gets these by card-field query, not by otag.
#: Ordered; the EDHREC block is always active (the ``edhrec_labels`` knob was removed). These
#: names also distill — as the pseudo-otag of their identity above (``vehicle-type``
#: is claimed by ``vehicles``; ``devotion-signal`` by ``devotion``; and so on).
SYNTH_EDHREC_ATTRIBUTES: list[tuple[str, str]] = [
    ('vehicle-type', 'type:Vehicle'),
    ('energy-text', "oracle:'energy counter' OR oracle:'{e}'"),
    ('extra-turn-text', "oracle:'extra turn'"),
    # Devotion is detected from the oracle text alone: the engine surfaces the
    # signal wherever a card speaks of devotion. (A pip-count form of the check
    # is available as the `devotion` query operator, but the attribute's
    # membership is the oracle-text test.)
    ('devotion-signal', 'oracle:devotion'),
]
#: Morph/face-down body: the keyword marks a face-down creature that Scryfall's
#: otags do not tag cleanly, so the attribute is synthesized from the keyword.
SYNTH_ALWAYS_ATTRIBUTES: list[tuple[str, str]] = [
    ('morph', 'keyword:morph OR keyword:megamorph OR keyword:disguise OR keyword:cloak'),
    # Rad counters (Fallout) are a novel PLAYER-targeted counter with NO Scryfall
    # otag, so membership is synthesized from the oracle text. This surfaces the
    # rad-giving (and rad-caring) cards so the `player-counter` event can model the
    # rad engine; it carries no archetype edge (it distills to None) — the rad engine
    # is isolated at the ENGINE layer, it does not rename the deck.
    ('rad-counter', "oracle:'rad counter'"),
    # Monarch-makers (#74 §2.2 option ii). Scryfall carries ONE `monarch-matters`
    # otag (49) over BOTH makers and payoffs, so the `monarch` event's single attr
    # on both sides formed no edge (the p != c_ self-pair exclusion). This synth
    # attr isolates the MAKERS by the oracle phrase every maker carries ("become
    # the monarch"), so `monarch`'s produced_by seeds it and triggers keeps the
    # payoff otag `monarch-matters` — maker->payoff edges now form. Measured 62
    # members (all literal "become the monarch" makers; the 2 that also carry a
    # "whenever you become the monarch" trigger still make the monarch on ETB, so
    # no pure-payoff contamination). Like `rad-counter` it carries NO archetype
    # edge (distills to None) — it surfaces the maker cards for the edge pass, it
    # does not rename the deck.
    ('monarch-maker', "oracle:'become the monarch'"),
    # Color-fixing producer (ADR 0024 domain state-event; #74). No Scryfall otag is
    # both mechanic-exclusive AND producer-role-pure for "fixes your colors", so it
    # is authored here as a UNION inside the closed query vocabulary (ADR 0009: the
    # evaluator must NOT grow a `produced_mana:` atom — the field form is off-limits).
    # `oracle:'mana of any color'` (686) catches the any-color mana sources (dorks /
    # rocks / lands); `otag:rainbow-land` (118) adds the any-color LANDS. Union = 709,
    # 92% produce >=3 of WUBRG (producer-role), 2/709 incidental payoff tags (Revel in
    # Riches, Luck Bobblehead). KNOWN GAP: basic-land-TYPE fixers (fetchlands, Prismatic
    # Omen) are NOT covered — no clean otag exists (`fetchland` 56 is 1-2 types not all;
    # `dual-land` 468 / basic-land-search 380 drag in generic ramp), so the land-fetch
    # half of domain fixing is deferred (see subagents/r1-realize-adr0024/outbox.md).
    # Like `rad-counter`/`monarch-maker` it carries NO archetype edge (distills to None)
    # — it surfaces the fixer cards for the `domain` event producer seed, it does not
    # rename the deck.
    ('color-fixing', "oracle:'mana of any color' OR otag:rainbow-land"),
    # Five-color-win payoff (ADR 0024 domain state-event). The domain/five-color WIN
    # payoff: `otag:alternate-win-condition AND oracle:'each color'` = exactly
    # {Coalition Victory, Call the Spirit Dragons} — the two cards that win by composing
    # all five colors (Coalition also needs a land of each basic type). Mechanic-
    # exclusive and payoff-pure (the broader `otag:alternate-win-condition AND
    # otag:lifegain` was rejected as coarse). TWO consumers read this synth: (a) the
    # `domain` EVENT's triggers (the enabler->win link color-fixing -> domain ->
    # five-color-win, ADR 0024); (b) the `domain` DISTILL identity (R1b naming layer) —
    # it carries an archetype edge `-> domain`, so a deck running the payoff is NAMED
    # `domain` (ADR 0024: "the distill archetype names the plan: domain"). Unlike the
    # `color-fixing` PRODUCER (which stays edge-less — a fixer must not rename a ramp
    # deck), the WIN payoff legitimately names the deck. Inert on the gauntlet corpus
    # (2 cards, neither a legal commander, never reaches node-admission support>=3),
    # exactly as the `domain` event is engine-inert.
    ('five-color-win', "otag:alternate-win-condition AND oracle:'each color'"),
    # Deckout / Thassa's-Oracle payoff (ADR 0023 decomposition of alt-wincon/combo; R1b).
    # The self-deplete WIN: win by emptying YOUR OWN library. Keyed on the mechanic-
    # exclusive `otag:empty-library` intersected with the wincon bucket = exactly
    # {Laboratory Maniac, Jace Wielder of Mysteries, Fblthp Impossibly Lost, Doctor Doom
    # Unrivaled, Platinum Persecutor} (5 cards, 0 false positives). Carries an archetype
    # edge `-> deckout/thoracle` (the DISTILL identity above), so a self-mill deck is
    # NAMED deckout, not the catch-all. NARROWER than the design's floated
    # `otag:mill-self OR oracle:'no cards in'`: `mill-self` is a broad graveyard-fuel
    # enabler (Central Elevator is not a deckout win) and `oracle:'no cards in'` matches
    # "no cards in HAND" (Barren Glory / The Cheese Stands Alone — empty-board wins).
    # KNOWN MISS: Thassa's Oracle carries no empty-library/mill-self otag and no
    # distinguishing oracle phrase (its text keys on devotion), so it is not surfaced —
    # the design flags it as the honest core of the problem (a threshold wincon whose
    # enabler is not otag-expressible); its infinite case is still closed by the walk
    # (`altwin::self-deplete`), and a real deckout deck runs Lab Maniac/Jace alongside it.
    # Inert on the gauntlet corpus (max co-occurrence 2 < node-admission 3).
    ('deckout-win', 'otag:alternate-win-condition AND otag:empty-library'),
    # Death-removal producer (#74 R2 §3.1). The `permanent-dies` event wants the
    # removal that KILLS (destroy/burn/sacrifice/sweeper/-X-X → graveyard, CR 700.4),
    # NOT the removal that changes zone without a death (exile/bounce/tuck, which
    # actively DODGE death triggers). Scryfall's `removal` otag children mix a METHOD
    # axis with a TARGET-TYPE axis and are not a clean tree (removal-creature itself
    # parents destroy/burn/bounce/fight children), so no union of method-children
    # separates death from non-death (it drops 141 sweeper/sunder deaths carrying only
    # removal-toughness, R2 STOP). The honest carve is SUBTRACTIVE: the holistic
    # `removal` population MINUS the three pure zone-change methods. AND/NOT over otags
    # is in the closed query vocabulary (ADR 0009 — no new atom). Measured 5,551 members
    # (raw removal 6,751 minus 1,200 exile/bounce/tuck); all destroys/burns/sacrifices/proper
    # sweepers kept (the 141 return). Residual imperfection = disable-auras that carry a
    # bare `removal` tag with no death method (Pacifism/Sleep Paralysis-likes): only ~84
    # pacifism-tagged (1.5% of the set) — a small bounded set that was ALSO in the raw
    # `removal` seed before (the carve only ever REMOVES, never adds, so precision
    # strictly improves). Known conservative under-coverage: ~182 MODAL cards that carry
    # a death method AND an exile/bounce/tuck mode (Bant Charm destroy-or-tuck, Chandra
    # Awakened Inferno burn+exile sweeper) are excluded by the NOT — modal, not pure
    # death-producers, so the conservative drop is acceptable (proof:
    # subagents/r2-tier-b-narrowing/outbox.md). The holistic `removal` otag is UNTOUCHED
    # elsewhere (permanent-leaves-battlefield etc. correctly keep it). Like
    # `color-fixing`/`rad-counter` it carries NO archetype edge (distills to None) — it
    # surfaces the death-removal cards for the `permanent-dies` producer seed, it does
    # not rename the deck.
    ('dies-removal', 'otag:removal AND NOT otag:removal-exile AND NOT otag:removal-bounce AND NOT otag:removal-tuck'),
    # Within-card lifetrade edges (Queza family). Each names a card that couples
    # TWO effects in ONE sentence: a trigger effect that drives a second payoff
    # effect (draw -> drain, gain -> draw, ...). Membership is a compound query:
    # a same-sentence `[^.]*` proximity regex over the (lowercased) oracle text,
    # intersected with the otags that pin the card's intent (so the regex word
    # sense is disambiguated by the matching `*-matters`/payoff otag). The
    # proximity regex uses unbounded `[^.]*` (0 RE2 budget) to stay a single
    # sentence yet Scryfall-pasteable. The trigger->effect ROLE pair lives only in
    # the attribute NAME (and this comment): the synth vocab is `(name, query)`
    # with no metadata slot, so a later edge-derivation pass reads the role off the
    # name. Like `rad-counter` these carry NO archetype edge (distill to None) — they
    # surface the member cards for that pass, they do not rename the deck.
    #
    # Disjunctions (cats 1 & 3) use a parenthesized exact-otag OR-group, the same
    # `(otag:a OR otag:b)` form the identity queries use. #7 (draw-to-drain-and-gain,
    # the full Queza shape) is DELIBERATELY NOT registered as a 7th attribute: the
    # vocab composes attributes as independent predicates (no query can reference
    # another attribute's name), and #7 is exactly the set intersection of
    # `draw-to-drain` and `draw-to-gain`. Re-inlining both regexes + otag lists into
    # a third query would duplicate them; the derived composition (membership #1 AND
    # membership #2) is the idiomatic, DRY form for the edge-derivation pass to take.
    ('draw-to-drain', 'oracle:/draw[^.]*los/ AND otag:draw-matters AND (otag:opponent-loses-life OR otag:drain-life)'),
    ('draw-to-gain', 'oracle:/draw[^.]*gain/ AND otag:draw-matters AND otag:lifegain'),
    (
        'gain-to-drain',
        'oracle:/gain[^.]*los/ AND otag:lifegain-matters AND (otag:opponent-loses-life OR otag:drain-life)',
    ),
    # `opponent` anchor drops self-life-loss false positives (e.g. Transcendence).
    ('drain-to-gain', 'oracle:/opponent[^.]*los[a-z]*[^.]*gain/ AND otag:life-loss-matters AND otag:lifegain'),
    # `life` token fixes word sense (drops "gains an ability"); the draw payoff has
    # no narrow produce-otag, so the regex word carries the effect (no effect otag).
    ('gain-to-draw', 'oracle:/gain[^.]*life[^.]*draw/ AND otag:lifegain-matters'),
    ('drain-to-draw', 'oracle:/opponent[^.]*los[a-z]*[^.]*draw/ AND otag:life-loss-matters'),
]


@dataclass(frozen=True)
class CompiledIdentity:
    name: str
    predicate: Predicate
    strategy: str


def _merge_overlay(base_pairs: list[tuple[str, str]], overlay: Mapping[str, str] | None) -> list[tuple[str, str]]:
    """Deep-merge an overlay's queries over an ordered base (user precedence)."""
    if not overlay:
        return list(base_pairs)
    out = [(n, overlay.get(n, q)) for n, q in base_pairs]
    known = {n for n, _ in base_pairs}
    out.extend((n, q) for n, q in overlay.items() if n not in known)
    return out


def _distill_identity_order(
    *,
    overlay: Mapping | None,
) -> list[tuple[str, str]]:
    """Assemble the ordered identity list (distillation precedence).

    EDHREC and new-bucket identities are always included (the ``edhrec_labels``
    knob and the ``new_buckets`` group toggle were both removed as frozen
    configuration -- ADR 0006).
    """
    attr_overlay = (overlay or {}).get('attributes')
    pairs: list[tuple[str, str]] = []
    pairs += EDHREC_IDENTITIES
    pairs += NEWBUCKET_IDENTITIES
    pairs += BASE_IDENTITIES
    return _merge_overlay(pairs, attr_overlay)


def build_strategy_edges(overlay: Mapping | None = None) -> dict[str, str]:
    edges = dict(ARCHETYPE_EDGES)
    if overlay and overlay.get('archetype_edges'):
        edges.update(overlay['archetype_edges'])
    return edges


@dataclass(frozen=True)
class StrategyDistiller:
    """Compiled, config-bound distiller: attribute NAME -> strategy (or ``None``).

    The identities are matched in precedence order against the pseudo-card of the
    name; the first match's edge is the strategy. ``distill_attr`` on the
    assets bundle is exactly ``distiller.distill(name)`` — zero slug literals, zero if/elif.
    """

    identities: tuple[CompiledIdentity, ...]

    def distill(self, name: str) -> str | None:
        view = CardView.for_name(name)
        for ident in self.identities:
            if ident.predicate(view):
                return ident.strategy
        return None


def build_strategy_distiller(
    *,
    overlay: Mapping | None = None,
) -> StrategyDistiller:
    pairs = _distill_identity_order(overlay=overlay)
    edges = build_strategy_edges(overlay)
    compiled = tuple(
        CompiledIdentity(name=n, predicate=compile_query(q), strategy=edges[n]) for n, q in pairs if n in edges
    )
    return StrategyDistiller(identities=compiled)


def compile_synth(
    *,
    overlay: Mapping | None = None,
) -> list[tuple[str, Predicate]]:
    """Compile the ordered synth-attribute (card-field) membership predicates.

    EDHREC synth attributes are always included (the ``edhrec_labels`` knob was
    removed as frozen configuration -- ADR 0006)."""
    pairs: list[tuple[str, str]] = []
    pairs += SYNTH_EDHREC_ATTRIBUTES
    pairs += SYNTH_ALWAYS_ATTRIBUTES
    syn_overlay = (overlay or {}).get('synth_attributes')
    pairs = _merge_overlay(pairs, syn_overlay)
    return [(n, compile_query(q)) for n, q in pairs]


def synth_attrs_for(card: dict, compiled: Iterable[tuple[str, Predicate]]) -> set[str]:
    view = CardView.from_card(card)
    return {name for name, pred in compiled if pred(view)}
