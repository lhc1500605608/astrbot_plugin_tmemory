"""Tests for RRF (Reciprocal Rank Fusion) hybrid search behavior.

Covers:
- RRFSearchFusion.fuse() ordering and duplicate merge
- Vector-only, FTS-only, both-hit, and no-hit cases
- RRF k parameter influence on rank weighting
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hybrid_search import RRFSearchFusion


def _make_vec_item(doc_id: int):
    return {"id": doc_id, "score": 0.9, "distance": 0.1}


def _make_fts_item(doc_id: int):
    return {"id": doc_id, "score": -0.5, "rank": -0.5}


class TestRRFSearchFusion:
    """Unit tests for RRF fusion algorithm — no database required."""

    def test_fuse_basic_ordering(self):
        """Results are ordered by RRF score (rank position), ignoring raw scores."""
        fusion = RRFSearchFusion(k=60)
        vec = [_make_vec_item(10), _make_vec_item(20), _make_vec_item(30)]
        fts = [_make_fts_item(20), _make_fts_item(30), _make_fts_item(40)]

        result = fusion.fuse(vec, fts, top_k=5)

        # Doc 20 appears at rank 2 in vector and rank 1 in FTS => highest RRF
        # Doc 30 appears at rank 3 in vector and rank 2 in FTS => second highest
        # Doc 10 appears only at rank 1 in vector
        # Doc 40 appears only at rank 3 in FTS
        ids = [r["id"] for r in result]
        assert ids[0] == 20, f"Expected doc 20 first (both lists), got {ids}"
        assert ids[1] == 30, f"Expected doc 30 second, got {ids}"
        assert len(result) == 4

    def test_fuse_duplicate_merge_sources(self):
        """Duplicate memory IDs are merged with source metadata for debugging."""
        fusion = RRFSearchFusion(k=60)
        vec = [_make_vec_item(1)]
        fts = [_make_fts_item(1)]

        result = fusion.fuse(vec, fts, top_k=5)

        assert len(result) == 1
        assert result[0]["id"] == 1
        assert set(result[0]["sources"]) == {"vector", "fts"}
        # RRF score is sum of both contributions
        expected = 1.0 / 61 + 1.0 / 61
        assert result[0]["rrf_score"] == pytest.approx(expected, rel=1e-9)

    def test_fuse_vector_only(self):
        """Vector-only results are returned ranked by position."""
        fusion = RRFSearchFusion(k=60)
        vec = [_make_vec_item(5), _make_vec_item(3), _make_vec_item(1)]

        result = fusion.fuse(vec, [], top_k=3)

        assert [r["id"] for r in result] == [5, 3, 1]
        for r in result:
            assert r["sources"] == ["vector"]

    def test_fuse_fts_only(self):
        """FTS-only results are returned ranked by position."""
        fusion = RRFSearchFusion(k=60)
        fts = [_make_fts_item(100), _make_fts_item(200)]

        result = fusion.fuse([], fts, top_k=5)

        assert [r["id"] for r in result] == [100, 200]
        for r in result:
            assert r["sources"] == ["fts"]

    def test_fuse_both_empty(self):
        """Empty input lists return empty result — no-hit case."""
        fusion = RRFSearchFusion(k=60)
        result = fusion.fuse([], [], top_k=5)
        assert result == []

    def test_fuse_top_k_truncation(self):
        """Results are truncated to top_k."""
        fusion = RRFSearchFusion(k=60)
        vec = [_make_vec_item(i) for i in range(10)]
        fts = [_make_fts_item(i) for i in range(5, 15)]

        result = fusion.fuse(vec, fts, top_k=5)
        assert len(result) == 5

    def test_fuse_k_parameter_dampens_rank_influence(self):
        """Higher k reduces the influence of rank position differences."""
        low_k = RRFSearchFusion(k=1)
        high_k = RRFSearchFusion(k=1000)

        vec = [_make_vec_item(1), _make_vec_item(2)]
        # At k=1, rank1/(rank1+rank2) ratio is large
        # At k=1000, the ratio approaches 1.0 (ranks matter less)
        low_result = low_k.fuse(vec, [], top_k=2)
        high_result = high_k.fuse(vec, [], top_k=2)

        low_ratio = low_result[1]["rrf_score"] / low_result[0]["rrf_score"]
        high_ratio = high_result[1]["rrf_score"] / high_result[0]["rrf_score"]
        assert high_ratio > low_ratio, (
            f"Higher k should make scores more similar: "
            f"low_k ratio={low_ratio:.4f}, high_k ratio={high_ratio:.4f}"
        )

    def test_fuse_ignores_raw_scores(self):
        """RRF uses only rank position, not the raw score/distance values."""
        fusion = RRFSearchFusion(k=60)
        # Item with low raw score but high rank (position 1) beats item with
        # high raw score but low rank (position 2)
        vec = [
            {"id": 1, "score": 0.1, "distance": 10.0},
            {"id": 2, "score": 0.99, "distance": 0.01},
        ]

        result = fusion.fuse(vec, [], top_k=2)
        assert result[0]["id"] == 1  # rank 1 wins despite worse raw score

    def test_fuse_different_rank_positions_summed(self):
        """Same doc at different ranks in each list: RRF scores sum correctly."""
        fusion = RRFSearchFusion(k=60)
        vec = [_make_fts_item(42)]  # index 0 in vec, rank 1
        fts = [
            _make_fts_item(99),  # rank 1 in fts
            _make_fts_item(42),  # rank 2 in fts
        ]

        result = fusion.fuse(vec, fts, top_k=5)
        doc42 = next(r for r in result if r["id"] == 42)
        expected = 1.0 / 61 + 1.0 / 62
        assert doc42["rrf_score"] == pytest.approx(expected, rel=1e-9)
        assert set(doc42["sources"]) == {"vector", "fts"}
