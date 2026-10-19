"""
src/visual_similarity.py
========================

轻量「以图找相似」：用商品图片的颜色直方图 + 差值哈希（dHash）做视觉相似度。

为什么不用 CLIP/VBPR？
----------------------
GitHub 上多模态推荐项目（VBPR、IDvs.MoRec、Hysia）大多依赖 CLIP/ResNet 预训练
权重。本项目要保证**离线可跑、零新增重型依赖**，因此先提供一条可解释的轻量视觉
通道：同品类热门候选 + 颜色/纹理相似度。它不替代个性化精排，只作为“外观相似”
的补充入口；后续可把 ``signature`` 换成 CLIP embedding 而不改上层接口。

缓存
----
签名缓存在 ``artifacts/visual_signatures.json``；artifacts 不进提交包。
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

CACHE_VERSION = 1


def _decode(image_bytes: bytes):
    import cv2
    import numpy as np

    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def compute_signature(image_bytes: bytes) -> dict[str, Any] | None:
    """返回 {sha, hist[256], hash}；无法解码时返回 None。"""
    try:
        import cv2
    except Exception:
        return None
    img = _decode(image_bytes)
    if img is None:
        return None
    img = cv2.resize(img, (64, 64), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1, 2], None, [8, 8, 4], [0, 180, 0, 256, 0, 256])
    hist = cv2.normalize(hist, hist).flatten()
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bool(bit))
    return {
        "sha": hashlib.sha256(image_bytes).hexdigest(),
        "hist": [round(float(x), 6) for x in hist],
        "hash": value,
    }


def _cosine(a: list[float], b: list[float]) -> float:
    import numpy as np

    va = np.asarray(a, dtype=float)
    vb = np.asarray(b, dtype=float)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    return float(np.dot(va, vb) / denom) if denom > 0 else 0.0


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    """0~1；颜色直方图 0.75 + dHash 0.25。"""
    hist = _cosine(a.get("hist") or [], b.get("hist") or [])
    hamming = 1.0 - ((int(a.get("hash", 0)) ^ int(b.get("hash", 0))).bit_count() / 64.0)
    return max(0.0, min(1.0, 0.75 * hist + 0.25 * hamming))


class VisualSimilarityIndex:
    """按 item_id 缓存图片签名，并对候选集排序。"""

    def __init__(self, cache_path: str | Path | None = None) -> None:
        self.cache_path = Path(cache_path) if cache_path else Path("artifacts") / "visual_signatures.json"
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if data.get("version") == CACHE_VERSION:
                return dict(data.get("items") or {})
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": CACHE_VERSION, "items": self._cache}
        self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def signature(self, item_id: str, image_bytes: bytes) -> dict[str, Any] | None:
        sha = hashlib.sha256(image_bytes).hexdigest()
        item_id = str(item_id)
        with self._lock:
            cached = self._cache.get(item_id)
        if cached and cached.get("sha") == sha:
            return cached
        sig = compute_signature(image_bytes)
        if sig is not None:
            with self._lock:
                self._cache[item_id] = sig
        return sig

    def rank(
        self,
        target_id: str,
        target_bytes: bytes,
        candidates: list[tuple[str, bytes]],
        k: int = 8,
    ) -> list[dict[str, Any]]:
        target = self.signature(target_id, target_bytes)
        if target is None:
            return []
        scored: list[tuple[str, float]] = []
        for cid, data in candidates:
            sig = self.signature(cid, data)
            if sig is not None:
                scored.append((str(cid), similarity(target, sig)))
        scored.sort(key=lambda item: item[1], reverse=True)
        with self._lock:
            self._save()
        return [{"item_id": cid, "similarity": round(score, 4)} for cid, score in scored[: max(1, k)]]
