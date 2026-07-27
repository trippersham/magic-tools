"""OFFLINE tests for the per-source pullers (parse/land + fail-open + safety).

No network: HTTP is monkeypatched to return canned payloads. Each puller's
parse/land is asserted by reading the landed Parquet back through the store. The
oracle_tags fail-open path is proven by making the mocked fetch RAISE and
asserting the bundled snapshot lands. The Airtable safety property (GET-only) is
proven by (a) a unit test that the request wrapper rejects POST/PATCH/DELETE and
(b) a mocked list-records pull that lands rows while only ever issuing GET.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from pipeline import store
from pipeline.ingest import airtable, oracle_tags, scryfall_bulk, spellbook


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "data"
    monkeypatch.setenv("MAKE_MAGIC_DATA_DIR", str(root))
    return root


def _rows(layer: str, name: str) -> list[tuple[Any, ...]]:
    with store.connect() as conn:
        return store.read_parquet(conn, layer, name).order("1").fetchall()


# --------------------------------------------------------------------------- #
# oracle_tags: parse/land + watermark advance + fail-open to snapshot
# --------------------------------------------------------------------------- #

_TAGS_PAYLOAD = [
    {
        "id": "t1",
        "label": "removal",
        "slug": "removal",
        "type": "oracle",
        "parent_ids": [],
        "child_ids": ["t2"],
        "taggings": [],
    },
    {
        "id": "t2",
        "label": "sweeper",
        "slug": "sweeper",
        "type": "oracle",
        "parent_ids": ["t1"],
        "child_ids": [],
        "taggings": [{"oracle_id": "oid-a", "weight": "median"}],
    },
]


def test_oracle_tags_parse_and_land(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_fetch(client: httpx.Client) -> tuple[list[dict[str, Any]], str]:
        return _TAGS_PAYLOAD, "2026-07-26T21:00:00+00:00"

    monkeypatch.setattr(oracle_tags, "_fetch_remote", fake_fetch)
    path = oracle_tags.run(client=httpx.Client())

    assert path.exists()
    assert store.table_exists("raw", "oracle_tags")
    with store.connect() as conn:
        n = (
            store.read_parquet(conn, "raw", "oracle_tags")
            .aggregate("count(*)")
            .fetchone()[0]
        )
    assert n == 2

    # watermark advanced
    from pipeline.ingest._common import Watermark

    assert Watermark.load().get("oracle_tags") == "2026-07-26T21:00:00+00:00"


def test_oracle_tags_skips_when_not_newer(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pipeline.ingest._common import Watermark

    wm = Watermark.load()
    wm.set("oracle_tags", "2026-07-26T21:00:00+00:00")
    wm.save()

    calls = {"land": 0}
    real_land = oracle_tags._land

    def counting_land(tags: list[dict[str, Any]]) -> Path:
        calls["land"] += 1
        return real_land(tags)

    # Same updated_at as the stored watermark -> not newer -> skip land.
    def fake_fetch(client: httpx.Client) -> tuple[list[dict[str, Any]], str]:
        return _TAGS_PAYLOAD, "2026-07-26T21:00:00+00:00"

    monkeypatch.setattr(oracle_tags, "_fetch_remote", fake_fetch)
    monkeypatch.setattr(oracle_tags, "_land", counting_land)

    # First land a table so the skip branch can early-return it.
    real_land(_TAGS_PAYLOAD)
    oracle_tags.run(client=httpx.Client())
    assert calls["land"] == 0  # skipped — not newer


def test_oracle_tags_fail_open_to_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(client: httpx.Client) -> tuple[list[dict[str, Any]], str]:
        raise httpx.ConnectError("network down")

    monkeypatch.setattr(oracle_tags, "_fetch_remote", boom)
    # Must NOT raise — falls back to the bundled snapshot.
    path = oracle_tags.run(client=httpx.Client())
    assert path.exists()
    assert store.table_exists("raw", "oracle_tags")
    with store.connect() as conn:
        n = (
            store.read_parquet(conn, "raw", "oracle_tags")
            .aggregate("count(*)")
            .fetchone()[0]
        )
    # The bundled snapshot has the full 4,499-tag DAG.
    assert n == 4499


# --------------------------------------------------------------------------- #
# spellbook: parse/land + fail-open
# --------------------------------------------------------------------------- #

_COMBOS_PAYLOAD = [
    {"id": "c1", "identity": "U", "status": "OK"},
    {"id": "c2", "identity": "BG", "status": "OK"},
    {"id": "c1", "identity": "U", "status": "OK"},  # duplicate id -> deduped
]


def test_spellbook_parse_and_land_dedupes(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(spellbook, "_remote_watermark", lambda c: "etag-1")
    monkeypatch.setattr(spellbook, "_fetch_remote", lambda c, m: _COMBOS_PAYLOAD)

    path = spellbook.run(client=httpx.Client())
    assert path.exists()
    with store.connect() as conn:
        n = (
            store.read_parquet(conn, "raw", "combos")
            .aggregate("count(*)")
            .fetchone()[0]
        )
    assert n == 2  # c1 deduped


def test_spellbook_fail_open_to_snapshot(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(client: httpx.Client) -> str | None:
        raise httpx.ConnectError("down")

    monkeypatch.setattr(spellbook, "_remote_watermark", boom)
    path = spellbook.run(client=httpx.Client())
    assert path.exists()
    with store.connect() as conn:
        n = (
            store.read_parquet(conn, "raw", "combos")
            .aggregate("count(*)")
            .fetchone()[0]
        )
    assert n == 2000  # bundled snapshot size


# --------------------------------------------------------------------------- #
# scryfall_bulk: streaming parse + bounded cap + projection
# --------------------------------------------------------------------------- #


def test_scryfall_bulk_streams_and_projects(
    data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cards = [
        {
            "oracle_id": "o1",
            "name": "Lightning Bolt",
            "cmc": 1.0,
            "type_line": "Instant",
            "colors": ["R"],
            "color_identity": ["R"],
            "keywords": [],
            "extra": "dropped",
        },
        {
            "oracle_id": "o2",
            "name": "Sol Ring",
            "cmc": 1.0,
            "type_line": "Artifact",
            "colors": [],
            "color_identity": [],
            "produced_mana": ["C"],
            "keywords": [],
        },
    ]

    monkeypatch.setattr(
        scryfall_bulk,
        "_fetch_meta",
        lambda c: ("http://x/uri", "2026-07-26T00:00:00+00:00"),
    )
    monkeypatch.setattr(
        scryfall_bulk,
        "_stream_cards",
        lambda c, uri, cap: iter(cards[:cap] if cap else cards),
    )

    path = scryfall_bulk.run(client=httpx.Client(), max_cards=2)
    assert path.exists()
    with store.connect() as conn:
        rel = store.read_parquet(conn, "raw", "oracle_cards")
        n = rel.aggregate("count(*)").fetchone()[0]
        cols = rel.columns
    assert n == 2
    assert "extra" not in cols  # projected away
    assert "oracle_id" in cols and "produced_mana" in cols


def test_scryfall_bulk_stream_decoder_parses_array() -> None:
    # Exercise the real streaming decoder over a chunked JSON array.
    body = '[{"oracle_id":"a","name":"A"},{"oracle_id":"b","name":"B"},{"oracle_id":"c","name":"C"}]'

    class FakeResp:
        def raise_for_status(self) -> None: ...
        def iter_text(self):
            # feed the body in awkward 7-char chunks to test buffering
            for i in range(0, len(body), 7):
                yield body[i : i + 7]

    class FakeStream:
        def __enter__(self):
            return FakeResp()

        def __exit__(self, *a):
            return False

    class FakeClient:
        def stream(self, *a, **k):
            return FakeStream()

    out = list(scryfall_bulk._stream_cards(FakeClient(), "uri", max_cards=2))  # type: ignore[arg-type]
    assert [c["oracle_id"] for c in out] == ["a", "b"]  # capped at 2


# --------------------------------------------------------------------------- #
# Airtable safety: GET-only guard + mocked pull lands rows with no writes
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("method", ["POST", "PATCH", "PUT", "DELETE", "post", "patch"])
def test_airtable_client_rejects_non_get(method: str) -> None:
    client = airtable.GetOnlyClient("fake-token", _client=httpx.Client())
    with pytest.raises(airtable.NonGetMethodError):
        client.request(method, "https://api.airtable.com/v0/x/y")


def test_airtable_get_is_allowed_and_routes_through_guard() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        return httpx.Response(200, json={"records": []})

    transport = httpx.MockTransport(handler)
    client = airtable.GetOnlyClient("tok", _client=httpx.Client(transport=transport))
    resp = client.get("https://api.airtable.com/v0/base/tbl")
    assert resp.status_code == 200
    assert seen["method"] == "GET"


def test_airtable_pull_lands_rows_and_only_issues_get(
    data_dir: Path,
) -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        return httpx.Response(
            200,
            json={
                "records": [
                    {
                        "id": "rec1",
                        "createdTime": "2026-01-01T00:00:00Z",
                        "fields": {"fldName": "Sol Ring", "fldLinks": ["recX", "recY"]},
                    },
                    {
                        "id": "rec2",
                        "createdTime": "2026-01-02T00:00:00Z",
                        "fields": {"fldName": "Lightning Bolt"},
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    client = airtable.GetOnlyClient("tok", _client=httpx.Client(transport=transport))

    path = airtable.run_table("decks", client=client, force=True)
    assert path.exists()
    with store.connect() as conn:
        n = (
            store.read_parquet(conn, "raw", "airtable_decks")
            .aggregate("count(*)")
            .fetchone()[0]
        )
    assert n == 2
    # THE PROOF: every request issued was a GET.
    assert methods and all(m == "GET" for m in methods)


def test_airtable_run_requires_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AIRTABLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="AIRTABLE_API_KEY"):
        airtable.run()
