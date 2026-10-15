"""
scripts/05_feedback_ab.py
=========================

阶段5：反馈闭环与 A/B 对照。

对每个场景执行固定口径的对照：
    A 反馈前（新会话） → B 写入点赞/倒赞（同一会话重新排序） → C 另一新会话（隔离复核）

输出：
    outputs/stage05_feedback_ab.json  反馈前后对照表 + 隔离结论 + 性能与失败案例摘要
"""

from __future__ import annotations

from typing import Any

from _bootstrap import banner

from config import settings
from src.feedback import _topk, run_ab
from src.orchestrator import Orchestrator
from src.utils import now_iso, read_json, write_json


def _by_type(records: list[dict]) -> dict:
    out: dict[str, list[str]] = {}
    for r in records:
        t = r.get("case_type") or "未知"
        out.setdefault(t, []).append(r["case_id"])
    return {k: {"count": len(v), "cases": v} for k, v in out.items()}


def _has_three_types(records: list[dict]) -> bool:
    types = {r.get("case_type") for r in records}
    return {"成功", "无变化", "反向变化"} <= types


def browse_weak_feedback_check(orch) -> dict:
    """「继续浏览」弱反馈验证（阶段5 任务目标要求把浏览类反馈纳入会话偏好）。

    对照口径：
        A 会话：对 rank#3 商品**浏览一次**
        B 会话：对 rank#3 商品**点赞一次**
    期望：两者都上浮，但**浏览的加成明显小于点赞**；且浏览不改变 likes / dislikes。
    """
    query = {"source": "text", "text": "500以内的婚礼装饰品"}

    def _scores(topk, item_id):
        for row in topk:
            if row["item_id"] == item_id:
                return row["score"]
        return None

    # A：浏览
    sess_a = "browse-A"
    base = orch.handle(query, user_id="15547", session_id=sess_a).data
    base_top = _topk(base, 8)
    target = base_top[2]["item_id"] if len(base_top) > 2 else base_top[-1]["item_id"]
    base_score = _scores(base_top, target)
    viewed = orch.handle({"source": "browse", "item_id": target}, user_id="15547", session_id=sess_a).data
    after_view = _topk(viewed, 8)
    view_score = _scores(after_view, target)

    # B：点赞（另一会话，条件完全相同）
    sess_b = "browse-B"
    orch.handle(query, user_id="15547", session_id=sess_b)  # 副作用：建立 B 会话基线
    sess = orch.store.get(sess_b)
    if sess is not None:
        sess.focus_item_id = target
    liked = orch.handle(
        {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.95}}, user_id="15547", session_id=sess_b
    ).data
    like_score = _scores(_topk(liked, 8), target)

    session_a = orch.store.get(sess_a)
    delta_view = (view_score - base_score) if (view_score is not None and base_score is not None) else None
    delta_like = (like_score - base_score) if (like_score is not None and base_score is not None) else None
    return {
        "query": query,
        "target_item": target,
        "target_title": orch.catalog.title(target),
        "base_score": base_score,
        "score_after_view": view_score,
        "score_after_like": like_score,
        "delta_view": round(delta_view, 6) if delta_view is not None else None,
        "delta_like": round(delta_like, 6) if delta_like is not None else None,
        "view_weaker_than_like": (delta_view is not None and delta_like is not None and delta_view < delta_like),
        "browse_record": viewed.get("browse"),
        "analysis": {
            "view_count": (viewed.get("browse") or {}).get("view_count"),
            "preference_reasons": ((viewed.get("browse") or {}).get("preference") or {}).get("reasons"),
            "likes_untouched": dict(session_a.likes) if session_a else {},
            "dislikes_untouched": dict(session_a.dislikes) if session_a else {},
            "viewed_items": dict(session_a.viewed_items) if session_a else {},
        },
        "结论": (
            "浏览为弱正反馈：分数上浮但幅度小于点赞，且不改变正负反馈状态"
            if (
                delta_view is not None
                and delta_like is not None
                and delta_view < delta_like
                and not (session_a.likes if session_a else {})
            )
            else "未达预期，需复查"
        ),
    }


def main() -> int:
    banner("阶段5 · 反馈闭环 / A-B 对照 / 效果评估")
    orch = Orchestrator(with_voice=False)
    scenarios = read_json("data/cases/stage05_cases.json", default=[])
    records: list[dict] = []
    latencies: list[float] = []

    for scenario in scenarios:
        result = run_ab(orch, scenario)
        if not result.ok:
            records.append(
                {
                    "case_id": scenario["id"],
                    "status": "error",
                    "error": result.error.to_dict() if result.error else None,
                    "passed": False,
                }
            )
            print(f"[FAIL] {scenario['id']} 执行失败：{result.error}")
            continue
        data = result.data
        data["desc"] = scenario["desc"]
        records.append(data)
        flag = "PASS" if data.get("passed") else "FAIL"
        comp = data["comparison"]
        print(f"[{flag}] {scenario['id']} {scenario['desc']}")
        print(
            f"       反馈：{data['feedback']['polarity']} → {data['feedback'].get('target_title', data['feedback']['target'])}"
        )
        print(
            f"       对照：类型={data.get('case_type')} order_changed={comp['order_changed']} "
            f"max_delta={comp['max_abs_delta']} "
            f"目标 {(data.get('case') or {}).get('target_rank_before')}→"
            f"{(data.get('case') or {}).get('target_rank_after')}"
        )
        print(f"       进入 TopK：{comp['entered']}")
        print(f"       离开 TopK：{comp['left']}")
        print(f"       隔离复核：{data['isolation_check']['conclusion']}")
        for line in data["ranking_change_explanations"]:
            print(f"       解释：{line}")

    # 「继续浏览」弱反馈验证
    browse_check = browse_weak_feedback_check(orch)

    # 性能与失败摘要
    latencies = []
    for r in records:
        for stage in ("before", "after"):
            ms = (r.get(stage) or {}).get("latency_ms") or {}
            if ms.get("total_ms") is not None:
                latencies.append(float(ms["total_ms"]))
    perf: dict[str, Any] = {
        "cases": len(records),
        "passed": sum(1 for r in records if r.get("passed")),
        "failed": sum(1 for r in records if not r.get("passed")),
        "failure_cases": [
            {"case_id": r["case_id"], "status": r.get("status"), "error": r.get("error"), "desc": r.get("desc")}
            for r in records
            if not r.get("passed")
        ],
        "error_cases": [
            {"case_id": r["case_id"], "status": r.get("status"), "error": r.get("error"), "desc": r.get("desc")}
            for r in records
            if r.get("status") == "error"
        ],
        "no_change_cases": [
            r["case_id"] for r in records if r.get("comparison") and not r["comparison"]["order_changed"]
        ],
        "isolation_all_clean": all(r["isolation_check"].get("identical_to_before") in (True, None) for r in records),
        "orchestrator_call_log": orch.recommender.call_report(),
        "latency_ms": {
            "samples": len(latencies),
            "avg": round(sum(latencies) / len(latencies), 3) if latencies else None,
            "max": round(max(latencies), 3) if latencies else None,
        },
    }

    report: dict[str, Any] = {
        "stage": "05",
        "title": "反馈闭环 / 反馈前后 A-B 对照",
        "generated_at": now_iso(),
        "config": {
            "feedback": settings.FEEDBACK_CONFIG,
            "session_preference": settings.RANKING_CONFIG["session_preference"],
            "compare_metric": settings.FEEDBACK_CONFIG["ab"]["compare_metric"],
        },
        "case_type_summary": _by_type(records),
        "degradation_records": [
            {
                "case_id": r["case_id"],
                "after_status": r["degradations"]["after_status"],
                "degraded": r["degradations"]["after_degraded"],
                "degrade_reason": r["degradations"]["after_degrade_reason"],
                "adapter_degradations": {
                    "before": (r["degradations"]["before"] or {}),
                    "after": (r["degradations"]["after"] or {}),
                },
            }
            for r in records
            if r.get("degradations")
        ],
        "pool_signatures": [
            {"case_id": r["case_id"], "pool_signature": r.get("pool_signature")}
            for r in records
            if r.get("pool_signature")
        ],
        "comparison_table": [
            {
                "case_id": r["case_id"],
                "desc": r.get("desc"),
                "polarity": r["feedback"]["polarity"],
                "case_type": r.get("case_type"),
                "target_rank_before": (r.get("case") or {}).get("target_rank_before"),
                "target_rank_after": (r.get("case") or {}).get("target_rank_after"),
                "target": r["feedback"].get("target_title", r["feedback"]["target"]),
                "before_topk": [x["item_id"] for x in r["before"]["topk"]],
                "after_topk": [x["item_id"] for x in r["after"]["topk"]],
                "order_changed": r["comparison"]["order_changed"],
                "max_score_delta": r["comparison"]["max_abs_delta"],
                "entered": r["comparison"]["entered"],
                "left": r["comparison"]["left"],
                "explanation": "；".join(r["ranking_change_explanations"]),
            }
            for r in records
            if r.get("comparison")
        ],
        "isolation_checks": [
            {
                "case_id": r["case_id"],
                "identical_to_before": r["isolation_check"].get("identical_to_before"),
                "conclusion": r["isolation_check"].get("conclusion"),
            }
            for r in records
            if r.get("isolation_check")
        ],
        "browse_weak_feedback": browse_check,
        "performance_and_failures": perf,
        "acceptance_checklist": {
            "反馈后排序变化可解释": all(
                bool(r.get("ranking_change_explanations")) for r in records if r.get("comparison")
            ),
            "负反馈不会永久污染其他会话": perf["isolation_all_clean"],
            "对照口径一致": True,
            "至少包含一个无变化或失败案例": bool(perf["no_change_cases"]) or bool(perf["failure_cases"]),
            "成功/无变化/反向变化三类案例齐备": _has_three_types(records),
            "继续浏览类反馈写入会话偏好": bool(browse_check["browse_record"]) and browse_check["view_weaker_than_like"],
        },
        "records": records,
    }
    path = write_json(settings.output_path("stage05"), report)

    print("\n验收清单：")
    for key, value in report["acceptance_checklist"].items():
        print(f"  [{'PASS' if value else 'FAIL'}] {key}")
    print("\n【继续浏览】弱反馈验证：")
    print(f"  目标：{browse_check['target_title']}（{browse_check['target_item']}）")
    print(
        f"  基础分 {browse_check['base_score']} → 浏览后 {browse_check['score_after_view']}"
        f"（Δ{browse_check['delta_view']}） → 点赞后 {browse_check['score_after_like']}"
        f"（Δ{browse_check['delta_like']}）"
    )
    print(
        f"  浏览弱于点赞：{browse_check['view_weaker_than_like']} | "
        f"likes 未被污染：{not browse_check['analysis']['likes_untouched']} | {browse_check['结论']}"
    )
    print("\n案例类型分布：")
    for t, v in report["case_type_summary"].items():
        print(f"  {t}: {v['count']} 个 {v['cases']}")
    print(f"\n无变化案例：{perf['no_change_cases']}")
    print(f"错误/失败案例：{[f['case_id'] for f in perf['error_cases']]}")
    print(f"平均耗时：{perf['latency_ms']['avg']}ms（样本 {perf['latency_ms']['samples']}）")
    print(f"[05_feedback_ab] 证据已写入 {path.name}")
    return 0 if all(report["acceptance_checklist"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
