"""Tests for spoiler_sync — the thin façade over the pipeline spoiler lineage.

#5 Task 8 migrated the spoiler tracker onto the lake and DELETED its SQLite
(``spoiler_cache.db`` — the last SQLite in the plugin). This test proves the
``sync`` / ``status`` / ``list`` verbs AND their output shape are preserved for
``chasing-cards``, driving the lineage entirely from FIXTURE lake snapshots (no
SQLite, no network).

Run:
    uv run --with pytest --with typer --with rich --with duckdb --with pydantic \
        --with-editable ../pipeline pytest test_spoiler_sync.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PIPELINE = Path(__file__).resolve().parents[1] / "pipeline"
if str(_PIPELINE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE))
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import spoiler_sync  # noqa: E402
from pipeline.contracts import Card  # noqa: E402
from pipeline.sources import spoilers as source  # noqa: E402
from pipeline.transforms import spoilers as transform  # noqa: E402
from typer.testing import CliRunner  # noqa: E402

runner = CliRunner()


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("MAKE_MAGIC_DATA_DIR", str(root))
    return root


class _StubResolver:
    def __init__(self, cards: dict[str, Card]) -> None:
        self._cards = cards

    def get_card(self, name: str) -> Card | None:
        return self._cards.get(name)


def _seed_lake(resolver: _StubResolver, rows: list[dict[str, str]]) -> None:
    """Land raw preview rows and reconcile them into the normalized lake."""
    source._load(rows)
    transform.build(resolver=resolver)


# --------------------------------------------------------------------------- #
# sync — drives puller + transform, detects new-since-last-run from the lake.
# --------------------------------------------------------------------------- #


def test_sync_detects_new_from_lake_no_sqlite(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Mock the scrape (no network); resolve one slug, miss the other.
    monkeypatch.setattr(
        source,
        "scrape_set",
        lambda c, b, sc: [
            {"slug": "solRing", "image_url": "i", "set_code": sc, "detail_url": "d"},
            {
                "slug": "mysteryCard",
                "image_url": "i",
                "set_code": sc,
                "detail_url": "d",
            },
        ],
    )
    monkeypatch.setattr(source, "scrape_new", lambda c, b, t: [])
    monkeypatch.setattr(source.time, "sleep", lambda _s: None)
    # transform.build() late-imports default_card_resolver; patch that seam so the
    # façade drives the real transform with a stub resolver (no network).
    stub = _StubResolver({"sol Ring": Card(name="Sol Ring", oracle_id="oid-sol")})
    monkeypatch.setattr(
        "pipeline.collection.resolver.default_card_resolver", lambda: stub
    )

    result = runner.invoke(spoiler_sync.app, ["sync", "eoe"])
    assert result.exit_code == 0, result.output
    # Output shape: header + phases + summary with "New since last sync".
    assert "MTG Spoiler Sync" in result.output
    assert "New since last sync:" in result.output
    assert "Sol Ring" in result.output
    assert "Total cards in lake:" in result.output

    # Second run, same scrape -> nothing new (new-since-last-sync from the lake).
    result2 = runner.invoke(spoiler_sync.app, ["sync", "eoe"])
    assert result2.exit_code == 0, result2.output
    assert "New since last sync:" not in result2.output


def test_sync_requires_set_codes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SPOILER_SET_CODES", raising=False)
    result = runner.invoke(spoiler_sync.app, ["sync"])
    assert result.exit_code == 1
    assert "No set codes provided" in result.output


# --------------------------------------------------------------------------- #
# status / list — output shape matches today's rich tables.
# --------------------------------------------------------------------------- #


def test_status_output_shape(data_dir: Path) -> None:
    resolver = _StubResolver({"sol Ring": Card(name="Sol Ring", oracle_id="oid-sol")})
    _seed_lake(
        resolver,
        [
            {"slug": "solRing", "image_url": "i", "set_code": "eoe", "detail_url": "d"},
            {
                "slug": "mysteryCard",
                "image_url": "i",
                "set_code": "eoe",
                "detail_url": "d",
            },
        ],
    )
    result = runner.invoke(spoiler_sync.app, ["status"])
    assert result.exit_code == 0, result.output
    # The status table columns are preserved.
    assert "Spoiler Sync Status" in result.output
    assert "Set" in result.output
    assert "Total" in result.output
    assert "Confirmed" in result.output
    assert "Unconfirmed" in result.output
    assert "EOE" in result.output


def test_status_no_data(data_dir: Path) -> None:
    result = runner.invoke(spoiler_sync.app, ["status"])
    assert result.exit_code == 1
    assert "No spoiler data found" in result.output


def test_list_output_shape_and_new_filter(data_dir: Path) -> None:
    resolver = _StubResolver({"sol Ring": Card(name="Sol Ring", oracle_id="oid-sol")})
    _seed_lake(
        resolver,
        [
            {"slug": "solRing", "image_url": "i", "set_code": "eoe", "detail_url": "d"},
            {
                "slug": "mysteryCard",
                "image_url": "i",
                "set_code": "eoe",
                "detail_url": "d",
            },
        ],
    )
    result = runner.invoke(spoiler_sync.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "Spoiler Cards" in result.output
    assert "Sol Ring" in result.output

    # --new surfaces only unconfirmed (the mystery card), not the confirmed one.
    new_result = runner.invoke(spoiler_sync.app, ["list", "--new"])
    assert new_result.exit_code == 0, new_result.output
    assert "Sol Ring" not in new_result.output


def test_list_set_filter(data_dir: Path) -> None:
    resolver = _StubResolver({})
    _seed_lake(
        resolver,
        [
            {"slug": "a", "image_url": "i", "set_code": "eoe", "detail_url": "d"},
            {"slug": "b", "image_url": "i", "set_code": "tdm", "detail_url": "d"},
        ],
    )
    result = runner.invoke(spoiler_sync.app, ["list", "--set", "tdm"])
    assert result.exit_code == 0, result.output
    assert "TDM" in result.output
    assert "EOE" not in result.output
