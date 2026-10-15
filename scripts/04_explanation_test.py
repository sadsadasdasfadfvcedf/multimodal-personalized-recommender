"""
scripts/04_explanation_test.py
==============================

阶段4：商品多模态理解 → 证据包 → 检索增强解释 → 文本/语音呈现。

用例类型：正常 / 证据不足 / 商品字段缺失 / 低置信视觉 / 高置信视觉 / 商品不存在 / 证据不一致。

输出：
    outputs/stage04_explanation_cases.jsonl  「解释 ↔ 证据」对照表（逐行）
    outputs/stage04_explanations.json        汇总 + 异常解释记录 + 验收清单
"""

from __future__ import annotations

from typing import Any

from _bootstrap import banner

from config import settings
from src.catalog import Catalog
from src.contracts import VisualTag, make_event
from src.errors import ErrorCode
from src.evidence import build_pack
from src.explain import explain
from src.intent import understand
from src.presentation import present
from src.rag import KnowledgeBase
from src.utils import now_iso, read_json, write_json, write_jsonl


def _build_request(spec: dict, user_id: str, catalog: Catalog, focus: str | None):
    event = make_event(
        user_id=user_id,
        source=spec.get("source", "text"),
        modality="asr" if spec.get("text") else "none",
        text=spec.get("text"),
        visual_tags=spec.get("visual_tags"),
        gesture=spec.get("gesture"),
    )
    result = understand(
        event,
        catalog_categories=catalog.categories(),
        focus_item_id=focus,
        session_items=[],
        title_lookup=None,
        session_id="stage04",
        mode="offline",
    )
    if result.ok:
        return event, result.data["request"], result.data["intent_result"]
    # 语境不构成明确请求时，构造一个最小请求（只用于解释，不做召回）
    from src.contracts import RecommendationRequest

    request = RecommendationRequest(
        trace_id=event.trace_id,
        user_id=user_id,
        timestamp=event.timestamp,
        source=event.source,
        intent="explain",
        session_id="stage04",
    )
    return (
        event,
        request,
        {"intent": "explain", "intent_confidence": 0.0, "slots": {}, "missing_slots": [], "conflicts": []},
    )


def main() -> int:
    banner("阶段4 · 商品多模态理解 / RAG 解释 / 交互呈现")
    base_catalog = Catalog.from_snapshot()
    kb = KnowledgeBase().load()
    cases = read_json("data/cases/stage04_cases.json", default=[])
    records: list[dict] = []
    abnormal: list[dict] = []

    for case in cases:
        catalog = base_catalog.clone()
        override = case.get("product_override")
        if override:
            catalog.apply_override(case["item_id"], override)

        visual_tags = [VisualTag.from_dict(t) for t in case.get("visual_tags", [])]
        event, request, _intent_info = _build_request(case["spec"], case["user_id"], catalog, focus=case["item_id"])
        request.trace_id = event.trace_id
        if case.get("spec", {}).get("visual_tags"):
            visual_tags = visual_tags or [VisualTag.from_dict(t) for t in case["spec"]["visual_tags"]]

        product = catalog.product(case["item_id"]) or {}
        item = {
            "item_id": case["item_id"],
            "title": product.get("title", ""),
            "category": product.get("category", ""),
            "price": product.get("price"),
            "currency": product.get("currency", catalog.currency),
            "sources": ["用户指定商品"],
            "matched_constraints": [],
        }
        if case.get("item_price_override") is not None:
            item["price"] = case["item_price_override"]

        record = {
            "case_id": case["id"],
            "kind": case["kind"],
            "desc": case["desc"],
            "user_id": case["user_id"],
            "item_id": case["item_id"],
            "request": request.to_dict(),
            "generated_at": now_iso(),
        }

        pack_result = build_pack(
            item,
            request=request,
            catalog=catalog,
            session=None,
            visual_tags=visual_tags,
            kb=kb if case.get("use_kb", True) else None,
            sources=item["sources"],
            matched_constraints=item["matched_constraints"],
        )
        if not pack_result.ok:
            record.update(
                {
                    "status": "item_not_found"
                    if pack_result.error and pack_result.error.code == ErrorCode.ITEM_NOT_FOUND
                    else "pack_error",
                    "error": pack_result.error.to_dict() if pack_result.error else None,
                }
            )
            record["passed"] = case["expect"]["status"] == record["status"]
            record["problems"] = (
                [] if record["passed"] else [f"状态期望 {case['expect']['status']} 实际 {record['status']}"]
            )
            records.append(record)
            if case["kind"] not in ("not_found",):
                abnormal.append(record)
            print(f"[{'PASS' if record['passed'] else 'FAIL'}] {case['id']} ({case['kind']}) → {record['status']}")
            continue

        pack = pack_result.data
        explanation = explain(item, pack)
        if explanation.ok:
            data = explanation.data
            record.update(
                {
                    "status": "ok",
                    "explanation_text": data["text"],
                    "evidence_count": data["evidence_count"],
                    "confidence": data["confidence"],
                    "consistent": data["consistent"],
                    "consistency_note": data["consistency_note"],
                    "evidence_table": [
                        {"segment_type": seg["type"], "claim": seg["text"], "evidence": seg["evidence"]}
                        for seg in data["segments"]
                    ],
                    "evidence_refs": data["evidence_refs"],
                    "visual_used": data["visual_used"],
                    "kb_used": data["kb_used"],
                    "product_facts": pack.product_facts,
                }
            )
            # 阶段4 步骤4 要求「验证文本/语音呈现与一致性」——
            # 因此正常用例实际合成一次语音，并把文本呈现与语音播报内容对齐记录。
            presentation = present(
                [{**item, "explanation": data["text"]}], header="解释：", with_voice=case["kind"] == "normal"
            )
            record["presentation"] = presentation
        else:
            record.update(
                {
                    "status": "no_evidence",
                    "error": explanation.error.to_dict() if explanation.error else None,
                    "explanation_text": explanation.error.message if explanation.error else "",
                    "evidence_count": explanation.error.context.get("evidence_count", 0) if explanation.error else 0,
                    "product_facts": pack.product_facts,
                }
            )
            abnormal.append(record)

        expect = case["expect"]
        problems: list[str] = []
        if expect["status"] != record["status"]:
            problems.append(f"状态期望 {expect['status']} 实际 {record['status']}")
        if "consistent" in expect and record.get("consistent") != expect["consistent"]:
            problems.append(f"一致性期望 {expect['consistent']} 实际 {record.get('consistent')}")
        if "min_evidence" in expect and record.get("evidence_count", 0) < expect["min_evidence"]:
            problems.append(f"证据数期望 ≥{expect['min_evidence']} 实际 {record.get('evidence_count')}")
        if "visual_used" in expect:
            got = [v["tag"] for v in record.get("visual_used", [])]
            if got != expect["visual_used"]:
                problems.append(f"视觉证据期望 {expect['visual_used']} 实际 {got}")
        record["problems"] = problems
        record["passed"] = not problems
        records.append(record)

        flag = "PASS" if record["passed"] else "FAIL"
        print(
            f"[{flag}] {case['id']} ({case['kind']}) → {record['status']} "
            f"证据={record.get('evidence_count')} 置信度={record.get('confidence')}"
        )
        if record["status"] == "ok":
            print(f"       {record['explanation_text'][:96]}…")
        for p in problems:
            print(f"       - {p}")

    write_jsonl(settings.output_path("stage04_cases"), records)

    passed = sum(1 for r in records if r.get("passed"))
    summary: dict[str, Any] = {
        "stage": "04",
        "generated_at": now_iso(),
        "summary": {
            "total": len(records),
            "passed": passed,
            "failed": len(records) - passed,
            "pass_rate": round(passed / len(records), 4) if records else 0.0,
        },
        "acceptance_checklist": {
            "每条解释至少有一项可核验证据": all(
                r.get("evidence_count", 0) >= 1 for r in records if r["status"] == "ok"
            ),
            "无证据时不编造": any(r["status"] == "no_evidence" for r in records),
            "商品价格与属性一致": all(
                r.get("consistent") for r in records if r["status"] == "ok" and r["case_id"] != "E-07"
            ),
            "视觉标签低置信度时降权或忽略": any(
                r.get("kind") == "low_conf_visual" and not r.get("visual_used") for r in records
            ),
        },
        "abnormal_records": abnormal,
        "explanation_evidence_table": [
            {
                "case_id": r["case_id"],
                "item_id": r["item_id"],
                "status": r["status"],
                "declaration": r.get("explanation_text", "")[:80],
                "evidence_count": r.get("evidence_count"),
                "evidence_refs": [e.get("type") for e in r.get("evidence_refs", [])],
            }
            for r in records
        ],
        "rag": {"chunks": kb.size(), "documents": kb.docs()},
    }
    # ---- 文本/语音呈现与一致性验证（阶段4 步骤4） ----
    spoken = [r for r in records if (r.get("presentation") or {}).get("spoken_text")]
    voice_ok = [r for r in spoken if (r.get("presentation") or {}).get("voice_available")]
    consistency_rows = []
    for r in spoken:
        pres = r["presentation"] or {}
        text = pres.get("text") or ""
        speech = pres.get("spoken_text") or ""
        # 一致性口径：朗读文本必须取自呈现文本（商品名/价格等关键信息不得凭空出现），
        # 且朗读内容不得长于展示文本（语音只能是摘要，不能额外加戏）。
        head = speech.replace("解释：", "").strip()
        consistency_rows.append(
            {
                "case_id": r["case_id"],
                "text_chars": len(text),
                "spoken_chars": len(speech),
                "spoken_not_longer": len(speech) <= len(text) or len(text) == 0,
                "spoken_is_excerpt": bool(head) and (head.split("。")[0][:12] in text or head[:8] in text),
                "voice_available": pres.get("voice_available"),
                "engine": pres.get("engine"),
                "audio_path": pres.get("audio_path"),
                "presentation_degradations": pres.get("degradations") or [],
            }
        )
    summary["presentation_check"] = {
        "cases": consistency_rows,
        "voice_available_count": len(voice_ok),
        "all_spoken_is_excerpt": all(x["spoken_is_excerpt"] for x in consistency_rows) if consistency_rows else False,
        "all_spoken_not_longer": all(x["spoken_not_longer"] for x in consistency_rows) if consistency_rows else False,
    }
    summary["acceptance_checklist"]["文本/语音呈现与一致性"] = (
        bool(consistency_rows)
        and summary["presentation_check"]["all_spoken_is_excerpt"]
        and summary["presentation_check"]["all_spoken_not_longer"]
    )

    path = write_json(settings.output_path("stage04_summary"), summary)

    print("\n验收清单：")
    for key, value in summary["acceptance_checklist"].items():
        print(f"  [{'PASS' if value else 'FAIL'}] {key}")
    pc = summary.get("presentation_check", {})
    print(
        f"\n文本/语音呈现：{pc.get('voice_available_count')} 条合成成功；"
        f"语音为文本摘录={pc.get('all_spoken_is_excerpt')}，语音不长于文本={pc.get('all_spoken_not_longer')}"
    )
    for row in pc.get("cases") or []:
        print(
            f"  {row['case_id']}: 文本 {row['text_chars']} 字 / 语音 {row['spoken_chars']} 字 "
            f"({row['engine']}) {row['audio_path']}"
        )
    print(f"\n[04_explanation_test] 解释与证据对照表：{settings.output_path('stage04_cases').name}；汇总：{path.name}")
    return 0 if passed == len(records) and all(summary["acceptance_checklist"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
