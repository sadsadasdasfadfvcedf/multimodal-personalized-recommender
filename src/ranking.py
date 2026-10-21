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
    relax_title_type: bool = False,
    relax_color: bool = False,
) -> tuple[list[Candidate], list[dict], list[dict]]:
    """执行硬约束过滤。返回 (通过, 被淘汰明细, 生效的硬约束)。"""
    exclude = exclude or set()
    hard = request.hard_constraints()
    applicable: list[dict] = []
    for c in hard:
        if c.field == "category" and relax_category:
            continue
        if c.field == "title_type" and relax_title_type:
            continue
        if c.field == "color" and relax_color:
            continue
        applicable.append(c.to_dict())

    kept: list[Candidate] = []
    dropped: list[dict] = []
    for cand in candidates:
        product = catalog.product(cand.item_id) or {}
        price = product.get("price")
        title = f"{product.get('title', '')} {product.get('english_title', '')}"
        title_lower = title.lower()
        category = product.get("category", "")
        reasons: list[dict] = []

        if cand.item_id in exclude:
            reasons.append(
                {
                    "constraint": {"field": "exclude_ids", "op": "not_in", "value": sorted(exclude)[:5]},
                    "reason": "已排除（已购/本会话负反馈/请求显式排除）",
                }
            )
        # 变量名用 spec 而非 c：上面那个循环里的 c 是 Constraint，
        # 复用同名会让 mypy 误判类型（运行时无害，但掩盖真实类型错误）。
        for spec in applicable:
            if spec["field"] == "price":
                if price is None:
                    reasons.append({"constraint": spec, "reason": "商品无价格，无法核验预算"})
                elif spec["op"] == "lte" and float(price) > float(spec["value"]) + 1e-9:
                    reasons.append({"constraint": spec, "reason": f"价格 {price} > 上限 {spec['value']}"})
                elif spec["op"] == "gte" and float(price) < float(spec["value"]) - 1e-9:
                    reasons.append({"constraint": spec, "reason": f"价格 {price} < 下限 {spec['value']}"})
            elif spec["field"] == "category":
                if category != spec["value"]:
                    reasons.append({"constraint": spec, "reason": f"类目 {category} ≠ {spec['value']}"})
            elif spec["field"] == "color":
                wanted = _color_words(list(spec["value"] or []))
                if wanted and not any(w.lower() in title_lower for w in wanted):
                    reasons.append({"constraint": spec, "reason": f"标题未命中颜色 {sorted(wanted)}"})
            elif spec["field"] == "title_type":
                terms = [str(term).lower() for term in (spec["value"] or [])]
                if terms and not any(term in title_lower for term in terms):
                    reasons.append({"constraint": spec, "reason": f"标题未命中商品类型 {terms}"})
        if reasons:
            dropped.append(
                {
                    "item_id": cand.item_id,
                    "title": product.get("title", ""),
                    "sources": cand.sources,
                    "reasons": reasons,
                }
            )
        else:
            kept.append(cand)
    return kept, dropped, applicable


def _constraint_match(
    cand: Candidate, request: RecommendationRequest, catalog: Catalog, session: SessionContext | None
) -> tuple[float, list[dict], list[dict]]:
    """软约束命中比例，并返回命中/未命中明细。"""
    product = catalog.product(cand.item_id) or {}
    title = f"{product.get('title', '')} {product.get('english_title', '')}".lower()
    category = product.get("category", "")
    matched: list[dict] = []
    missed: list[dict] = []

    soft = request.soft_constraints()
    type_checks = [c for c in request.constraints if c.field == "title_type"]
    for c in [*soft, *type_checks]:
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
        elif c.field == "title_type":
            terms = [str(term).lower() for term in (c.value or [])]
            hit = next((term for term in terms if term in title), None)
            if hit:
                matched.append({"field": "商品类型", "value": c.value, "actual": hit})
            else:
                missed.append({"field": "商品类型", "value": c.value, "actual": None})

    # 无软约束时，用「有来源证据」作为基础匹配度
    total = len(soft) + len(type_checks)
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
    # 已购商品双重防线：recall 合并时已按排除项过滤，这里在排序入口
    # 再兜底一次 —— rank() 也可能被测试/脚本直接调用，硬约束过滤的
    # 淘汰原因「已排除（已购/…）」必须与事实一致。
    exclude |= catalog.purchased(request.user_id)
    if session:
        exclude |= session.blocked_item_ids()

    kept, dropped, applied = hard_filter(candidates, request, catalog, exclude=exclude)
    exact_ids = {c.item_id for c in kept}
    strict_dropped = len(dropped)
    degraded = False
    degrade_reason = None
    has_title_type = any(c.field == "title_type" for c in request.constraints)
    has_hard_color = any(c.field == "color" and c.kind == "hard" for c in request.constraints)

    if not kept:
        if has_hard_color:
            relaxed_color, dropped_color, applied_color = hard_filter(
                candidates,
                request,
                catalog,
                exclude=exclude,
                relax_color=True,
            )
            if relaxed_color:
                kept, dropped, applied = relaxed_color, dropped_color, applied_color
                degraded = True
                degrade_reason = "颜色无精确匹配，已保留商品类型并放宽颜色"

        # 降级 1：具体商品类型没有命中时，先退回同类目，保留预算/颜色。
        if not kept:
            relaxed_type, dropped_type, applied_type = hard_filter(
                candidates,
                request,
                catalog,
                exclude=exclude,
                relax_title_type=True,
            )
            if relaxed_type:
                kept, dropped, applied = relaxed_type, dropped_type, applied_type
                degraded = True
                degrade_reason = "具体商品类型无匹配，已放宽为同类目推荐并保留预算/颜色约束"

        # 降级 2：类目也无匹配时，再放宽类目，仅保留预算/颜色。
        if not kept:
            relaxed, dropped_relax, applied_relax = hard_filter(
                candidates,
                request,
                catalog,
                exclude=exclude,
                relax_category=True,
                relax_title_type=True,
            )
            if relaxed:
                kept, dropped, applied = relaxed, dropped_relax, applied_relax
                degraded = True
                degrade_reason = (
                    "硬约束（类目/商品类型）过滤后为空，已放宽并保留预算/颜色硬约束"
                    if has_title_type
                    else "硬约束（类目）过滤后为空，已放宽类目约束并保留预算/颜色硬约束"
                )

        if not kept:
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

    broadened_reasons: dict[str, list[str]] = {}
    if not exact_ids and degrade_reason:
        broadened_reasons = {c.item_id: [degrade_reason] for c in kept}

    target = min(top_k, max(int(RANK.get("min_results", top_k)), len(kept)))
    if len(kept) < target:
        seen = {c.item_id for c in kept}

        def add_candidates(rows: list[Candidate], reason: str) -> None:
            for cand in rows:
                if len(kept) >= target:
                    return
                if cand.item_id in seen or cand.item_id in exclude:
                    continue
                kept.append(cand)
                seen.add(cand.item_id)
                broadened_reasons[cand.item_id] = [reason]

        def add_relaxed(reason: str, **flags: bool) -> None:
            relaxed, _, _ = hard_filter(candidates, request, catalog, exclude=exclude, **flags)
            add_candidates(relaxed, reason)

        item_type = request.slot_value("item_type")
        type_cfg = LEX.get("product_types", {}).get(str(item_type), {}) if item_type else {}
        related_terms = [str(term).lower() for term in type_cfg.get("related_terms", [])]
        if related_terms:
            related_rows = []
            for cand in candidates:
                product = catalog.product(cand.item_id) or {}
                title = f"{product.get('title', '')} {product.get('english_title', '')}".lower()
                if any(term in title for term in related_terms):
                    related_rows.append(cand)
            add_candidates(related_rows, "同类商品补充")

        # 优先补同大类，避免“黑色背包”不足时混入蜡烛、贺卡等无关商品。
        category = request.slot_value("category")
        if category:
            same_category = [cand for cand in candidates if catalog.category_of(cand.item_id) == str(category)]
            add_candidates(same_category, "同品类补充")

        if has_title_type:
            add_relaxed("放宽商品类型", relax_title_type=True)
        add_relaxed("放宽颜色", relax_title_type=True, relax_color=True)
        add_relaxed(
            "放宽类目",
            relax_title_type=True,
            relax_color=True,
            relax_category=True,
        )
        if len(kept) < target:
            add_candidates(candidates, "热门补充")

    scored: list[ScoredCandidate] = []
    for cand in kept:
        # 值类型不齐（score 是 float，reasons 是 list），显式标注避免推断成联合类型
        pref: dict[str, Any] = (
            session.preference_signal(cand.item_id, catalog.category_of(cand.item_id))
            if session
            else {"score": 0.0, "reasons": []}
        )
        ratio, matched, missed = _constraint_match(cand, request, catalog, session)
        type_matched = any(item.get("field") == "商品类型" for item in matched)
        trust = max((trust_table.get(label, 0.5) for label in cand.sources), default=0.5)
        base = cand.raw_scores.get("personalized")
        if base is None:
            # 非个性化候选打折，保证「推荐系统个性化精排」仍是主信号
            base = RANK["non_personalized_base_discount"] * max(cand.norm_scores.values() or [0.0])
        base = clamp(float(base), 0.0, 1.0)
        pref_norm = clamp(
            (pref["score"] + RANK["session_preference"]["max_boost"]) / (2 * RANK["session_preference"]["max_boost"]),
            0.0,
            1.0,
        )
        final = (
            weights["base_score"] * base
            + weights["constraint_match"] * ratio
            + weights["source_trust"] * trust
            + weights["session_preference"] * pref_norm
        )
        match_scope = "broadened" if cand.item_id in broadened_reasons else "exact"
        evidence = dict(cand.evidence)
        evidence["match_scope"] = match_scope
        evidence["type_matched"] = type_matched
        if match_scope == "broadened":
            evidence["broadened_reasons"] = broadened_reasons[cand.item_id]
        scored.append(
            ScoredCandidate(
                item_id=cand.item_id,
                final_score=final,
                base_score=base,
                constraint_match=ratio,
                source_trust=trust,
                pref_score=pref["score"],
                pref_reasons=list(pref["reasons"]),
                sources=list(cand.sources),
                source_keys=list(cand.source_keys),
                matched_constraints=matched,
                missed_constraints=missed,
                raw_scores=dict(cand.raw_scores),
                evidence=evidence,
            )
        )

    # 稳定排序：final 降序 → base 降序 → item_id 升序（保证可复现）
    scored.sort(
        key=lambda s: (
            s.evidence.get("match_scope") == "broadened",
            not bool(s.evidence.get("type_matched")),
            -s.final_score,
            -s.base_score,
            s.item_id,
        )
    )
    for idx, item in enumerate(scored[:top_k], start=1):
        item.rank = idx

    top_items = scored[:top_k]
    exact_in_output = sum(item.evidence.get("match_scope") == "exact" for item in top_items)
    type_matched_in_output = sum(bool(item.evidence.get("type_matched")) for item in top_items)
    broadened_in_output = len(top_items) - exact_in_output
    trace = {
        "input_candidates": len(candidates),
        "after_hard_filter": len(exact_ids),
        "hard_dropped": strict_dropped,
        "hard_constraints_applied": applied,
        "hard_constraint_violation_rate": round(strict_dropped / len(candidates), 6) if candidates else None,
        "dropped_sample": dropped[:5],
        "strict_count": exact_in_output,
        "supplemented_count": broadened_in_output,
        "supplement_note": (
            (
                f"严格匹配 {exact_in_output} 件，另补充 {broadened_in_output} 件同类好物"
                if exact_in_output
                else (
                    f"{type_matched_in_output} 件符合商品类型，"
                    f"另补充 {broadened_in_output - type_matched_in_output} 件同类好物"
                    if type_matched_in_output
                    else f"当前条件较窄，为你补充 {broadened_in_output} 件同类好物"
                )
            )
            if broadened_in_output
            else None
        ),
        "weights": weights,
        "top_k": top_k,
        "degraded": degraded,
        "degrade_reason": degrade_reason,
        "empty_result_hint": RANK["empty_result_hint"] if degraded else None,
        "ordering_key": "final_score desc → base_score desc → item_id asc",
    }
    return ok({"ranked": top_items, "all_scored": scored, "trace": trace})
