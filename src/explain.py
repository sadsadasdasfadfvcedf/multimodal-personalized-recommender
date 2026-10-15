"""
src/explain.py
==============

检索增强的推荐解释（阶段4）。

硬性约束（阶段4 验收项）:
    1. 每条解释至少有一项可核验证据；证据不足 → 返回 E_NO_EVIDENCE，不编造。
    2. 事实只能来自 RecommendationItem 与 EvidencePack，不允许外部捏造。
    3. 商品价格/属性必须与目录一致（EvidencePack.consistency）。
    4. 视觉标签低置信度时降权或忽略。

解释输出结构：
    {"text": str, "segments": [...], "evidence_refs": [...], "evidence_count": int,
     "confidence": float, "consistent": bool, "visual_used": [...], "kb_used": [...]}
"""

from __future__ import annotations

from typing import Any

from config import settings

from .contracts import EvidencePack
from .errors import ErrorCode, Result, fail, ok

CFG = settings.EXPLANATION_CONFIG
TEMPLATES = CFG["templates"]

# 「实质性证据」类型：只有这些才算“可核验依据”。
# candidate_source 只是出处标注，不能单独支撑一条推荐解释。
SUBSTANTIVE_EVIDENCE = {"product_fact", "matched_constraint", "kb_snippet", "visual_tag", "session_signal"}


def _fmt_price(value: Any, currency: str) -> str:
    """价格展示：符号取自 config.CURRENCY_SYMBOLS，数值不做任何加工。"""
    if value is None:
        return "—"
    symbol = settings.CURRENCY_SYMBOLS.get(str(currency).upper(), "")
    return f"{symbol}{value:g}" if isinstance(value, (int, float)) else f"{symbol}{value}"


def explain(
    item: dict,
    pack: EvidencePack,
    *,
    request_summary: dict | None = None,
) -> Result:
    """基于证据包生成解释；证据不足直接失败（不编造）。"""
    facts = pack.product_facts or {}
    segments: list[dict] = []
    evidence_refs: list[dict] = []

    # --- 一致性核验：不一致时降级为「仅事实」并标记 ---
    consistent = bool(pack.consistency.get("ok", True))
    consistency_note = None
    if not consistent:
        consistency_note = "注意：该商品的部分字段与目录不一致，解释已按目录口径修正。"

    # --- 证据 1：商品事实 ---
    if facts.get("category"):
        segments.append(
            {
                "type": "category",
                "text": f"它属于「{facts['category']}」品类。",
                "evidence": {"field": "category", "value": facts["category"]},
            }
        )
        evidence_refs.append(
            {
                "type": "product_fact",
                "field": "category",
                "value": facts["category"],
                "verifiable": True,
                "source": "catalog",
            }
        )
    if facts.get("price") is not None:
        segments.append(
            {
                "type": "price",
                "text": f"参考价 {_fmt_price(facts.get('price'), facts.get('currency', 'GBP'))}"
                f"（来源：{facts.get('price_source', 'price_map')}）。",
                "evidence": {"field": "price", "value": facts["price"], "currency": facts.get("currency")},
            }
        )
        evidence_refs.append(
            {
                "type": "product_fact",
                "field": "price",
                "value": facts["price"],
                "verifiable": True,
                "source": "price_map",
            }
        )

    # --- 证据 2：命中约束 ---
    for hit in pack.matched_constraints:
        field = hit.get("field")
        if field == "price":
            op = "不超过" if hit.get("op") == "lte" else "不低于"
            text = f"符合你的预算要求：价格 {hit.get('actual')} {op} {hit.get('required')}。"
        elif field == "category":
            text = f"符合你的品类要求：{hit.get('actual')}（要求 {hit.get('required')}）。"
        elif field == "color":
            text = f"符合你的颜色要求：命中「{hit.get('actual')}」（要求 {hit.get('required')}）。"
        elif field == "similar_to":
            # 相似约束的命中信息放在 value 里，没有 required —— 必须单独处理，
            # 否则会渲染成「命中约束 similar_to=None」这种不可读文本。
            text = f"与参照商品（{hit.get('value')}）相似（依据：推荐系统相似索引）。"
        else:
            required = hit.get("required", hit.get("value"))
            text = f"命中约束 {field}={required}。"
        segments.append({"type": "constraint", "text": text, "evidence": hit})
        evidence_refs.append({"type": "matched_constraint", **hit, "verifiable": bool(hit.get("verifiable", True))})

    # --- 证据 3：相似证据 ---
    for src in pack.candidate_sources:
        segments.append({"type": "source", "text": f"它来自「{src}」这一路的候选。", "evidence": {"source": src}})
        evidence_refs.append({"type": "candidate_source", "value": src, "verifiable": True})

    # --- 证据 4：会话信号 ---
    for signal in pack.session_signals:
        segments.append({"type": "session", "text": f"会话偏好：{signal['detail']}。", "evidence": signal})
        evidence_refs.append({"type": "session_signal", **signal})

    # --- 证据 5：视觉标签（已按有效置信度筛选） ---
    visual_used: list[dict] = []
    for tag in pack.visual_tags:
        effective = float(tag.raw.get("effective_confidence", tag.confidence))
        text = TEMPLATES["visual"].format(tag=tag.tag, confidence=effective, source=tag.source)
        segments.append({"type": "visual", "text": text, "evidence": tag.to_dict()})
        evidence_refs.append(
            {"type": "visual_tag", "tag": tag.tag, "confidence": effective, "source": tag.source, "verifiable": True}
        )
        visual_used.append({"tag": tag.tag, "confidence": effective, "source": tag.source})

    # --- 证据 6：知识片段 ---
    kb_used: list[dict] = []
    for snippet in pack.evidence_snippets:
        short = snippet["text"].strip().replace("\n", " ")
        if len(short) > 70:
            short = short[:69] + "…"
        segments.append(
            {
                "type": "kb",
                "text": f"知识库依据（{snippet['chunk_id']}）：{short}",
                "evidence": {"chunk_id": snippet["chunk_id"], "score": snippet["score"]},
            }
        )
        evidence_refs.append(
            {
                "type": "kb_snippet",
                "chunk_id": snippet["chunk_id"],
                "score": snippet["score"],
                "doc": snippet.get("doc"),
                "verifiable": True,
            }
        )
        kb_used.append({"chunk_id": snippet["chunk_id"], "score": snippet["score"]})

    # --- 证据不足：不编造 ---
    verifiable = [r for r in evidence_refs if r.get("verifiable", True)]
    substantive = [r for r in verifiable if r.get("type") in SUBSTANTIVE_EVIDENCE]
    if len(substantive) < CFG["min_evidence"]:
        return fail(
            ErrorCode.NO_EVIDENCE,
            CFG["no_evidence_text"],
            item_id=pack.item_id,
            evidence_count=len(substantive),
            provenance_only=len(verifiable) - len(substantive),
            evidence_refs=evidence_refs,
            consistent=consistent,
        )

    # --- 组装文本 ---
    headline = "推荐理由："
    body = "".join(seg["text"] for seg in segments[: CFG["max_evidence"] + 2])
    text = headline + body
    if consistency_note:
        text += consistency_note

    # 置信度：证据数量 + 一致性 + 是否含视觉
    confidence = min(
        1.0, 0.35 + 0.13 * len(verifiable) + (0.1 if consistent else -0.15) + (0.05 if visual_used else 0.0)
    )
    return ok(
        {
            "text": text,
            "segments": segments,
            "evidence_refs": evidence_refs,
            "evidence_count": len(verifiable),
            "confidence": round(max(0.0, confidence), 4),
            "consistent": consistent,
            "consistency_note": consistency_note,
            "visual_used": visual_used,
            "kb_used": kb_used,
            "item_id": pack.item_id,
        }
    )


def explain_item(
    item: dict,
    *,
    request,
    catalog,
    session=None,
    visual_tags=None,
    kb=None,
) -> Result:
    """便捷入口：构建证据包 + 生成解释（一步到位）。"""
    from .evidence import build_pack

    pack_result = build_pack(
        item,
        request=request,
        catalog=catalog,
        session=session,
        visual_tags=visual_tags,
        kb=kb,
        sources=item.get("sources"),
        matched_constraints=item.get("matched_constraints"),
    )
    if not pack_result.ok:
        return pack_result
    explained = explain(item, pack_result.data)
    if not explained.ok:
        return explained
    explained.data["evidence_pack"] = pack_result.data.to_dict()
    return explained
