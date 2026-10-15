"""
src/evidence.py
===============

证据包构建（阶段4）。

EvidencePack 是解释器 **唯一** 允许读取事实的地方，包含：
    product_facts        商品事实（来自商品目录/价格表）
    candidate_sources    候选来源（可追溯）
    matched_constraints  命中的约束（含实际值）
    visual_tags          视觉标签（已做置信度治理）
    evidence_snippets    RAG 命中的知识片段
    session_signals      会话偏好信号
    consistency          价格/属性一致性核验结果

原则：证据要么来自结构化字段，要么来自可复现的检索结果；没有证据就留空。
"""

from __future__ import annotations

from config import settings

from .catalog import Catalog
from .contracts import EvidencePack, RecommendationRequest, VisualTag
from .errors import ErrorCode, Result, fail, ok
from .rag import KnowledgeBase
from .session import SessionContext
from .utils import is_number

CFG = settings.EXPLANATION_CONFIG
LEX = settings.SLOT_LEXICON


def verify_consistency(item: dict, catalog: Catalog) -> dict:
    """核验「商品价格与属性一致」：解释所用字段必须与目录一致。"""
    item_id = str(item.get("item_id"))
    product = catalog.product(item_id)
    if product is None:
        return {"ok": False, "reason": "商品不在目录中", "item_id": item_id}
    checks: list[dict] = []
    price_ok = True
    if is_number(item.get("price")):
        delta = abs(float(item["price"]) - float(product.get("price") or 0.0))
        price_ok = delta <= CFG["price_tolerance"]
        checks.append(
            {
                "field": "price",
                "explanation": item["price"],
                "catalog": product.get("price"),
                "delta": round(delta, 6),
                "ok": price_ok,
            }
        )
    category_ok = True
    if item.get("category"):
        category_ok = str(item["category"]) == str(product.get("category"))
        checks.append(
            {
                "field": "category",
                "explanation": item["category"],
                "catalog": product.get("category"),
                "ok": category_ok,
            }
        )
    return {"ok": price_ok and category_ok, "checks": checks, "item_id": item_id}


def build_pack(
    item: dict,
    *,
    request: RecommendationRequest,
    catalog: Catalog,
    session: SessionContext | None = None,
    visual_tags: list[VisualTag] | None = None,
    kb: KnowledgeBase | None = None,
    sources: list[str] | None = None,
    matched_constraints: list[dict] | None = None,
) -> Result:
    """构造某商品的 EvidencePack。"""
    item_id = str(item.get("item_id", ""))
    if not item_id:
        return fail(ErrorCode.MISSING_FIELD, "缺少 item_id，无法构建证据包", field="item_id")
    product = catalog.product(item_id)
    if product is None:
        return fail(ErrorCode.ITEM_NOT_FOUND, f"商品 {item_id} 不在目录中", field="item_id")

    facts = {
        "item_id": item_id,
        "title": product.get("title", ""),
        "english_title": product.get("english_title", ""),
        "category": product.get("category", ""),
        "price": product.get("price"),
        "currency": product.get("currency", catalog.currency),
        "price_source": product.get("price_source"),
        "transactions": product.get("transactions"),
        "origin": catalog.origin,
    }

    # 硬约束命中核验（预算/颜色/类目）
    constraints_hit: list[dict] = list(matched_constraints or [])
    for c in request.hard_constraints():
        if c.field == "price" and is_number(facts.get("price")):
            value = float(c.value)
            actual = float(facts["price"])
            if c.op == "lte" and actual <= value + 1e-9:
                constraints_hit.append(
                    {"field": "price", "op": "lte", "required": value, "actual": actual, "verifiable": True}
                )
            if c.op == "gte" and actual >= value - 1e-9:
                constraints_hit.append(
                    {"field": "price", "op": "gte", "required": value, "actual": actual, "verifiable": True}
                )
        if c.field == "category" and facts["category"] == c.value:
            constraints_hit.append(
                {"field": "category", "op": "eq", "required": c.value, "actual": facts["category"], "verifiable": True}
            )
        if c.field == "color":
            words = [w for color in (c.value or []) for w in LEX["colors"].get(color, [color])]
            title = f"{facts['title']} {facts['english_title']}".lower()
            hit = next((w for w in words if w.lower() in title), None)
            if hit:
                constraints_hit.append(
                    {"field": "color", "op": "in", "required": c.value, "actual": hit, "verifiable": True}
                )

    # 视觉标签（只保留有效置信度达标的）
    governed_tags: list[VisualTag] = []
    for tag in visual_tags or []:
        effective = float(tag.raw.get("effective_confidence", tag.confidence))
        if effective >= CFG.get("visual_min_effective_confidence", 0.55):
            governed_tags.append(tag)

    # RAG 片段
    snippets: list[dict] = []
    if kb is not None and settings.RAG_CONFIG["enabled"]:
        query = " ".join(
            filter(
                None,
                [
                    facts["category"],
                    facts["title"],
                    request.slot_value("scene") or "",
                    " ".join(str(c.value) for c in request.constraints if c.field in ("category", "color")),
                ],
            )
        )
        snippets = kb.search(query, k=settings.RAG_CONFIG["top_k"])

    # 会话信号
    signals: list[dict] = []
    if session is not None:
        pref = session.preference_signal(item_id, facts["category"])
        for reason in pref["reasons"]:
            signals.append({"type": "session_preference", "detail": reason, "verifiable": True, "value": pref["score"]})

    pack = EvidencePack(
        trace_id=request.trace_id,
        item_id=item_id,
        product_facts=facts,
        candidate_sources=list(sources or item.get("sources") or []),
        matched_constraints=constraints_hit,
        visual_tags=governed_tags,
        evidence_snippets=snippets,
        session_signals=signals,
        consistency=verify_consistency(item, catalog),
    )
    return ok(pack)


def collect_visual_tags(event_visual_tags: list[VisualTag], *, min_effective: float | None = None) -> list[VisualTag]:
    """供外部调用：按有效置信度筛选视觉标签。"""
    threshold = CFG.get("visual_min_effective_confidence", 0.55) if min_effective is None else min_effective
    out: list[VisualTag] = []
    for tag in event_visual_tags:
        effective = float(tag.raw.get("effective_confidence", tag.confidence))
        if effective >= threshold:
            out.append(tag)
    return out
