"""
The pending store — in-flight bookkeeping.

Small, but it is what stops a paid result being paid for twice: without the
request body, a result collected after a timeout can be stored under its
activity_id and never matched to an identical future request.
"""

from __future__ import annotations

from pathlib import Path

from fortyguard_mcp.config import Settings
from fortyguard_mcp.store.pending import PendingStore


def store_at(tmp_path: Path) -> PendingStore:
    return PendingStore(Settings(api_key="x", data_dir=tmp_path))


def test_round_trip(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    body = {"polygon_aoi": {"type": "FeatureCollection"}, "granularity": 100}
    s.remember("act-1", "/v1/heatmap", body)

    got = s.recall("act-1")
    assert got is not None
    assert got.endpoint == "/v1/heatmap"
    assert got.request_body == body
    assert got.submitted_at


def test_forget_is_idempotent(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.remember("act-1", "/v1/heatmap", {})
    s.forget("act-1")
    s.forget("act-1")
    assert s.recall("act-1") is None


def test_unknown_id_is_a_miss_not_an_error(tmp_path: Path) -> None:
    assert store_at(tmp_path).recall("never-seen") is None


def test_corrupt_record_is_a_miss_not_a_crash(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.remember("act-1", "/v1/heatmap", {})
    next(s.root.glob("*.json")).write_text("{not json", encoding="utf-8")
    assert s.recall("act-1") is None


def test_long_ids_cannot_collide(tmp_path: Path) -> None:
    """
    Regression. The first version of `_path` truncated to 128 characters with no
    hash, so two long ids sharing a prefix mapped to one file and one request
    body silently overwrote the other — which would then key the wrong result
    into the paid archive. It now uses the result store's `safe_filename`,
    which appends a digest whenever sanitising or truncation changed anything.
    """
    s = store_at(tmp_path)
    a = "x" * 200 + "-alpha"
    b = "x" * 200 + "-beta"
    s.remember(a, "/v1/heatmap", {"which": "a"})
    s.remember(b, "/v1/heatmap", {"which": "b"})

    assert s.recall(a) is not None and s.recall(a).request_body == {"which": "a"}
    assert s.recall(b) is not None and s.recall(b).request_body == {"which": "b"}
    assert s.count() == 2


def test_traversal_characters_never_escape_the_directory(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    s.remember("../../etc/passwd", "/v1/heatmap", {"x": 1})
    for p in s.root.glob("*.json"):
        assert p.parent == s.root
    assert s.recall("../../etc/passwd") is not None


def test_count_reflects_what_is_outstanding(tmp_path: Path) -> None:
    s = store_at(tmp_path)
    assert s.count() == 0
    s.remember("a", "/v1/heatmap", {})
    s.remember("b", "/v1/satellite", {})
    assert s.count() == 2
    s.forget("a")
    assert s.count() == 1
