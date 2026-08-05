"""Validation vocabulary shared by the card- and deck-export destinations.

A deck can be rendered to a target format's text, but a card in it may not be
usable at that target: it may be UNRESOLVED (the pipeline never matched it to a
Scryfall ``oracle_id`` — "name-only") or ABSENT_FROM_TARGET (the target — e.g.
Forge's card DB — simply doesn't have it). The first is destination-agnostic; the
second is destination-specific. Without this check a name-only or Forge-absent
card is emitted verbatim and only "works" when the target happens to have the
name, otherwise producing a silently short/broken deck.

This module is the small, dependency-free core: the issue taxonomy, a per-deck
:class:`ValidationReport`, a loud :class:`DeckExportError`, and the shared
:func:`export_checked` that turns a report into a fail-before-emit gate. The
concrete availability check lives with each destination (forge_dck composes a
:class:`~pipeline.destinations.card_export.ForgeDckCardExporter` backed by a
Forge card index); this module knows nothing about Forge.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pipeline.contracts import Deck
    from pipeline.destinations.deck_export import DeckExporter

__all__ = (
    'CardIssue',
    'DeckExportError',
    'ExportResult',
    'IssueKind',
    'Severity',
    'ValidationReport',
    'export_checked',
)


class Severity(Enum):
    """How bad a :class:`CardIssue` is for actually using the exported deck."""

    #: The deck can still be exported/run; surface it, don't block.
    WARNING = 'warning'
    #: The deck will mis-load at the target; refuse to export (unless overridden).
    BLOCKING = 'blocking'


class IssueKind(Enum):
    """What is wrong with a card, for a given export target."""

    #: No ``oracle_id`` — the pipeline never resolved this card (Scryfall miss).
    #: Destination-agnostic. WARNING: it may still be present at the target by name.
    UNRESOLVED = 'unresolved'
    #: The target's card set does not contain this name. Destination-specific.
    #: BLOCKING: the deck will short/mis-load (this is the real footgun).
    ABSENT_FROM_TARGET = 'absent_from_target'


@dataclass(frozen=True)
class CardIssue:
    """One problem with one card, for one export target."""

    card_name: str
    kind: IssueKind
    severity: Severity
    detail: str


@dataclass(frozen=True)
class ValidationReport:
    """The issues found validating a whole deck for a target (empty == all clear)."""

    deck_name: str
    issues: tuple[CardIssue, ...]

    @property
    def ok(self) -> bool:
        """True when there are no issues at all (not even warnings)."""
        return not self.issues

    @property
    def blocking(self) -> tuple[CardIssue, ...]:
        """The subset of issues that must block an export (severity BLOCKING)."""
        return tuple(i for i in self.issues if i.severity is Severity.BLOCKING)

    @property
    def warnings(self) -> tuple[CardIssue, ...]:
        """The subset of issues that are advisory (severity WARNING)."""
        return tuple(i for i in self.issues if i.severity is Severity.WARNING)


@dataclass(frozen=True)
class ExportResult:
    """The rendered text plus the validation report that cleared it."""

    text: str
    report: ValidationReport


class DeckExportError(ValueError):
    """A deck cannot be exported because it has BLOCKING validation issues.

    Subclasses :class:`ValueError` so the CLI's existing top-level handler turns
    it into a clean ``error:`` line + non-zero exit (never a traceback). Carries
    the :class:`ValidationReport` for programmatic callers.
    """

    def __init__(self, report: ValidationReport) -> None:
        self.report = report
        blocking = report.blocking
        names = ', '.join(f'{i.card_name} ({i.detail})' for i in blocking)
        super().__init__(
            f'deck {report.deck_name!r} cannot be exported: {len(blocking)} card(s) unusable at the target — {names}'
        )


def export_checked(
    exporter: DeckExporter,
    deck: Deck,
    *,
    strict: bool = True,
) -> ExportResult:
    """Validate ``deck`` for ``exporter``'s target, then render — or refuse.

    Runs ``exporter.validate(deck)``; if ``strict`` and the report has BLOCKING
    issues, raises :class:`DeckExportError` (naming the offending cards) before
    rendering — so a downstream consumer (e.g. the sim) fails fast with an
    actionable message instead of spawning work on a deck that will mis-load.
    ``strict=False`` downgrades blocking to advisory (the ``--allow-missing``
    escape hatch): it still returns the rendered text + the full report so the
    caller can warn. ``export(deck)`` itself stays lenient/unchanged.
    """
    report = exporter.validate(deck)
    if report.blocking and strict:
        raise DeckExportError(report)
    return ExportResult(text=exporter.export(deck), report=report)


def merge_reports(deck_name: str, reports: Iterable[ValidationReport]) -> ValidationReport:
    """Flatten several per-deck reports into one (used when a call spans decks)."""
    issues: list[CardIssue] = []
    for r in reports:
        issues.extend(r.issues)
    return ValidationReport(deck_name=deck_name, issues=tuple(issues))
