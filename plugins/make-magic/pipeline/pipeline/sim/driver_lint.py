"""Layer-1 forbidden-API bytecode scan for in-search quad drivers.

A make-magic quad driver may ONLY enqueue LEGAL game actions (cast / activate / choose /
target / priority decisions). Every terminal state MUST come from the rules engine. A driver
that reaches for a game/player TERMINAL or STATE-FABRICATION API is asserting a win it never
played — the ``MACRO_FIRE_REAL → gameOver=true`` on turn 1 pathology where the opponent
(holding counterspells) never got priority. That data is synthetic and must be rejected.

This module enforces the rule STATICALLY at gate/compile time by parsing the constant pool of
every compiled ``.class`` in a driver's ``classes_dir`` — in pure Python, reading the
``Methodref`` / ``InterfaceMethodref`` entries (class name + method name) directly. It does NOT
shell to ``javap`` (that would add a fragile toolchain dependency); the ``.class`` constant
pool format is simple and self-describing.

Two severities:

  * **FAIL** — a driver referencing any of these is REJECTED by the gate:

    - ``mage.players.Player``.{``lost``, ``won``, ``leave``, ``quit``, ``setLosses``,
      ``setWins``}
    - ``mage.game.Game``.{``end``, ``setWinner``}
    - ``concede`` (on any owner) — a driver forcing the OPPONENT to concede yields an
      engine-legitimate, aggregator-CREDITED decisive win (a concession is a rules-legal loss).
      Self-concession was only ever a theoretical nicety, so drivers may not concede at all —
      safety wins.

    - zone-fabrication: ``moveCards`` / ``moveCardTo*`` (any ``moveCard*``) on a ``mage/`` owner.
      These move cards between zones WITHOUT paying costs or passing priority — the buggy macro
      used them to exile the library and drop Thassa's Oracle into play, manufacturing a credited
      deck-out. A driver's only legal levers are casts / activations / choices; the rules engine
      owns every zone change, so a direct ``moveCard*`` has NO safe driver use and FAILs. (The
      bounded combo-resolution line is expressed as ``cast`` + yield priority to the engine, never
      as a direct zone move — see the priority-fair authoring seed.)

    These directly fabricate a terminal state / zone / victory without playing the line.

All forbidden APIs FAIL hard: the rules engine owns every terminal state AND every zone change, a
driver-forced concession fabricates a credited win, and a direct zone move fabricates the board a
line never legally produced. There is no WARN tier — a driver either acts only through legal
actions or it is rejected.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

__all__ = (
    'ClassScan',
    'Finding',
    'LintResult',
    'lint_class_bytes',
    'lint_driver_classes',
    'scan_class_bytes',
)

# Constant-pool tags (JVMS §4.4). Only the ones we must walk to size entries correctly.
_TAG_UTF8 = 1
_TAG_INTEGER = 3
_TAG_FLOAT = 4
_TAG_LONG = 5
_TAG_DOUBLE = 6
_TAG_CLASS = 7
_TAG_STRING = 8
_TAG_FIELDREF = 9
_TAG_METHODREF = 10
_TAG_INTERFACE_METHODREF = 11
_TAG_NAME_AND_TYPE = 12
_TAG_METHOD_HANDLE = 15
_TAG_METHOD_TYPE = 16
_TAG_DYNAMIC = 17
_TAG_INVOKE_DYNAMIC = 18
_TAG_MODULE = 19
_TAG_PACKAGE = 20

#: Terminal-state / victory-assertion APIs — no legitimate driver use → FAIL. Keyed by an
#: owner-class-name SUBSTRING (bytecode uses ``/`` separators, e.g. ``mage/players/Player``;
#: a substring also catches concrete subclasses like ``mage/game/GameImpl``) to a set of method
#: names.
_FAIL_DENYLIST: tuple[tuple[str, frozenset[str]], ...] = (
    ('mage/players/Player', frozenset({'lost', 'won', 'leave', 'quit', 'setLosses', 'setWins'})),
    ('mage/game/Game', frozenset({'end', 'setWinner'})),
)

#: Terminal/victory-assertion method names forbidden on ANY owner in the ``mage/`` package
#: namespace (Sol HIGH 2). Owner-substring denylisting missed a call compiled against a concrete
#: subclass whose name lacks the ``Game``/``Player`` substring (e.g. ``mage/game/CommanderFreeForAll``
#: or ``mage/players/HumanControlled``); scoping by the ``mage/`` namespace catches every such
#: subclass while leaving a NON-mage owner that coincidentally shares a method name allowed.
_MAGE_TERMINAL_METHODS: frozenset[str] = frozenset(
    {'lost', 'won', 'leave', 'quit', 'setLosses', 'setWins', 'setWinner'}
)
#: ``end`` is a common method name, so it is scoped to the ``mage/game/`` package specifically
#: (a game's terminal API) rather than the whole ``mage/`` namespace, to avoid flagging an
#: unrelated ``.end()`` elsewhere in mage while still catching every Game subclass.
_MAGE_GAME_PACKAGE = 'mage/game/'

#: Reflective / dynamic-invocation escape hatches (Sol HIGH 2): a driver has no legitimate need
#: for reflection, and reflection defeats every static owner/method check. FAIL on:
#:   * ``java/lang/reflect/Method.invoke``
#:   * any ``java/lang/invoke/MethodHandle*`` invocation
#:   * ``java/lang/Class.{getMethod,getDeclaredMethod}`` (resolving a method by name at run time)
#: NOTE: the compiler-generated invokedynamic bootstraps ``LambdaMetafactory.metafactory`` and
#: ``StringConcatFactory.makeConcatWithConstants`` are NOT these owners and stay allowed.
_REFLECT_CLASS_METHODS: frozenset[str] = frozenset({'getMethod', 'getDeclaredMethod'})


def _is_reflection(owner: str, method: str) -> bool:
    if owner == 'java/lang/reflect/Method' and method == 'invoke':
        return True
    if owner.startswith('java/lang/invoke/MethodHandle') and method in (
        'invoke', 'invokeExact', 'invokeWithArguments',
    ):
        return True
    return owner == 'java/lang/Class' and method in _REFLECT_CLASS_METHODS


@dataclass(frozen=True)
class ClassScan:
    """The extracted references of one parsed ``.class``.

    ``this_class`` is the internal binary name of the class itself (``a/b/C``). ``method_refs``
    is every ``Methodref`` / ``InterfaceMethodref`` as ``(owner_internal_name, method_name)``.
    """

    this_class: str
    method_refs: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class Finding:
    """One forbidden-API hit. ``severity`` is ``'FAIL'`` or ``'WARN'``; ``detail`` names the
    class + the forbidden ``Owner.method`` reference in human form."""

    severity: str
    class_name: str
    owner: str
    method: str
    detail: str


@dataclass(frozen=True)
class LintResult:
    """The aggregate verdict over a driver's classes_dir.

    ``ok`` is False iff any FAIL finding exists. ``fail_findings`` / ``warn_findings`` split the
    findings by severity. ``summary`` is a one-line human message naming the FAIL refs (empty on
    a clean pass).
    """

    ok: bool
    findings: tuple[Finding, ...] = ()
    fail_findings: tuple[Finding, ...] = field(default_factory=tuple)
    warn_findings: tuple[Finding, ...] = field(default_factory=tuple)
    summary: str = ''


class ClassParseError(ValueError):
    """A ``.class`` blob was too short / malformed to parse its constant pool."""


def _short_owner(internal: str) -> str:
    """``mage/players/Player`` → ``Player`` for readable messages."""
    return internal.rsplit('/', 1)[-1]


def scan_class_bytes(data: bytes) -> ClassScan:
    """Parse ``data`` (one ``.class`` file) and return its class name + method references.

    Reads the constant pool per JVMS §4.4 in pure Python: every ``Methodref`` /
    ``InterfaceMethodref`` resolves through its ``Class`` and ``NameAndType`` to
    ``(owner_internal_name, method_name)``. ``Long``/``Double`` occupy two pool slots (§4.4.5).
    """
    if len(data) < 10 or data[:4] != b'\xca\xfe\xba\xbe':
        raise ClassParseError('not a .class file (bad magic or too short)')
    count = struct.unpack_from('>H', data, 8)[0]  # constant_pool_count
    # Pass 1: record each entry's raw shape, indexed 1..count-1.
    utf8: dict[int, str] = {}
    class_name_idx: dict[int, int] = {}  # Class entry index -> name_index (Utf8)
    name_and_type: dict[int, tuple[int, int]] = {}  # index -> (name_index, descriptor_index)
    methodrefs: list[tuple[int, int]] = []  # (class_index, name_and_type_index)

    off = 10
    i = 1
    try:
        return _walk_pool(data, count, utf8, class_name_idx, name_and_type, methodrefs, off, i)
    except (struct.error, IndexError) as exc:
        # A truncated / malformed pool cannot be proven clean — fail CLOSED by raising, so the
        # caller records a FAIL rather than silently treating a half-parsed class as benign.
        msg = f'truncated or malformed constant pool: {exc}'
        raise ClassParseError(msg) from exc


def _walk_pool(
    data: bytes,
    count: int,
    utf8: dict[int, str],
    class_name_idx: dict[int, int],
    name_and_type: dict[int, tuple[int, int]],
    methodrefs: list[tuple[int, int]],
    off: int,
    i: int,
) -> ClassScan:
    while i < count:
        tag = data[off]
        off += 1
        if tag == _TAG_UTF8:
            (length,) = struct.unpack_from('>H', data, off)
            off += 2
            utf8[i] = data[off:off + length].decode('utf-8', 'replace')
            off += length
        elif tag in (_TAG_INTEGER, _TAG_FLOAT, _TAG_FIELDREF, _TAG_DYNAMIC, _TAG_INVOKE_DYNAMIC):
            off += 4
        elif tag in (_TAG_METHODREF, _TAG_INTERFACE_METHODREF):
            cls_idx, nt_idx = struct.unpack_from('>HH', data, off)
            methodrefs.append((cls_idx, nt_idx))
            off += 4
        elif tag == _TAG_NAME_AND_TYPE:
            n_idx, d_idx = struct.unpack_from('>HH', data, off)
            name_and_type[i] = (n_idx, d_idx)
            off += 4
        elif tag in (_TAG_LONG, _TAG_DOUBLE):
            off += 8
            i += 1  # 8-byte constants take two pool slots (JVMS §4.4.5).
        elif tag == _TAG_CLASS:
            (name_idx,) = struct.unpack_from('>H', data, off)
            class_name_idx[i] = name_idx
            off += 2
        elif tag in (_TAG_STRING, _TAG_METHOD_TYPE, _TAG_MODULE, _TAG_PACKAGE):
            off += 2
        elif tag == _TAG_METHOD_HANDLE:
            off += 3
        else:
            raise ClassParseError(f'unknown constant-pool tag {tag} at index {i}')
        i += 1

    this_class_idx = struct.unpack_from('>H', data, off + 2)[0]  # access_flags(2), this_class(2)
    this_class = utf8.get(class_name_idx.get(this_class_idx, -1), '<unknown>')

    refs: list[tuple[str, str]] = []
    for cls_idx, nt_idx in methodrefs:
        owner = utf8.get(class_name_idx.get(cls_idx, -1), '')
        n_idx, _d = name_and_type.get(nt_idx, (-1, -1))
        method = utf8.get(n_idx, '')
        if owner and method:
            refs.append((owner, method))
    return ClassScan(this_class=this_class, method_refs=tuple(refs))


def _classify(owner: str, method: str) -> tuple[str, str] | None:
    """Return ``(severity, kind)`` for a forbidden ref, or ``None`` if benign."""
    for sub, methods in _FAIL_DENYLIST:
        if sub in owner and method in methods:
            return 'FAIL', 'terminal'
    # Subclass owners (Sol HIGH 2): a terminal method on ANY mage/ owner, so a call compiled
    # against a concrete subclass whose name lacks the Game/Player substring is still caught.
    if owner.startswith('mage/'):
        if method in _MAGE_TERMINAL_METHODS:
            return 'FAIL', 'terminal-subclass'
        if method == 'end' and owner.startswith(_MAGE_GAME_PACKAGE):
            return 'FAIL', 'terminal-subclass'
    if _is_reflection(owner, method):
        return 'FAIL', 'reflection'
    if method == 'concede':
        # FAIL, not WARN: a driver forcing the OPPONENT to concede yields an engine-legitimate,
        # aggregator-credited decisive win (concede is a rules-legal loss). Drivers may not concede
        # at all — self-concession was only ever a theoretical nicety, so safety wins.
        return 'FAIL', 'concede'
    if method.startswith('moveCard') and owner.startswith('mage/'):
        # FAIL, not WARN: a driver moving cards between zones directly (``moveCards`` /
        # ``moveCardToExile*`` / ``moveCardToGraveyard*`` / …) mutates state WITHOUT paying
        # costs or passing priority — the exact ``moveCards``-exile-library + drop-Thassa's-Oracle
        # fabrication that manufactures a credited deck-out. A driver's only legal levers are
        # casts / activations / choices; the engine owns every zone change. The old WARN split
        # (a "sanctioned bounded-resolution move") is retired — there is no safe direct zone move.
        # Scoped to the ``mage/`` namespace so a call compiled against a concrete impl/subclass
        # (e.g. ``mage/players/PlayerImpl``) is caught too.
        return 'FAIL', 'zone-fabrication'
    return None


def lint_class_bytes(data: bytes, *, name: str) -> tuple[Finding, ...]:
    """Return the forbidden-API findings for one ``.class`` blob (FAIL + WARN)."""
    scan = scan_class_bytes(data)
    findings: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for owner, method in scan.method_refs:
        if (owner, method) in seen:
            continue
        seen.add((owner, method))
        verdict = _classify(owner, method)
        if verdict is None:
            continue
        severity, kind = verdict
        pretty = f'{_short_owner(owner)}.{method}'
        detail = f'{name}: {severity} {kind} call to {pretty} ({owner}.{method})'
        findings.append(Finding(severity=severity, class_name=name, owner=owner, method=method, detail=detail))
    return tuple(findings)


def lint_driver_classes(classes_dir: str | Path) -> LintResult:
    """Scan every ``.class`` under ``classes_dir`` and return the aggregate verdict.

    A FAIL finding anywhere makes ``ok`` False. WARN findings are recorded but never block.
    The scan is recursive (drivers compile into a package subtree). A missing/empty dir yields
    a clean pass (nothing to reject) — the compile gate already fails loud on a missing tree.
    """
    root = Path(classes_dir)
    all_findings: list[Finding] = []
    for cls in sorted(root.rglob('*.class')):
        try:
            data = cls.read_bytes()
            all_findings.extend(lint_class_bytes(data, name=cls.stem))
        except (ClassParseError, OSError) as exc:
            # Fail CLOSED (Sol HIGH 2): an unreadable or malformed/truncated .class cannot be
            # proven free of terminal-API calls, so it is a FAIL — never a silent skip that would
            # let an unscannable driver load.
            all_findings.append(
                Finding(
                    severity='FAIL',
                    class_name=cls.stem,
                    owner='<unscannable>',
                    method='<scan-error>',
                    detail=f'{cls.stem}: FAIL scan-error (fail-closed) — {exc}',
                )
            )
    fails = tuple(f for f in all_findings if f.severity == 'FAIL')
    warns = tuple(f for f in all_findings if f.severity == 'WARN')
    if fails:
        summary = 'forbidden terminal-API references: ' + '; '.join(
            f'{f.class_name} → {_short_owner(f.owner)}.{f.method}' for f in fails
        )
    else:
        summary = ''
    return LintResult(
        ok=not fails,
        findings=tuple(all_findings),
        fail_findings=fails,
        warn_findings=warns,
        summary=summary,
    )
