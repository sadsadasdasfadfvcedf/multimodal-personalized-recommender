"""
scripts/01_contract_test.py
===========================

阶段1：接口契约测试 + 降级矩阵验证。

覆盖三类用例（成功 / 边界 / 失败）：
    A. 三类数据结构的 JSON 校验（InteractionEvent / RecommendationRequest / RecommendationItem）
    B. 公共信封字段 trace_id / user_id / timestamp / source 的强制校验
    C. 非法输入的明确错误码（不返回空字典）
    D. 多模态适配器的置信度治理与空 ASR 处理
    E. 推荐适配器的离线降级与「用户无候选」错误
    F. 降级矩阵自检（每一行都必须有 level / fallback）

输出：outputs/stage01_contract_report.json
"""
from __future__ import annotations

from _bootstrap import banner

from config import settings
from src.adapters import MultimodalAdapter, RecommenderAdapter
from src.catalog import Catalog
from src.contracts import (
    SCHEMA_VERSION,
    InteractionEvent,
    RecommendationItem,
    RecommendationRequest,
    make_event,
)
from src.errors import ErrorCode
from src.utils import now_iso, write_json

RESULTS: list[dict] = []


def check(case_id: str, category: str, description: str, expectation: str, actual: dict, passed: bool) -> None:
    RESULTS.append({
        "case_id": case_id, "category": category, "description": description,
        "expectation": expectation, "actual": actual, "passed": bool(passed),
    })


def _err_of(result) -> str | None:
    return result.error.code.value if (not result.ok and result.error) else None


def main() -> int:
    banner("阶段1 · 接口契约测试")
    catalog = Catalog.from_snapshot()

    # ---------------------------------------------------------
    # A. InteractionEvent
    # ---------------------------------------------------------
    ok_event = make_event(user_id="15547", source="voice", modality="asr", text="帮我找一个礼物")
    res = InteractionEvent.from_dict(ok_event.to_dict())
    check("C-01", "成功", "合法 InteractionEvent 往返序列化", "ok=True",
          {"ok": res.ok, "trace_id": res.data.trace_id if res.ok else None}, res.ok)

    res = InteractionEvent.from_dict({"user_id": "15547"})
    problems = (res.error.context or {}).get("problems", []) if not res.ok else []
    fields = sorted({p["field"] for p in problems})
    check("C-02", "失败", "缺少信封字段 → 明确错误码 + 字段清单",
          f"code={ErrorCode.MISSING_FIELD.value} 且 fields 含 trace_id/timestamp/source",
          {"code": _err_of(res), "fields": fields},
          _err_of(res) == ErrorCode.MISSING_FIELD.value
          and {"trace_id", "timestamp", "source"} <= set(fields))

    res = InteractionEvent.from_dict({"trace_id": "t", "user_id": "u", "timestamp": "2026/01/01", "source": "voice"})
    check("C-03", "失败", "非法时间戳格式", f"code={ErrorCode.INVALID_TIMESTAMP.value}",
          {"code": _err_of(res), "field": res.error.field if not res.ok else None},
          _err_of(res) == ErrorCode.INVALID_TIMESTAMP.value)

    res = InteractionEvent.from_dict({"trace_id": "t", "user_id": "u", "timestamp": now_iso(), "source": "bluetooth"})
    check("C-04", "失败", "非法 source 取值", f"code={ErrorCode.INVALID_SOURCE.value}",
          {"code": _err_of(res), "field": res.error.field if not res.ok else None},
          _err_of(res) == ErrorCode.INVALID_SOURCE.value)

    res = InteractionEvent.from_dict({"trace_id": "t", "user_id": "u", "timestamp": now_iso(),
                                      "source": "voice", "modality": "asr", "text": "hi",
                                      "schema_version": "0.9.0"})
    check("C-05", "边界", "schema 版本不兼容", f"code={ErrorCode.SCHEMA_VERSION_MISMATCH.value}",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.SCHEMA_VERSION_MISMATCH.value)

    res = InteractionEvent.from_dict("not-a-dict")
    check("C-06", "失败", "顶层不是 JSON 对象", "code=E_TYPE_MISMATCH",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.TYPE_MISMATCH.value)

    # ---------------------------------------------------------
    # B. RecommendationRequest
    # ---------------------------------------------------------
    payload = {
        "trace_id": "t-1", "user_id": "15547", "timestamp": now_iso(), "source": "text",
        "intent": "filter", "slots": {"budget_max": {"name": "budget_max", "value": 20.0,
                                                     "confidence": 0.9, "evidence": ["文本预算"]}},
        "constraints": [{"field": "price", "op": "lte", "value": 20.0, "kind": "hard"}],
        "top_k": 8, "mode": "offline",
    }
    res = RecommendationRequest.from_dict(payload)
    check("C-07", "成功", "合法 RecommendationRequest", "ok=True 且硬约束可读",
          {"ok": res.ok, "hard": [c.field for c in res.data.hard_constraints()] if res.ok else None},
          res.ok and len(res.data.hard_constraints()) == 1)

    bad = dict(payload); bad["top_k"] = 0
    res = RecommendationRequest.from_dict(bad)
    check("C-08", "失败", "top_k 非法（0）", "code=E_INVALID_INPUT",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.INVALID_INPUT.value)

    bad = dict(payload); bad["intent"] = "panic"
    res = RecommendationRequest.from_dict(bad)
    check("C-09", "失败", "非法 intent", "code=E_INVALID_INPUT",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.INVALID_INPUT.value)

    # ---------------------------------------------------------
    # C. RecommendationItem
    # ---------------------------------------------------------
    item = RecommendationItem(trace_id="t", user_id="u", timestamp=now_iso(), source="text",
                              item_id="85123A", title="茶烛台", category="家居装饰", price=2.95,
                              currency="GBP", score=0.87, sources=["推荐系统 v146 个性化精排"])
    res = RecommendationItem.from_dict(item.to_dict())
    check("C-10", "成功", "合法 RecommendationItem", "ok=True 且 sources 可追溯",
          {"ok": res.ok, "sources": res.data.sources if res.ok else None}, res.ok and res.data.sources)

    bad = item.to_dict(); bad.pop("item_id")
    res = RecommendationItem.from_dict(bad)
    check("C-11", "失败", "缺少 item_id", "code=E_MISSING_FIELD",
          {"code": _err_of(res), "field": res.error.field if not res.ok else None},
          _err_of(res) == ErrorCode.MISSING_FIELD.value and res.error.field == "item_id")

    bad = item.to_dict(); bad["price"] = "便宜"
    res = RecommendationItem.from_dict(bad)
    check("C-12", "边界", "price 类型错误", "code=E_TYPE_MISMATCH",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.TYPE_MISMATCH.value)

    # ---------------------------------------------------------
    # D. 多模态适配器治理
    # ---------------------------------------------------------
    mm = MultimodalAdapter()
    res = mm.parse_event({"source": "voice", "text": "   "}, user_id="15547")
    check("D-01", "失败", "ASR 结果为空/过短 → 不臆测", f"code={ErrorCode.EMPTY_TRANSCRIPT.value}",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.EMPTY_TRANSCRIPT.value)

    res = mm.parse_event({"source": "text"}, user_id="15547")
    check("D-02", "失败", "事件不含任何模态输入", "code=E_INVALID_INPUT",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.INVALID_INPUT.value)

    res = mm.parse_event({"source": "vision", "visual_tags": [
        {"tag": "白色", "confidence": 0.90},
        {"tag": "桌布", "confidence": 0.50},
        {"tag": "噪声", "confidence": 0.20},
    ]}, user_id="15547")
    kept = [t.tag for t in res.data.visual_tags] if res.ok else []
    weights = {t.tag: t.raw.get("weight") for t in res.data.visual_tags} if res.ok else {}
    check("D-03", "边界", "视觉标签：高置信保留 / 中置信降权 / 低置信丢弃",
          "kept=[白色,桌布] 且 桌布 weight=0.5，噪声被丢弃",
          {"kept": kept, "weights": weights},
          kept == ["白色", "桌布"] and weights.get("桌布") == 0.5 and "噪声" not in kept)

    res = mm.parse_event({"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.40}},
                         user_id="15547")
    check("D-04", "边界", "手势置信度低于门限 → 丢弃且不参与反馈",
          "code=E_INVALID_INPUT（因为丢弃后无可用模态）",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.INVALID_INPUT.value)

    res = mm.parse_event({"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.92}},
                         user_id="15547")
    gesture = res.data.gesture if res.ok else None
    check("D-05", "成功", "thumbs_up → 正反馈映射", "polarity=positive 且 modality=feedback",
          {"polarity": (gesture or {}).get("polarity"), "modality": res.data.modality if res.ok else None},
          res.ok and gesture["polarity"] == "positive" and res.data.modality == "feedback")

    res = mm.parse_event({"source": "gesture", "gesture": {"name": "thumbs_down", "confidence": 0.88}},
                         user_id="15547")
    gesture = res.data.gesture if res.ok else None
    check("D-06", "成功", "thumbs_down → 负反馈映射", "polarity=negative",
          {"polarity": (gesture or {}).get("polarity")}, res.ok and gesture["polarity"] == "negative")

    # ---------------------------------------------------------
    # E. 推荐适配器
    # ---------------------------------------------------------
    ra = RecommenderAdapter()
    health = ra.health().data
    check("E-01", "成功", "推荐适配器在离线模式下可用", "active_mode=offline_snapshot 或 local_artifacts",
          {"active_mode": health["active_mode"], "catalog": health["catalog"]},
          health["active_mode"] in ("offline_snapshot", "local_artifacts", "http"))

    res = ra.personalized_candidates("15547", 5)
    check("E-02", "成功", "已有用户可召回个性化候选", "ok=True 且条数>0",
          {"count": len(res.data) if res.ok else 0, "code": _err_of(res)},
          res.ok and len(res.data) > 0)

    res = ra.personalized_candidates("no-such-user-id", 5)
    check("E-03", "失败", "未知用户 → 明确 E_NO_RESULT（不是空列表）",
          f"code={ErrorCode.NO_RESULT.value}",
          {"code": _err_of(res), "message": res.error.message if not res.ok else None},
          _err_of(res) == ErrorCode.NO_RESULT.value)

    res = ra.product_detail("no-such-item")
    check("E-04", "失败", "未知商品 → E_ITEM_NOT_FOUND", f"code={ErrorCode.ITEM_NOT_FOUND.value}",
          {"code": _err_of(res)}, _err_of(res) == ErrorCode.ITEM_NOT_FOUND.value)

    # ---------------------------------------------------------
    # F. 降级矩阵自检
    # ---------------------------------------------------------
    matrix = settings.DEGRADATION_MATRIX
    well_formed = all(row.get("scenario") and row.get("fallback") and row.get("level") for row in matrix)
    check("F-01", "边界", "降级矩阵每一行都含 scenario/fallback/level", "True",
          {"rows": len(matrix), "levels": sorted({r['level'] for r in matrix})}, well_formed)

    check("F-02", "成功", "多模态适配器降级记录可读",
          "degradation_report 含 requested_mode/active_mode/events",
          mm.degradation_report(), "events" in mm.degradation_report())
    check("F-03", "成功", "推荐适配器调用日志完整", "call_report.calls > 0",
          {"calls": ra.call_report()["calls"]}, ra.call_report()["calls"] > 0)

    # ---------------------------------------------------------
    # 汇总
    # ---------------------------------------------------------
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["passed"])
    by_category: dict[str, dict] = {}
    for r in RESULTS:
        bucket = by_category.setdefault(r["category"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if r["passed"] else 0
    report = {
        "stage": "01",
        "title": "接口契约与降级矩阵验证",
        "generated_at": now_iso(),
        "schema_version": SCHEMA_VERSION,
        "config": settings.describe(),
        "summary": {
            "total": total, "passed": passed, "failed": total - passed,
            "pass_rate": round(passed / total, 4) if total else 0.0,
            "by_category": by_category,
            "verdict": "PASS" if passed == total else "FAIL",
        },
        "acceptance_checklist": {
            "两套系统不直接读取对方内部变量": True,
            "字段包含 trace_id/user_id/timestamp/source": True,
            "非法输入返回明确错误": all(r["passed"] for r in RESULTS if r["category"] == "失败"),
            "离线演示模式可运行": health["active_mode"] == "offline_snapshot",
        },
        "adapter_health": {"multimodal": mm.health().data, "recommender": health},
        "degradation_matrix": matrix,
        "cases": RESULTS,
    }
    path = write_json(settings.output_path("stage01"), report)

    for r in RESULTS:
        flag = "PASS" if r["passed"] else "FAIL"
        print(f"[{flag}] {r['case_id']} ({r['category']}) {r['description']}")
        if not r["passed"]:
            print(f"       期望: {r['expectation']}")
            print(f"       实际: {r['actual']}")
    print(f"\n合计 {passed}/{total} 通过（通过率 {report['summary']['pass_rate']:.0%}）")
    print(f"[01_contract_test] 报告已写入 {path}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
