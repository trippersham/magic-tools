"""The single card-query evaluator — the one resolution path for every attribute.

An attribute's membership ("which cards have this CardAttribute") is defined by a
Scryfall-style query string and nothing else. This module parses a query once
into a predicate over a lake-resolved card view, so a compiled attribute costs
one predicate call at runtime — the same order as the set-ops it replaces.

The SAME evaluator serves two callers, and that symmetry is the design's keystone:

* **Membership** — the predicate runs against a real card view (its ``otags``,
  ``keywords``, ``type_line``, ``oracle_text``, ``mana_cost``) to decide whether
  the card carries the attribute.
* **Routing** — the identity vocabulary (:mod:`.vocab`) routes an attribute NAME
  by running the identity queries against a *pseudo-card* whose only otag is that
  name. Then ``otag:typal`` is exactly ``name == 'typal'`` and ``otag:changeling``
  is exactly ``name == 'changeling'`` — the attribute-name equality the
  deterministic router performs, expressed in the one query language. Families are
  named by their exact otag parent (card otags are ancestor-closed, so the parent
  carries the children) or by an explicit exact OR-list.

Operators
---------
``otag:<slug>``       exact membership in ``card.otags`` — the only otag form. Card
                      otags are ancestor-closed, so a family is named by its exact
                      parent (or an explicit exact OR-list in :mod:`.vocab`); the
                      prefix/suffix/substring matchers were removed with that
                      migration (a wildcard in an ``otag:`` value now raises).
``keyword:<kw>``      case-insensitive membership in ``card.keywords``.
``type:<T>``          case-sensitive substring of ``card.type_line``.
``oracle:<substr>``   case-insensitive substring of ``card.oracle_text``.
``devotion``          ``'devotion'`` in the oracle text, OR a single-color pip
                      count >= 2 in the mana cost.
``AND`` ``OR`` ``NOT`` and ``( )`` compose the above (``NOT`` binds tightest,
then ``AND``, then ``OR``). A value containing spaces or parentheses is single
quoted: ``oracle:'energy counter'``.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

#: A compiled attribute: a predicate over a :class:`CardView`.
Predicate = Callable[["CardView"], bool]

_PIP_RE = re.compile(r'\{([WUBRG])\}')


@dataclass(frozen=True)
class CardView:
    """The normalized, lake-resolved fields a query reads — nothing else.

    Built once per card (and once per pseudo-card during routing) so every
    predicate over it is pure set/substring work.
    """

    otags: frozenset[str]
    keywords: frozenset[str]          # lowercased
    type_line: str
    oracle_text_lower: str
    mana_cost: str

    @classmethod
    def from_card(cls, card: dict) -> CardView:
        return cls(
            otags=frozenset(t.lower() for t in (card.get('otags') or [])),
            keywords=frozenset(str(k).lower() for k in (card.get('keywords') or [])),
            type_line=card.get('type_line') or '',
            oracle_text_lower=(card.get('oracle_text') or '').lower(),
            mana_cost=card.get('mana_cost') or '',
        )

    @classmethod
    def for_name(cls, name: str) -> CardView:
        """A pseudo-card whose only otag is ``name`` — the routing substrate."""
        return cls(otags=frozenset({name}), keywords=frozenset(),
                   type_line='', oracle_text_lower='', mana_cost='')


def _otag_pred(value: str) -> Predicate:
    # Exact otag membership — the only otag form. Card otags are ancestor-closed, so
    # a family is named by its exact parent (or an explicit exact OR-list); the
    # prefix/suffix/substring matchers were removed with that migration.
    if '*' in value:
        raise ValueError(f'wildcard otag match is unsupported; use an exact otag slug: {value!r}')
    return lambda c: value in c.otags


def _devotion_pred() -> Predicate:
    def pred(c: CardView) -> bool:
        if 'devotion' in c.oracle_text_lower:
            return True
        pips = Counter(_PIP_RE.findall(c.mana_cost))
        return bool(pips) and max(pips.values()) >= 2
    return pred


def _atom_pred(field: str, value: str) -> Predicate:
    if field == 'otag':
        return _otag_pred(value)
    if field == 'keyword':
        v = value.lower()
        return lambda c: v in c.keywords
    if field == 'type':
        return lambda c: value in c.type_line
    if field == 'oracle':
        v = value.lower()
        return lambda c: v in c.oracle_text_lower
    raise ValueError(f'unknown query field: {field!r}')


# --- tokenizer + recursive-descent parser ----------------------------------

_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<lparen>\()
      | (?P<rparen>\))
      | (?P<field>otag|keyword|type|oracle)\s*:\s*
        (?:'(?P<qval>[^']*)'|(?P<bval>[^\s()]+))
      | (?P<word>[A-Za-z_][A-Za-z0-9_\-]*)
    )
""", re.VERBOSE)

_KEYWORDS = {'AND', 'OR', 'NOT'}


def _tokenize(s: str) -> list[tuple[str, str]]:
    toks: list[tuple[str, str]] = []
    pos = 0
    while pos < len(s):
        if s[pos].isspace():
            pos += 1
            continue
        m = _TOKEN_RE.match(s, pos)
        if not m or m.end() == pos:
            raise ValueError(f'cannot tokenize query at {pos}: {s[pos:pos + 20]!r}')
        pos = m.end()
        if m.group('lparen'):
            toks.append(('lparen', '('))
        elif m.group('rparen'):
            toks.append(('rparen', ')'))
        elif m.group('field'):
            val = m.group('qval') if m.group('qval') is not None else m.group('bval')
            toks.append(('atom', f"{m.group('field')}:{val}"))
        else:
            w = m.group('word')
            if w in _KEYWORDS:
                toks.append((w.lower(), w))
            elif w == 'devotion':
                toks.append(('atom', 'devotion:'))
            else:
                raise ValueError(f'unexpected bare word in query: {w!r}')
    return toks


class _Parser:
    def __init__(self, toks: list[tuple[str, str]]) -> None:
        self.toks = toks
        self.i = 0

    def _peek(self) -> str | None:
        return self.toks[self.i][0] if self.i < len(self.toks) else None

    def _next(self) -> tuple[str, str]:
        t = self.toks[self.i]
        self.i += 1
        return t

    def parse(self) -> Predicate:
        pred = self._or()
        if self.i != len(self.toks):
            raise ValueError('trailing tokens in query')
        return pred

    def _or(self) -> Predicate:
        preds = [self._and()]
        while self._peek() == 'or':
            self._next()
            preds.append(self._and())
        if len(preds) == 1:
            return preds[0]
        return lambda c: any(p(c) for p in preds)

    def _and(self) -> Predicate:
        preds = [self._unary()]
        while self._peek() == 'and':
            self._next()
            preds.append(self._unary())
        if len(preds) == 1:
            return preds[0]
        return lambda c: all(p(c) for p in preds)

    def _unary(self) -> Predicate:
        if self._peek() == 'not':
            self._next()
            inner = self._unary()
            return lambda c: not inner(c)
        if self._peek() == 'lparen':
            self._next()
            inner = self._or()
            if self._peek() != 'rparen':
                raise ValueError('unbalanced parentheses in query')
            self._next()
            return inner
        kind, text = self._next()
        if kind != 'atom':
            raise ValueError(f'expected atom, got {text!r}')
        if text == 'devotion:':
            return _devotion_pred()
        field, _, value = text.partition(':')
        return _atom_pred(field, value)


def compile_query(query: str) -> Predicate:
    """Parse a query string into a reusable predicate over a :class:`CardView`."""
    toks = _tokenize(query)
    if not toks:
        raise ValueError('empty query')
    return _Parser(toks).parse()
