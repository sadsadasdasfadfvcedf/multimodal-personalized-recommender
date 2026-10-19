"""视觉相似：颜色直方图 + dHash 的排序与缓存回归。"""

from __future__ import annotations

import pytest

from src.visual_similarity import VisualSimilarityIndex, compute_signature, similarity


def _image_bytes(rgb: tuple[int, int, int], square: tuple[int, int, int] | None = None) -> bytes:
    pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    import cv2 as _cv2

    img = np.zeros((32, 32, 3), dtype=np.uint8)
    img[:, :] = rgb
    if square is not None:
        img[8:24, 8:24] = square
    ok, buf = _cv2.imencode(".png", img)
    assert ok
    return bytes(buf)


def test_signature_shape_and_self_similarity():
    img = _image_bytes((30, 30, 220))
    sig = compute_signature(img)
    assert sig is not None
    assert len(sig["hist"]) == 256
    assert 0 <= int(sig["hash"]) < (1 << 64)
    assert similarity(sig, sig) == pytest.approx(1.0)


def test_same_color_is_more_similar_than_different_color():
    red = compute_signature(_image_bytes((30, 30, 220)))
    red_square = compute_signature(_image_bytes((30, 30, 220), square=(240, 240, 240)))
    blue = compute_signature(_image_bytes((220, 30, 30)))
    assert red and red_square and blue
    assert similarity(red, red_square) > similarity(red, blue)


def test_rank_prefers_visually_close_candidate_and_caches(tmp_path):
    red = _image_bytes((30, 30, 220))
    red_square = _image_bytes((30, 30, 220), square=(240, 240, 240))
    blue = _image_bytes((220, 30, 30))
    cache = tmp_path / "visual_signatures.json"
    index = VisualSimilarityIndex(cache)
    ranked = index.rank("target", red, [("blue", blue), ("red_square", red_square)], k=2)
    assert [row["item_id"] for row in ranked] == ["red_square", "blue"]
    assert ranked[0]["similarity"] > ranked[1]["similarity"]
    assert cache.is_file()

    index2 = VisualSimilarityIndex(cache)
    assert "target" in index2._load()
