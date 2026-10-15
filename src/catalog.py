"""
src/catalog.py
==============

商品数据访问层（只读）。

本模块把「数据从哪来」与「推荐链路怎么用」解耦：
    - Catalog.from_snapshot()  ← data/offline/ 内置快照（离线演示，默认）
    - Catalog.from_external()  ← 外部推荐系统 *发布制品*（只读文件，不 import 其代码）

对上层暴露与来源无关的只读查询接口。
"""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from config import settings


class Catalog:
    """商品目录 + 相似索引 + 个性化分数 + 热门池 + 购买历史。"""

    def __init__(
        self,
        catalog: dict[str, dict],
        similar: dict[str, list[str]],
        scores: dict[str, dict[str, float]],
        popular: dict,
        history: dict,
        users: dict,
        meta: dict,
        origin: str,
    ):
        self.catalog = catalog
        self.similar = similar
        self.scores = scores
        self.popular = popular
        self.history = history
        self.users = users
        self.meta = meta
        self.origin = origin
        self._popularity: dict[str, int] = popular.get("popularity", {}) or {}
        self._cat_top: dict[str, list[str]] = popular.get("by_category", {}) or {}
        self._global_top: list[str] = popular.get("global_top", []) or []
        self._categories = sorted({p.get("category", "其他") for p in catalog.values()})

    # ---------------- 构造 ----------------
    @classmethod
    def _load(cls, base: Path, origin: str) -> "Catalog":
        def _json(name: str, default):
            path = base / name
            if not path.exists():
                return default
            with path.open("r", encoding="utf-8-sig") as fh:
                return json.load(fh)

        return cls(
            catalog=_json("catalog.json", {}),
            similar=_json("similar.json", {}),
            scores=_json("scores.json", {}),
            popular=_json("popular.json", {"global_top": [], "by_category": {}, "popularity": {}}),
            history=_json("history.json", {}),
            users=_json("users.json", {}),
            meta=_json("meta.json", {}),
            origin=origin,
        )

    @classmethod
    def from_snapshot(cls, offline_dir: Path | str | None = None) -> "Catalog":
        base = Path(offline_dir) if offline_dir else settings.PATHS["offline"]
        return cls._load(base, origin=f"offline_snapshot:{base}")

    @classmethod
    def from_external(cls, root: Path | str) -> "Catalog":
        """读取外部推荐系统 *发布制品*（products.csv / app_data.json / price_map.json ...）。"""
        root = Path(root)
        interop = settings.RECOMMENDER_INTEROP

        def _json(rel: str, default):
            path = root / rel
            if not path.exists():
                return default
            with path.open("r", encoding="utf-8-sig") as fh:
                return json.load(fh)

        products_path = root / interop["products_csv"]
        app_data = _json(interop["app_data_json"], {})
        price_map = _json(interop["price_map_json"], {})
        history = _json(interop["user_history_json"], {})
        if not products_path.exists():
            raise FileNotFoundError(f"缺少发布制品: {products_path}")

        price_items = price_map.get("items", {})
        default_price = float(price_map.get("default_price", 0.0) or 0.0)
        catalog: dict[str, dict] = {}
        with products_path.open("r", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                code = str(row.get("StockCode", "")).strip()
                if not code:
                    continue
                rec = price_items.get(code, {})
                catalog[code] = {
                    "item_id": code,
                    "stock_code": code,
                    "title": str(row.get("Chinese_Description") or row.get("Description") or "").strip(),
                    "english_title": str(row.get("Description") or "").strip(),
                    "category": str(row.get("Product_Category") or "其他").strip() or "其他",
                    "price": round(float(rec.get("price", default_price)), 4),
                    "currency": price_map.get("currency", "GBP"),
                    "price_source": rec.get("source", "catalog_default"),
                    "transactions": int(rec.get("transactions", 0) or 0),
                }

        similar: dict[str, list[str]] = {}
        for item_id, neighbors in (app_data.get("similar") or {}).items():
            keep = [str(c) for c in neighbors if str(c) in catalog and str(c) != str(item_id)]
            if keep:
                similar[str(item_id)] = keep[:12]

        scores: dict[str, dict[str, float]] = {}
        submission = root / interop["submission_csv"]
        if submission.exists():
            with submission.open("r", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    user = str(row.get("user_id", "")).strip()
                    item = str(row.get("item_id", "")).strip()
                    if not user or item not in catalog:
                        continue
                    try:
                        scores.setdefault(user, {})[item] = round(float(row.get("score", 0.0) or 0.0), 6)
                    except ValueError:
                        continue
        for user, rows in (app_data.get("recommendations") or {}).items():
            bucket = scores.setdefault(str(user), {})
            for row in rows:
                item = str(row.get("item_id"))
                if item in catalog and item not in bucket:
                    bucket[item] = round(float(row.get("score", 0.0) or 0.0), 6)

        popularity: Counter[str] = Counter()
        for neighbors in similar.values():
            popularity.update(neighbors)
        global_top = [i for i, _ in popularity.most_common(400) if i in catalog]
        by_category: dict[str, list[str]] = {}
        for item in global_top:
            by_category.setdefault(catalog[item]["category"], []).append(item)

        popular = {
            "global_top": global_top,
            "by_category": {c: ids[:120] for c, ids in by_category.items()},
            "popularity": {i: int(n) for i, n in popularity.most_common(2000)},
        }
        users = {"users": app_data.get("users", {}), "model_version": app_data.get("model_version", "unknown")}
        meta = {
            "cutoff": price_map.get("cutoff"),
            "currency": price_map.get("currency", "GBP"),
            "symbol": price_map.get("symbol", "£"),
            "model_version": app_data.get("model_version", "unknown"),
            "categories": sorted({p["category"] for p in catalog.values()}),
        }
        return cls(catalog, similar, scores, popular, history, users, meta, origin=f"external_artifacts:{root}")

    def apply_override(self, item_id: str, override: dict[str, Any]) -> dict:
        """注入/覆盖一条商品记录（用于「商品字段缺失」等边界用例）。

        仅影响内存对象，不写回任何文件；调用方可先 deepcopy 目录再做实验。
        """
        key = str(item_id)
        base = dict(self.catalog.get(key, {"item_id": key, "stock_code": key}))
        base.update(override)
        self.catalog[key] = base
        return base

    def clone(self) -> "Catalog":
        """浅拷贝一份目录（用例之间互不污染）。"""
        import copy as _copy

        return Catalog(
            catalog=_copy.deepcopy(self.catalog), similar=_copy.deepcopy(self.similar),
            scores=_copy.deepcopy(self.scores), popular=_copy.deepcopy(self.popular),
            history=_copy.deepcopy(self.history), users=_copy.deepcopy(self.users),
            meta=_copy.deepcopy(self.meta), origin=self.origin + "+clone",
        )

    # ---------------- 只读查询 ----------------
    @property
    def model_version(self) -> str:
        return (self.meta.get("model_version") or self.users.get("model_version") or "unknown")

    @property
    def currency(self) -> str:
        return self.meta.get("currency", "GBP")

    @property
    def symbol(self) -> str:
        return self.meta.get("symbol", "£")

    def categories(self) -> list[str]:
        return list(self._categories)

    def size(self) -> int:
        return len(self.catalog)

    def product(self, item_id: str) -> dict | None:
        return self.catalog.get(str(item_id))

    def exists(self, item_id: str) -> bool:
        return str(item_id) in self.catalog

    def title(self, item_id: str) -> str:
        p = self.product(item_id) or {}
        return p.get("title") or p.get("english_title") or str(item_id)

    def category_of(self, item_id: str) -> str:
        return (self.product(item_id) or {}).get("category", "其他")

    def price_of(self, item_id: str) -> float | None:
        p = self.product(item_id) or {}
        value = p.get("price")
        return float(value) if value is not None else None

    def similar_of(self, item_id: str, k: int = 12) -> list[str]:
        return [i for i in self.similar.get(str(item_id), []) if i in self.catalog][:k]

    def user_scores(self, user_id: str) -> dict[str, float]:
        return dict(self.scores.get(str(user_id), {}))

    def has_user_scores(self, user_id: str) -> bool:
        return str(user_id) in self.scores

    def user_profile(self, user_id: str) -> dict:
        return dict((self.users.get("users") or {}).get(str(user_id), {}))

    def popularity(self, item_id: str) -> int:
        return int(self._popularity.get(str(item_id), 0))

    def global_top(self, k: int = 100) -> list[str]:
        if self._global_top:
            return [i for i in self._global_top[:k] if i in self.catalog]
        ranked = sorted(self.catalog, key=lambda i: -self.popularity(i))
        return ranked[:k]

    def category_top(self, category: str, k: int = 50) -> list[str]:
        ids = self._cat_top.get(category)
        if ids:
            return [i for i in ids[:k] if i in self.catalog]
        return [i for i in self.catalog.values() if i["category"] == category][:k]

    def purchased(self, user_id: str) -> set[str]:
        payload = self.history.get(str(user_id), {}) or {}
        out: set[str] = set()
        for row in payload.get("purchased", []) or []:
            out.add(str(row.get("item_id")))
        return out

    def frequent(self, user_id: str, limit: int = 6) -> list[dict]:
        payload = self.history.get(str(user_id), {}) or {}
        return list(payload.get("frequent", []) or [])[:limit]

    def history_of(self, user_id: str) -> dict:
        return dict(self.history.get(str(user_id), {}) or {})

    def describe(self) -> dict[str, Any]:
        return {
            "origin": self.origin,
            "model_version": self.model_version,
            "products": self.size(),
            "users_with_scores": len(self.scores),
            "categories": len(self._categories),
            "currency": self.currency,
        }
