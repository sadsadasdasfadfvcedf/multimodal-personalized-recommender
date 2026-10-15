"""
src/adapters/recommender_adapter.py
===================================

「推荐系统 v146」适配器。

三种策略（按 config.RECOMMENDER_ADAPTER["strategy_order"] 依次尝试，失败自动降级）：

    http            调用推荐系统对外 HTTP 接口（需其进程在跑，且提供演示账号环境变量）
    local_artifacts 读取推荐系统 *发布的只读制品文件*（products.csv / app_data.json / ...）
    offline_snapshot 使用本项目内置 data/offline/ 快照（默认，完全离线）

重要：本适配器只通过「公开接口/发布制品」交互，不 import 推荐系统的 Python 模块，
      不读取其内部变量 —— 满足阶段1 的接口边界要求。
"""

from __future__ import annotations

import os
from typing import Any

from config import settings

from ..catalog import Catalog
from ..errors import ErrorCode, Result, fail, ok
from .base import AdapterBase

CFG = settings.RECOMMENDER_ADAPTER
SOURCE_LABELS = CFG["sources"]


class RecommenderAdapter(AdapterBase):
    name = "recommender_adapter"

    def __init__(self, mode: str | None = None, *, strategy_order: list[str] | None = None) -> None:
        requested = (mode or settings.RUNTIME["mode"]).lower()
        super().__init__(requested_mode=requested)
        self.strategy_order = strategy_order or list(CFG["strategy_order"])
        if requested == "offline":
            self.strategy_order = ["offline_snapshot"]
        self.catalog: Catalog | None = None
        self._http_session: Any = None
        self.http_base = CFG["http"]["base_url"].rstrip("/")
        self._resolve()

    # ---------------- 策略解析 ----------------
    def _resolve(self) -> None:
        for strategy in self.strategy_order:
            try:
                if strategy == "http" and self._try_http():
                    return
                if strategy == "local_artifacts" and self._try_local():
                    return
                if strategy == "offline_snapshot" and self._try_snapshot():
                    return
            except Exception as exc:
                self.record_degradation(
                    level="L1",
                    reason=f"{strategy} 策略异常: {type(exc).__name__}: {exc}",
                    from_mode=strategy,
                    to_mode="next",
                )
        # 兜底：内置快照
        self.active_mode = "offline_snapshot"
        self.record_degradation(
            level="L2", reason="所有策略均不可用，强制使用内置快照", from_mode="*", to_mode="offline_snapshot"
        )

    def _try_http(self) -> bool:
        if not CFG["http"]["enabled"]:
            self.record_degradation(level="L1", reason="http 策略被配置禁用", from_mode="http", to_mode="next")
            return False
        user = os.getenv(CFG["http"]["username_env"])
        password = os.getenv(CFG["http"]["password_env"])
        if not user or not password:
            self.record_degradation(
                level="L1",
                reason=f"缺少演示账号环境变量 {CFG['http']['username_env']}/{CFG['http']['password_env']}，跳过 HTTP 策略",
                from_mode="http",
                to_mode="next",
            )
            return False
        try:
            import requests  # noqa: F401
        except ImportError:
            self.record_degradation(level="L1", reason="未安装 requests", from_mode="http", to_mode="next")
            return False
        if not self._http_login(user, password):
            return False
        # HTTP 模式下仍需一份本地目录用于类目/价格/相似补全
        self.catalog = self._build_catalog("offline_snapshot")
        self.active_mode = "http"
        return True

    def _http_login(self, user: str, password: str) -> bool:
        import requests

        timeout = CFG["http"]["timeout_s"]
        keys = CFG["http"].get("login_payload_keys", {"user": "user_id", "password": "password"})
        payload = {keys.get("user", "user_id"): user, keys.get("password", "password"): password}
        try:
            session = requests.Session()
            resp = session.post(f"{self.http_base}{CFG['http']['endpoints']['login']}", json=payload, timeout=timeout)
            if resp.status_code != 200:
                detail = ""
                try:
                    detail = resp.json().get("detail", "")
                except Exception:
                    detail = (resp.text or "").strip()[:120]
                raise RuntimeError(f"login HTTP {resp.status_code} {detail}".strip())
            self._http_session = session
            return True
        except Exception as exc:
            self.record_degradation(
                level="L1",
                reason=f"推荐系统 HTTP 不可用: {type(exc).__name__}: {exc}",
                from_mode="http",
                to_mode="next",
            )
            self._http_session = None
            return False

    def _try_local(self) -> bool:
        if not CFG["local_artifacts"]["enabled"]:
            self.record_degradation(
                level="L1", reason="local_artifacts 策略被禁用", from_mode="local_artifacts", to_mode="next"
            )
            return False
        root = settings.RECOMMENDER_ROOT
        if root is None:
            self.record_degradation(
                level="L1", reason="未找到推荐系统根目录", from_mode="local_artifacts", to_mode="next"
            )
            return False
        try:
            self.catalog = Catalog.from_external(root)
        except Exception as exc:
            self.record_degradation(
                level="L1", reason=f"读取发布制品失败: {exc}", from_mode="local_artifacts", to_mode="next"
            )
            return False
        self.active_mode = "local_artifacts"
        return True

    def _try_snapshot(self) -> bool:
        if not CFG["offline_snapshot"]["enabled"]:
            return False
        base = settings.PATHS["offline"]
        if not (base / "catalog.json").exists():
            self.record_degradation(
                level="L2",
                reason="内置快照缺失，请先运行 scripts/build_offline_snapshot.py",
                from_mode="offline_snapshot",
                to_mode="none",
            )
            return False
        self.catalog = self._build_catalog("offline_snapshot")
        self.active_mode = "offline_snapshot"
        return True

    def _build_catalog(self, which: str) -> Catalog:
        if which == "offline_snapshot":
            return Catalog.from_snapshot()
        root = settings.RECOMMENDER_ROOT
        assert root is not None
        return Catalog.from_external(root)

    # ---------------- 健康检查 ----------------
    def health(self) -> Result:
        return ok(
            {
                "adapter": self.name,
                "active_mode": self.active_mode,
                "http_base": self.http_base,
                "http_available": self._http_session is not None,
                "catalog": self.catalog.describe() if self.catalog else None,
                "degradations": list(self.degradations),
            }
        )

    # ---------------- 原语：候选池 ----------------
    def _require_catalog(self) -> Catalog:
        if self.catalog is None:
            self.catalog = Catalog.from_snapshot()
        return self.catalog

    def _label(self, key: str) -> str:
        return SOURCE_LABELS.get(key, key)

    def personalized_candidates(self, user_id: str, k: int = 80) -> Result:
        """个性化精排候选（推荐系统 v146 分数）。"""

        def run() -> Result:
            if self.active_mode == "http":
                http_result = self._http_recommendations(k)
                if http_result.ok and http_result.data:
                    return http_result
            cat = self._require_catalog()
            scores = cat.user_scores(user_id)
            items = [i for i in sorted(scores, key=lambda x: -scores[x]) if cat.exists(i)][:k]
            if not items:
                return fail(
                    ErrorCode.NO_RESULT,
                    f"用户 {user_id} 在推荐系统中没有个性化候选",
                    field="user_id",
                    user_id=user_id,
                    mode=self.active_mode,
                )
            return ok(
                [
                    {
                        "item_id": i,
                        "score": float(scores[i]),
                        "source_key": "personalized",
                        "source_label": self._label("personalized"),
                        "detail": {"model_version": cat.model_version, "mode": self.active_mode},
                    }
                    for i in items
                ]
            )

        return self.guard("personalized_candidates", run)

    def _http_recommendations(self, k: int) -> Result:

        assert self._http_session is not None
        timeout = CFG["http"]["timeout_s"]
        items: list[dict] = []
        offset = 0
        try:
            while len(items) < k and offset < 200:
                resp = self._http_session.get(
                    f"{self.http_base}{CFG['http']['endpoints']['recommendations']}",
                    params={"offset": offset, "limit": min(40, k - len(items))},
                    timeout=timeout,
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"recommendations HTTP {resp.status_code}")
                payload = resp.json()
                chunk = payload.get("items", [])
                if not chunk:
                    break
                for row in chunk:
                    if row.get("score") is not None:
                        items.append(
                            {
                                "item_id": str(row["item_id"]),
                                "score": float(row["score"]),
                                "source_key": "personalized",
                                "source_label": self._label("personalized"),
                                "detail": {"reason": row.get("reason"), "http": True},
                            }
                        )
                offset = payload.get("next_offset", offset + len(chunk))
                if not payload.get("has_more"):
                    break
        except Exception as exc:
            self.record_degradation(
                level="L1",
                reason=f"HTTP 召回失败: {type(exc).__name__}: {exc}",
                from_mode="http",
                to_mode="local_artifacts",
            )
            return fail(ErrorCode.UPSTREAM_UNAVAILABLE, f"HTTP 召回失败: {exc}")
        if not items:
            return fail(ErrorCode.NO_RESULT, "HTTP 推荐结果为空")
        return ok(items[:k])

    def similar_candidates(self, seed_item_ids: list[str], k: int = 40, exclude: set[str] | None = None) -> Result:
        """相似商品召回（以若干种子商品的相似索引扩散）。"""
        exclude = exclude or set()

        def run() -> Result:
            cat = self._require_catalog()
            counter: dict[str, int] = {}
            seed_of: dict[str, list[str]] = {}
            for seed in seed_item_ids:
                for cand in cat.similar_of(seed, k=12):
                    if cand in exclude or cand in seed_item_ids:
                        continue
                    counter[cand] = counter.get(cand, 0) + 1
                    seed_of.setdefault(cand, []).append(seed)
            if not counter:
                return fail(ErrorCode.NO_RESULT, "相似召回为空", field="seed_item_ids", seeds=list(seed_item_ids))
            ranked = sorted(counter, key=lambda i: (-counter[i], -cat.popularity(i)))[:k]
            max_hit = max(counter.values())
            return ok(
                [
                    {
                        "item_id": i,
                        "score": round(counter[i] / max_hit, 6),
                        "source_key": "similar",
                        "source_label": self._label("similar"),
                        "detail": {"shared_seeds": seed_of.get(i, [])[:3], "hit_count": counter[i]},
                    }
                    for i in ranked
                ]
            )

        return self.guard("similar_candidates", run)

    def popular_candidates(self, k: int = 60, exclude: set[str] | None = None) -> Result:
        """全局热门兜底召回。"""
        exclude = exclude or set()

        def run() -> Result:
            cat = self._require_catalog()
            top = [i for i in cat.global_top(max(k * 3, 200)) if i not in exclude][:k]
            if not top:
                return fail(ErrorCode.NO_RESULT, "热门池为空")
            peak = max(cat.popularity(i) for i in top) or 1
            return ok(
                [
                    {
                        "item_id": i,
                        "score": round(cat.popularity(i) / peak, 6),
                        "source_key": "popular",
                        "source_label": self._label("popular"),
                        "detail": {"popularity": cat.popularity(i), "fallback": True},
                    }
                    for i in top
                ]
            )

        return self.guard("popular_candidates", run)

    def category_candidates(self, category: str, k: int = 40, exclude: set[str] | None = None) -> Result:
        """品类热门召回。"""
        exclude = exclude or set()

        def run() -> Result:
            cat = self._require_catalog()
            ids = [i for i in cat.category_top(category, k * 3) if i not in exclude][:k]
            if not ids:
                return fail(ErrorCode.NO_RESULT, f"品类 {category} 无候选", field="category")
            peak = max(cat.popularity(i) for i in ids) or 1
            return ok(
                [
                    {
                        "item_id": i,
                        "score": round(cat.popularity(i) / peak, 6),
                        "source_key": "category_hot",
                        "source_label": self._label("category_hot"),
                        "detail": {"category": category, "popularity": cat.popularity(i)},
                    }
                    for i in ids
                ]
            )

        return self.guard("category_candidates", run)

    def history_candidates(self, user_id: str, k: int = 30, exclude: set[str] | None = None) -> Result:
        """基于历史购买的相似召回。"""
        exclude = exclude or set()

        def run() -> Result:
            cat = self._require_catalog()
            payload = cat.history_of(user_id)
            seeds = [str(r.get("item_id")) for r in (payload.get("frequent") or [])][:5]
            seeds += [str(r.get("item_id")) for r in (payload.get("purchased") or [])][:10]
            seeds = [s for s in seeds if cat.exists(s)]
            if not seeds:
                return fail(ErrorCode.NO_RESULT, f"用户 {user_id} 无可用购买历史", field="user_id")
            counter: dict[str, int] = {}
            for seed in seeds:
                for cand in cat.similar_of(seed, k=12):
                    if cand in exclude:
                        continue
                    counter[cand] = counter.get(cand, 0) + 1
            ranked = sorted(counter, key=lambda i: -counter[i])[:k]
            peak = max(counter.values()) if counter else 1
            return ok(
                [
                    {
                        "item_id": i,
                        "score": round(counter[i] / peak, 6),
                        "source_key": "history",
                        "source_label": self._label("history"),
                        "detail": {"from_history": True, "hit_count": counter[i]},
                    }
                    for i in ranked
                ]
            )

        return self.guard("history_candidates", run)

    # ---------------- 商品详情 ----------------
    def product_detail(self, item_id: str) -> Result:
        def run() -> Result:
            if self.active_mode == "http":
                http_result = self._http_product(item_id)
                if http_result.ok:
                    return http_result
            cat = self._require_catalog()
            product = cat.product(item_id)
            if not product:
                return fail(ErrorCode.ITEM_NOT_FOUND, f"商品 {item_id} 不在目录中", field="item_id")
            return ok(
                {
                    "product": dict(product),
                    "similar": cat.similar_of(item_id, k=12),
                    "origin": cat.origin,
                }
            )

        return self.guard("product_detail", run)

    def _http_product(self, item_id: str) -> Result:
        assert self._http_session is not None
        timeout = CFG["http"]["timeout_s"]
        try:
            resp = self._http_session.get(
                f"{self.http_base}{CFG['http']['endpoints']['product'].format(item_id=item_id)}", timeout=timeout
            )
            if resp.status_code != 200:
                raise RuntimeError(f"product HTTP {resp.status_code}")
            payload = resp.json()
            product = payload.get("product") or {}
            return ok({"product": product, "similar": payload.get("similar", []), "origin": "http"})
        except Exception as exc:
            self.record_degradation(
                level="L2", reason=f"HTTP 商品详情失败: {exc}", from_mode="http", to_mode="local_artifacts"
            )
            return fail(ErrorCode.UPSTREAM_UNAVAILABLE, f"HTTP 商品详情失败: {exc}")

    def metadata(self) -> dict[str, Any]:
        cat = self._require_catalog()
        return {"mode": self.active_mode, **cat.describe()}
