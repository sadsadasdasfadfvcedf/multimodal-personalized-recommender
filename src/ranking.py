"""
src/ranking.py
==============

硬约束过滤 + 上下文重排（阶段3）。

关键不变量（阶段3 验收项）:
    1. 硬约束先于打分生效，且 **排序分数不得覆盖硬约束** —— 被硬约束淘汰的商品绝不会出现在结果里；
    2. 空结果有清晰提示；
    3. 同输入 + 同配置可复现（无随机性）。

打分公式（权重全部来自 config.RANKING_CONFIG["weights"]）::

    final = w_base * base_score          # 推荐系统 v146 原始分数（无则取候选内归一化最高）
          + w_constraint * constraint_match   # 软约束命中比例
          + w_source * source_trust           # 候选来源可信度
          + w_pref * session_pref_norm        # 会话偏好（点赞/倒赞/已见）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config import settings
from .catalog import Catalog
from .contracts import RecommendationRequest
from .errors import ErrorCode, Result, fail, ok
from .recall import Candidate
from .session import SessionContext
from .utils import clamp

RANK = settings.RANKING_CONFIG
LEX = settings.SLOT_LEXICON


@dataclass
class ScoredCandidate:
    item_id: str
    final_score: float
    base_score: float
    constraint_match: float
    source_trust: float
    pref_score: float
    pref_reasons: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    source_keys: list[str] = field(default_factory=list)
    matched_constraints: list[dict] = field(default_factory=list)
    missed_constraints: list[dict] = field(default_factory=list)
    raw_scores: dict[str, float] = field(default_factory=dict)
    evidence: dict = field(default_factory=dict)
    rank: int = 0

    def to_dict(self, catalog: Catalog | None = None) -> dict:
        product = catalog.product(self.item_id) if catalog else None
        return {
            "item_id": self.item_id,
            "rank": self.rank,
            "title": (product or {}).get("title", ""),
            "category": (product or {}).get("category", ""),
            "price": (product or {}).get("price"),
            "currency": (product or {}).get("currency", ""),
            "score": round(self.final_score, 6),
            "score_breakdown": {
                "base_score": round(self.base_score, 6),
                "constraint_match": round(self.constraint_match, 6),
                "source_trust": round(self.source_trust, 6),
                "session_preference": round(self.pref_score, 6),
            },
            "sources": list(self.sources),
            "source_keys": list(self.source_keys),
            "matched_constraints": self.matched_constraints,
            "missed_constraints": self.missed_constraints,
            "session_reasons": self.pref_reasons,
            "raw_scores": {k: round(v, 6) for k, v in self.raw_scores.items()},
            "evidence": self.evidence,
        }


# ------------------------------------------------------------
# 硬约束判定
# ------------------------------------------------------------
def _color_words(colors: list[str]) -> set[str]:
    words: set[str] = set()
    for color in colors:
        words.update(LEX["colors"].get(color, [color]))
    return words


def _scene_words(scene: str) -> list[str]:
    return [w for w, s in LEX["scenes"].items() if s == scene] + [scene]


def hard_filter(
    candidates: list[Candidate],
    request: RecommendationRequest,
    catalog: Catalog,
    *,
    exclude: set[str] | None = None,
    relax_category: bool = False,
) -> tuple[list[Candidate], list[dict], list[dict]]:
    """执行硬约束过滤。返回 (通过, 被淘汰明细, 生效的硬约束)。"""
    exclude = exclude or set()
    hard = request.hard_constraints()
    applicable: list[dict] = []
    for c in hard:
        if c.field == "category" and relax_category:
            continue
        applicable.append(c.to_dict())

    kept: list[Candidate] = []
    dropped: list[dict] = []
    for cand in candidates:
        product = catalog.product(cand.item_id) or {}
        price = product.get("price")
        title = f"{product.get('title', '')} {product.get('english_title', '')}"
        category = product.get("category", "")
        reasons: list[dict] = []

        if cand.item_id in exclude:
            reasons.append({"constraint": {"field": "exclude_ids", "op": "not_in", "value": sorted(exclude)[:5]},
                            "reason": "已排除（已购/本会话负反馈/请求显式排除）"})
        for c in applicable:
            if c["field"] == "price":
                if price is None:
                    reasons.append({"constraint": c, "reason": "商品无价格，无法核验预算"})
                elif c["op"] == "lte" and float(price) > float(c["value"]) + 1e-9:
                    reasons.append({"constraint": c, "reason": f"价格 {price} > 上限 {c['value']}"})
                elif c["op"] == "gte" and float(price) < float(c["value"]) - 1e-9:
                    reasons.append({"constraint": c, "reason": f"价格 {price} < 下限 {c['value']}"})
            elif c["field"] == "category":
                if category != c["value"]:
                    reasons.append({"constraint": c, "reason": f"类目 {category} ≠ {c['value']}"})
            elif c["field"] == "color":
                wanted = _color_words(list(c["value"] or []))
                if wanted and not any(w.lower() in title.lower() for w in wanted):
                    reasons.append({"constraint": c, "reason": f"标题未命中颜色 {sorted(wanted)}"})
        if reasons:
            dropped.append({"item_id": cand.item_id, "title": product.get("title", ""),
                            "sources": cand.sources, "reasons": reasons})
        else:
            kept.append(cand)
    return kept, dropped, applicable


def _constraint_match(cand: Candidate, request: RecommendationRequest, catalog: Catalog,
                      session: SessionContext | None) -> tuple[float, list[dict], list[dict]]:
    """软约束命中比例，并返回命中/未命中明细。"""
    product = catalog.product(cand.item_id) or {}
    title = f"{product.get('title', '')} {product.get('english_title', '')}".lower()
    category = product.get("category", "")
    matched: list[dict] = []
    missed: list[dict] = []

    soft = request.soft_constraints()
    for c in soft:
        if c.field == "category":
            if category == c.value:
                matched.append({"field": "category", "value": c.value, "actual": category})
            else:
                missed.append({"field": "category", "value": c.value, "actual": category})
        elif c.field == "scene":
            words = _scene_words(str(c.value))
            hit = next((w for w in words if w.lower() in title), None)
            if hit:
                matched.append({"field": "scene", "value": c.value, "hit_word": hit})
            else:
                missed.append({"field": "scene", "value": c.value, "actual": None})
        elif c.field == "item_id" and c.op == "similar_to":
            neighbors = set(catalog.similar_of(str(c.value), k=12))
            if cand.item_id in neighbors:
                matched.append({"field": "similar_to", "value": c.value, "hit": True})
            else:
                missed.append({"field": "similar_to", "value": c.value, "hit": False})

    # 无软约束时，用「有来源证据」作为基础匹配度
    total = len(soft)
    ratio = (len(matched) / total) if total else 1.0
    return ratio, matched, missed


def rank(
    candidates: list[Candidate],
    request: RecommendationRequest,
    catalog: Catalog,
    session: SessionContext | None = None,
    *,
    top_k: int | None = None,
) -> Result:
    """硬约束过滤 + 上下文打分排序。空结果时降级并标记 degraded。"""
    top_k = int(top_k or request.top_k or RANK["top_k"])
    weights = RANK["weights"]
    trust_table = RANK["source_trust"]
    exclude = set(request.exclude_item_ids or [])
    if session:
        exclude |= session.blocked_item_ids()

    kept, dropped, applied = hard_filter(candidates, request, catalog, exclude=exclude)
    degraded = False
    degrade_reason = None

    if not kept:
        # 降级：放宽硬约束中的类目，仅保留预算/颜色等可核验硬约束
        relaxed, dropped_relax, applied_relax = hard_filter(
            candidates, request, catalog, exclude=exclude, relax_category=True)
        if relaxed:
            kept, dropped, applied = relaxed, dropped_relax, applied_relax
            degraded = True
            degrade_reason = "硬约束（类目）过滤后为空，已放宽类目约束并保留预算/颜色硬约束"
        else:
            # 完全放宽：仅排除集合生效，用热门兜底
            kept = [c for c in candidates if c.item_id not in exclude]
            applied = []
            degraded = True
            degrade_reason = "全部硬约束过滤后为空，已降级为热门兜底"
        if not kept:
            return fail(
                ErrorCode.HARD_CONSTRAINT_EMPTY,
                RANK["empty_result_hint"],
                applied_constraints=applied,
                candidates=len(candidates),
                dropped_sample=dropped[:5],
                degraded=True,
            )

    scored: list[ScoredCandidate] = []
    for cand in kept:
        pref = session.preference_signal(cand.item_id, catalog.category_of(cand.item_id)) if session \
            else {"score": 0.0, "reasons": []}
        ratio, matched, missed = _constraint_match(cand, request, catalog, session)
        trust = max((trust_table.get(label, 0.5) for label in cand.sources), default=0.5)
        base = cand.raw_scores.get("personalized")
        if base is None:
            # 非个性化候选打折，保证「推荐系统个性化精排」仍是主信号
            base = RANK["non_personalized_base_discount"] * max(cand.norm_scores.values() or [0.0])
        base = clamp(float(base), 0.0, 1.0)
        pref_norm = clamp((pref["score"] + RANK["session_preference"]["max_boost"]) /
                          (2 * RANK["session_preference"]["max_boost"]), 0.0, 1.0)
        final = (weights["base_score"] * base
                 + weights["constraint_match"] * ratio
                 + weights["source_trust"] * trust
                 + weights["session_preference"] * pref_norm)
        scored.append(ScoredCandidate(
            item_id=cand.item_id, final_score=final, base_score=base,
            constraint_match=ratio, source_trust=trust, pref_score=pref["score"],
            pref_reasons=list(pref["reasons"]), sources=list(cand.sources),
            source_keys=list(cand.source_keys), matched_constraints=matched,
            missed_constraints=missed, raw_scores=dict(cand.raw_scores),
            evidence=dict(cand.evidence),
        ))

    # 稳定排序：final 降序 → base 降序 → item_id 升序（保证可复现）
    scored.sort(key=lambda s: (-s.final_score, -s.base_score, s.item_id))
    for idx, item in enumerate(scored[:top_k], start=1):
        item.rank = idx

    trace = {
        "input_candidates": len(candidates),
        "after_hard_filter": len(kept),
        "hard_dropped": len(dropped),
        "hard_constraints_applied": applied,
        "hard_constraint_violation_rate": round(len(dropped) / len(candidates), 6) if candidates else None,
        "dropped_sample": dropped[:5],
        "weights": weights,
        "top_k": top_k,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "empty_result_hint": RANK["empty_result_hint"] if degraded else None,
        "ordering_key": "final_score desc → base_score desc → item_id asc",
    }
    return ok({"ranked": scored[:top_k], "all_scored": scored, "trace": trace})
