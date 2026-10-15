"""
src/recall.py
=============

多路召回与候选合并（阶段3）。

多路来源（config.RECALL_CONFIG["sources_enabled"]）:
    personalized  推荐系统 v146 个性化精排
    similar       相似商品召回（以会话焦点/参照商品为种子）
    category_hot  品类热门召回
    history       基于历史购买的相似召回
    popular       全局热门兜底

合并规则：
    同一 item 由多路命中 → 合并来源、取各来源归一化分数最大值，并保留全部证据；
    合并后按 merge_top_k 截断。每条候选都保留 sources/score/evidence，保证可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import settings

from .adapters.base import AdapterBase
from .catalog import Catalog
from .contracts import RecommendationRequest
from .errors import Result, ok
from .session import SessionContext
from .utils import minmax_normalize

CFG = settings.RECALL_CONFIG
SOURCE_LABELS = settings.RECOMMENDER_ADAPTER["sources"]


@dataclass
class Candidate:
    item_id: str
    sources: list[str] = field(default_factory=list)  # 来源标签（可读）
    source_keys: list[str] = field(default_factory=list)  # 来源键（机器可读）
    raw_scores: dict[str, float] = field(default_factory=dict)
    norm_scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, catalog: Catalog | None = None) -> dict:
        product = catalog.product(self.item_id) if catalog else None
        return {
            "item_id": self.item_id,
            "title": (product or {}).get("title", ""),
            "category": (product or {}).get("category", ""),
            "price": (product or {}).get("price"),
            "sources": list(self.sources),
            "source_keys": list(self.source_keys),
            "raw_scores": {k: round(v, 6) for k, v in self.raw_scores.items()},
            "norm_scores": {k: round(v, 6) for k, v in self.norm_scores.items()},
            "evidence": self.evidence,
        }


def recall(
    request: RecommendationRequest,
    adapter: AdapterBase,
    catalog: Catalog,
    session: SessionContext | None = None,
) -> Result:
    """执行多路召回，返回合并去重后的候选列表与召回 trace。"""
    enabled = set(CFG["sources_enabled"])
    exclude: set[str] = set(request.exclude_item_ids or [])
    if session:
        exclude |= catalog.purchased(request.user_id)  # 已购商品不再推荐
        exclude |= session.blocked_item_ids()  # 本会话负反馈排除

    per_source: dict[str, list[dict]] = {}
    source_reports: list[dict] = []

    def _record(key: str, result: Result) -> None:
        if result.ok and result.data:
            per_source[key] = result.data
            source_reports.append(
                {"source": key, "label": SOURCE_LABELS.get(key, key), "status": "ok", "count": len(result.data)}
            )
        else:
            source_reports.append(
                {
                    "source": key,
                    "label": SOURCE_LABELS.get(key, key),
                    "status": "empty" if result.error is None else "failed",
                    "count": 0,
                    "error": result.error.to_dict() if result.error else None,
                }
            )

    # 1) 个性化
    if "personalized" in enabled:
        _record("personalized", adapter.personalized_candidates(request.user_id, CFG["recall_k"]))

    # 2) 相似（种子 = 参照商品 + 会话焦点 + 个性化 Top-N）
    if "similar" in enabled:
        seeds: list[str] = []
        target = request.slot_value("target")
        if target:
            seeds.append(str(target))
        if session and session.focus_item_id:
            seeds.append(session.focus_item_id)
        for row in per_source.get("personalized", [])[: CFG["similar_seed_size"]]:
            seeds.append(str(row["item_id"]))
        seeds = [s for s in dict.fromkeys(seeds) if catalog.exists(s)]
        if seeds:
            _record("similar", adapter.similar_candidates(seeds, CFG["recall_k"], exclude=exclude))

    # 3) 品类热门
    if "category_hot" in enabled:
        category = request.slot_value("category")
        if not category:
            for constraint in request.constraints:
                if constraint.field == "category":
                    category = constraint.value
                    break
        if category:
            _record("category_hot", adapter.category_candidates(str(category), CFG["recall_k"], exclude=exclude))

    # 3.5) 会话关注（焦点商品 + 刚点赞商品），保证反馈对象留在候选池内
    if "session_focus" in enabled and session is not None:
        focus_ids: list[str] = []
        if session.focus_item_id:
            focus_ids.append(session.focus_item_id)
        focus_ids.extend(list(session.likes.keys())[:3])
        focus_ids = [i for i in dict.fromkeys(focus_ids) if catalog.exists(i) and i not in exclude]
        if focus_ids:
            per_source["session_focus"] = [
                {
                    "item_id": i,
                    "score": 1.0,
                    "source_key": "session_focus",
                    "source_label": SOURCE_LABELS.get("session_focus", "会话关注召回"),
                    "detail": {"in_session_focus": True, "liked": i in session.likes},
                }
                for i in focus_ids
            ]
            source_reports.append(
                {
                    "source": "session_focus",
                    "label": SOURCE_LABELS.get("session_focus", "会话关注召回"),
                    "status": "ok",
                    "count": len(focus_ids),
                }
            )

    # 4) 历史相似
    if "history" in enabled:
        _record("history", adapter.history_candidates(request.user_id, CFG["recall_k"], exclude=exclude))

    # 5) 全局热门兜底
    if "popular" in enabled:
        _record("popular", adapter.popular_candidates(CFG["recall_k"], exclude=exclude))

    # ---------------- 合并去重 ----------------
    merged: dict[str, Candidate] = {}
    dropped_unknown = 0
    dropped_excluded = 0
    case_insensitive = CFG.get("dedupe_case_insensitive", True)
    alias: dict[str, str] = {}  # 归一化键 → 实际 item_id

    def _norm(item_id: str) -> str:
        return item_id.upper() if case_insensitive else item_id

    for key, rows in per_source.items():
        scores = {_norm(str(r["item_id"])): float(r.get("score") or 0.0) for r in rows}
        norm = minmax_normalize(scores)
        for row in rows:
            item_id = str(row["item_id"])
            if not catalog.exists(item_id):
                dropped_unknown += 1
                continue
            if item_id in exclude and key != "personalized":
                dropped_excluded += 1
                continue
            nkey = _norm(item_id)
            if nkey in alias:
                item_id = alias[nkey]
            else:
                alias[nkey] = item_id
            cand = merged.get(item_id)
            if cand is None:
                cand = Candidate(item_id=item_id)
                merged[item_id] = cand
            label = row.get("source_label") or SOURCE_LABELS.get(key, key)
            if label not in cand.sources:
                cand.sources.append(label)
            if key not in cand.source_keys:
                cand.source_keys.append(key)
            cand.raw_scores[key] = scores.get(nkey, 0.0)
            cand.norm_scores[key] = norm.get(nkey, 0.0)
            cand.evidence.setdefault("source_detail", {})[key] = row.get("detail", {})

    ranked = sorted(
        merged.values(),
        key=lambda c: (-max(c.norm_scores.values() or [0.0]), -len(c.source_keys), c.item_id),
    )
    top = ranked[: CFG["merge_top_k"]]

    trace = {
        "sources": source_reports,
        "merged_unique": len(merged),
        "kept": len(top),
        "dropped_unknown_item": dropped_unknown,
        "dropped_excluded": dropped_excluded,
        "dedupe_policy": CFG["dedupe_keep"],
        "exclude_count": len(exclude),
        "multi_source_items": sum(1 for c in merged.values() if len(c.source_keys) > 1),
        "recall_config": {"recall_k": CFG["recall_k"], "merge_top_k": CFG["merge_top_k"]},
    }
    return ok({"candidates": top, "trace": trace})
