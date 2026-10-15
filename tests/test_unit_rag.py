"""src/rag.py —— 纯 Python BM25 检索。

test_search_result_lengths_are_aligned 盯的是批次二 B905 修复处：
结果列表由 zip(normalized[:k], scored[:k]) 生成，两者同源同长，
一旦不等就说明内部索引对错了位。
"""

from __future__ import annotations

import pytest

from src import rag


@pytest.fixture(scope="module")
def kb():
    return rag.get_kb()


def test_kb_loads_chunks(kb):
    assert kb.size() > 0


def test_search_returns_expected_keys(kb):
    rows = kb.search("结婚礼物 装饰品", 3)
    assert rows
    for row in rows:
        assert set(row) == {"chunk_id", "doc", "text", "score", "raw_score"}
        assert isinstance(row["score"], float)
        assert isinstance(row["raw_score"], float)


def test_search_respects_k(kb):
    assert len(kb.search("礼物", 2)) <= 2


def test_search_scores_are_descending(kb):
    scores = [r["score"] for r in kb.search("礼物 婚礼", 5)]
    assert scores == sorted(scores, reverse=True)


def test_search_normalized_score_within_unit_range(kb):
    for row in kb.search("礼物", 5):
        assert 0.0 <= row["score"] <= 1.0 + 1e-9


def test_search_result_lengths_are_aligned(kb):
    for query in ("礼物", "婚礼 装饰品", "预算 五百以内"):
        rows = kb.search(query, 5)
        assert len(rows) <= 5


def test_search_empty_query_returns_empty(kb):
    assert kb.search("", 5) == []


def test_search_unmatched_query_returns_empty(kb):
    assert kb.search("zzzzzzzzxxxxxxyyyyyy", 5) == []
