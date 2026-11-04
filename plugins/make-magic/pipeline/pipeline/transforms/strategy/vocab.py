"""The declarative identity vocabulary — the single source of truth for routing.

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
the edge is ``ARCHETYPE_EDGES``. Routing an attribute name is: find the identity
whose query matches that name, then take its edge. The identity order below is the
routing precedence — the first matching identity wins, which is how overlapping
mechanic families (a land that is also graveyard recursion; a treasure that is
also an artifact payoff) resolve to a single strategy. Config toggles select
which identity blocks are active; the ``overlay`` parameter of
:func:`build_router` deep-merges user extensions over this pristine base.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from pipeline.transforms.strategy.query import CardView, Predicate, compile_query

#: EDHREC new-node identities (active when ``cfg.edhrec_labels``). Ordered: this
#: block is the highest routing precedence, matching the engine's tier-1 router.
EDHREC_IDENTITIES: list[tuple[str, str]] = [
    ('devotion', 'otag:devotion-signal'),
    ('vehicles', 'otag:synergy-vehicle OR otag:crew OR otag:bring-your-own-crew '
                 'OR otag:alternative-crewing OR otag:animate-vehicle OR otag:crewless-vehicle '
                 'OR otag:vehicle-type'),
    ('energy', 'otag:energy-generator OR otag:counter-fuel-energy OR otag:energy-increaser '
               'OR otag:synergy-energy OR otag:energy-text'),
    ('extra-turns', 'otag:extra-turn OR otag:extra-upkeep OR otag:extra-turn-text'),
    ('mill', 'otag:mill-opponent OR otag:mill-any OR otag:synergy-mill'),
    ('planeswalkers', 'otag:synergy-planeswalker OR otag:recursion-planeswalker '
                      'OR otag:protects-planeswalker OR otag:superfriends'),
    ('discard-wheels', 'otag:discard-matters OR otag:self-discard-matters OR otag:wheel '
                       'OR otag:wheel-one-sided OR otag:wheel-symmetrical '
                       'OR otag:wheel-symmetrical-optional OR otag:miniwheel'),
    ('treasure-food', 'otag:repeatable-treasures OR otag:synergy-treasure OR otag:repeatable-clues '
                      'OR otag:synergy-clue OR otag:repeatable-blood OR otag:synergy-blood '
                      'OR otag:repeatable-food OR otag:synergy-food OR otag:eat-food OR otag:food'),
    ('theft-edhrec', 'otag:theft OR otag:theft-creature OR otag:theft-artifact OR otag:theft-land '
                     'OR otag:theft-planeswalker OR otag:theft-spell OR otag:theft-cast '
                     'OR otag:nightveil-theft'),
    ('poison', 'otag:synergy-poison OR otag:poison-opponents OR otag:poison-mechanics '
               'OR otag:poisonous OR otag:synergy-toxic OR otag:synergy-infect'),
    ('enchantress', 'otag:synergy-enchantment OR otag:synergy-aura OR otag:synergy-shrine '
                    'OR otag:synergy-enchantment-creature OR otag:enchantmentfall '
                    'OR otag:enchantment-engine OR otag:constellation '
                    'OR otag:repeatable-enchantment-tokens OR otag:repeatable-aura-tokens '
                    'OR otag:affinity-for-enchantments OR otag:creature-type-enchantress '
                    'OR otag:quick-enchant OR otag:auto-attach OR otag:cast-enchantment-trigger '
                    'OR otag:enchantment-cost-reducer'),
]

#: New-bucket identities (active when the ``new_buckets`` group is enabled).
NEWBUCKET_IDENTITIES: list[tuple[str, str]] = [
    ('food', 'otag:repeatable-food OR otag:synergy-food OR otag:eat-food OR otag:food'),
    ('mm-counters', 'otag:gives-mm-counters OR otag:gains-mm-counters OR otag:mm-counters-matter '
                    'OR otag:removes-mm-counters-self OR otag:mm_counters'),
    ('theft-newbuckets', 'otag:theft-creature OR otag:theft-artifact OR otag:theft-land '
                         'OR otag:theft-planeswalker OR otag:theft-spell OR otag:theft'),
]

#: Anthem deconfliction identity (active only when ``deconf_anthem`` is enabled);
#: routes the broad power/toughness-matters payoffs into the anthem archetype.
ANTHEM_DECONF_IDENTITIES: list[tuple[str, str]] = [
    ('anthem-deconf', 'otag:power-matters OR otag:toughness-matters'),
]

#: Always-active identities, in routing precedence. Every family is named by its
#: exact otag parent (ancestor-closure carries the children) or, where the tag DAG
#: does not parent the family under one root, by an explicit exact OR-list that
#: enumerates the orphan/sibling slugs (``attacking-matters`` + ``-self``/``-any``;
#: ``lifegain`` + ``lifegain-matters``/``-increaser``/``-to-damage``; ``tax`` +
#: ``tax-attack``/``-block``; ``synergy-artifact`` + ``-creature``). The hand-rolled
#: unions (``counters`` pp-counters; ``deathtouch``) are likewise exact OR-lists.
#: A granted-keyword payload slug is simply left out of the list rather than guarded
#: away: ``gives-pp-counters`` is not counters, ``gives-deathtouch`` is not
#: deathtouch, and an unlisted ``gives-*`` slug falls through to "no identity". The
#: facts-only ``enchantments`` archetype lists the enchantment-*synergy* members
#: (the removal/hate/disenchant slugs the old broad regex also swept up are not an
#: enchantment strategy); the real synergy payoffs route to ``enchantress`` first.
BASE_IDENTITIES: list[tuple[str, str]] = [
    ('typal/tribal', 'otag:typal OR otag:changeling'),
    ('tokens/go-wide', 'otag:tokens OR otag:token-increaser OR otag:repeatable-token-generator '
                       'OR otag:repeatable-creature-tokens OR otag:repeatable-noncreature-tokens '
                       'OR otag:synergy-token'),
    ('counters', 'otag:counters OR otag:gains-pp-counters OR otag:pp-counters-matter '
                 'OR otag:repeatable-pp-counters OR otag:counters-matter '
                 'OR otag:counter-increaser OR otag:proliferate'),
    ('sacrifice', 'otag:sac OR otag:sacrifice-outlet OR otag:your-sacrifice-matters '
                  'OR otag:sacrifice-matters OR otag:death-trigger OR otag:death-trigger-self '
                  'OR otag:free-sacrifice-outlet OR otag:sacrifice-self'),
    ('lands-matter', 'otag:landfall OR otag:lands-matter OR otag:land-count-matters '
                     'OR otag:extra-land OR otag:recursion-land OR otag:reanimate-land'),
    ('graveyard/reanimator', 'otag:graveyard OR otag:recursion OR otag:reanimate '
                             'OR otag:cards-in-graveyard-matter OR otag:castable-from-graveyard '
                             'OR otag:graveyard-fuel OR otag:graveyard-fuel-self '
                             'OR otag:activate-from-graveyard OR otag:mill-self'),
    ('spellslinger', 'otag:spellslinger OR otag:cast-trigger-you OR otag:cast-trigger-other '
                     'OR otag:second-spell-matters OR otag:storm-count-matters OR otag:synergy-instant '
                     'OR otag:synergy-sorcery OR otag:synergy-noncreature OR otag:prowess-like '
                     'OR otag:copy-instant OR otag:copy-sorcery'),
    ('artifacts', 'otag:artifacts OR otag:synergy-artifact OR otag:synergy-artifact-creature '
                  'OR otag:artifact-count-matters OR otag:affinity-for-artifacts '
                  'OR otag:repeatable-treasures OR otag:artifactfall'),
    ('blink/flicker', 'otag:flicker'),
    ('voltron/equipment', 'otag:equipment OR otag:auras OR otag:synergy-equipment '
                          'OR otag:synergy-equipment-legendary OR otag:synergy-aura '
                          'OR otag:quick-equip OR otag:auto-equip'),
    ('lifegain', 'otag:lifegain OR otag:lifegain-matters OR otag:lifegain-increaser '
                 'OR otag:lifegain-to-damage OR otag:repeatable-lifegain'),
    ('stax/prison', 'otag:stax OR otag:tax OR otag:tax-attack OR otag:tax-block OR otag:cast-tax'),
    ('deathtouch', 'otag:deathtouch-combat OR otag:deathtouch-counter '
                   'OR otag:deathtouch-to-planeswalkers OR otag:gains-deathtouch '
                   'OR otag:hate-deathtouch OR otag:old-blocking-deathtouch '
                   'OR otag:old-damage-deathtouch OR otag:removes-deathtouch '
                   'OR otag:synergy-deathtouch OR otag:tutor-creature-deathtouch'),
    ('control/permission', 'otag:counterspells OR otag:counterspell'),
    ('anthem/go-wide', 'otag:anthem OR otag:power-boost-to-all OR otag:toughness-boost-to-all'),
    ('extra-combat', 'otag:extra_combat OR otag:extra-combat-phase'),
    ('burn', 'otag:burn OR otag:burn-player OR otag:opponent-loses-life OR otag:drain-life'),
    ('combat/aggro', 'otag:combat OR otag:attacking-matters OR otag:attacking-matters-self '
                     'OR otag:attacking-matters-any OR otag:attack-trigger'),
    ('toughness-matters', 'otag:toughness-matters OR otag:toughness-matters-self OR otag:buttstrike '
                          'OR otag:turns-off-defender OR otag:turns-off-defender-self '
                          'OR otag:synergy-defender'),
    ('morph', 'otag:morph OR otag:synergy-face-down OR otag:face-up-face-down-effects '
              'OR otag:turn-face-down OR otag:synergy-manifest OR otag:synergy-manifest-dread '
              'OR otag:synergy-disguise'),
    ('alt-wincon/combo', 'otag:wincon OR otag:alternate-win-condition'),
    ('enchantments', 'otag:synergy-enchantment OR otag:synergy-enchantment-creature '
                     'OR otag:synergy-aura OR otag:synergy-shrine OR otag:synergy-saga '
                     'OR otag:constellation OR otag:enchantmentfall OR otag:enchantment-engine'),
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
    # anthem deconfliction
    'anthem-deconf': 'anthem/go-wide',
    # base
    'typal/tribal': 'typal/tribal',
    'tokens/go-wide': 'tokens/go-wide',
    'counters': '+1/+1 counters',
    'sacrifice': 'aristocrats/sacrifice',
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
    'alt-wincon/combo': 'alt-wincon/combo',
    'enchantments': 'enchantments',
}

#: Synthesized attributes: a card gets these by card-field query, not by otag.
#: Ordered; the EDHREC block is active only when ``cfg.edhrec_labels``. These
#: names also route — as the pseudo-otag of their identity above (``vehicle-type``
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
]


@dataclass(frozen=True)
class CompiledIdentity:
    name: str
    predicate: Predicate
    strategy: str


def _merge_overlay(base_pairs: list[tuple[str, str]],
                   overlay: Mapping[str, str] | None) -> list[tuple[str, str]]:
    """Deep-merge an overlay's queries over an ordered base (user precedence)."""
    if not overlay:
        return list(base_pairs)
    out = [(n, overlay.get(n, q)) for n, q in base_pairs]
    known = {n for n, _ in base_pairs}
    out.extend((n, q) for n, q in overlay.items() if n not in known)
    return out


def _resolve_identity_order(
    *, edhrec: bool, newbuckets: bool, anthem_deconf: bool,
    overlay: Mapping | None,
) -> list[tuple[str, str]]:
    """Assemble the config-gated, ordered identity list (routing precedence)."""
    attr_overlay = (overlay or {}).get('attributes')
    pairs: list[tuple[str, str]] = []
    if edhrec:
        pairs += EDHREC_IDENTITIES
    if newbuckets:
        pairs += NEWBUCKET_IDENTITIES
    if anthem_deconf:
        pairs += ANTHEM_DECONF_IDENTITIES
    pairs += BASE_IDENTITIES
    return _merge_overlay(pairs, attr_overlay)


def build_edges(overlay: Mapping | None = None) -> dict[str, str]:
    edges = dict(ARCHETYPE_EDGES)
    if overlay and overlay.get('archetype_edges'):
        edges.update(overlay['archetype_edges'])
    return edges


@dataclass(frozen=True)
class Router:
    """Compiled, config-bound router: attribute NAME -> strategy (or ``None``).

    The identities are matched in precedence order against the pseudo-card of the
    name; the first match's edge is the strategy. ``archetype_of_attr`` on the
    assets bundle is exactly ``router(name)`` — zero slug literals, zero if/elif.
    """

    identities: tuple[CompiledIdentity, ...]

    def route(self, name: str) -> str | None:
        view = CardView.for_name(name)
        for ident in self.identities:
            if ident.predicate(view):
                return ident.strategy
        return None


def build_router(
    *, edhrec: bool, newbuckets: bool, anthem_deconf: bool,
    overlay: Mapping | None = None,
) -> Router:
    pairs = _resolve_identity_order(
        edhrec=edhrec, newbuckets=newbuckets, anthem_deconf=anthem_deconf, overlay=overlay)
    edges = build_edges(overlay)
    compiled = tuple(
        CompiledIdentity(name=n, predicate=compile_query(q), strategy=edges[n])
        for n, q in pairs if n in edges
    )
    return Router(identities=compiled)


def compile_synth(
    *, edhrec: bool, overlay: Mapping | None = None,
) -> list[tuple[str, Predicate]]:
    """Compile the ordered synth-attribute (card-field) membership predicates."""
    pairs: list[tuple[str, str]] = []
    if edhrec:
        pairs += SYNTH_EDHREC_ATTRIBUTES
    pairs += SYNTH_ALWAYS_ATTRIBUTES
    syn_overlay = (overlay or {}).get('synth_attributes')
    pairs = _merge_overlay(pairs, syn_overlay)
    return [(n, compile_query(q)) for n, q in pairs]


def synth_attrs_for(card: dict, compiled: Iterable[tuple[str, Predicate]]) -> set[str]:
    view = CardView.from_card(card)
    return {name for name, pred in compiled if pred(view)}
