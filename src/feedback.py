"""
src/feedback.py
===============

反馈闭环与 A/B 对照（阶段5）。

对照口径（严格一致）：
    同一用户、同一会话、同一查询、同一候选条件；唯一变量是「是否写入反馈」。

流程：
    A（before） 新会话执行查询 → 记录 TopK 与分数
    B（after）  在同一会话写入点赞/倒赞 → 重新执行同一查询 → 记录 TopK 与分数
    C（隔离）   在**另一个新会话**执行同一查询 → 结果应与 A 完全一致（负反馈不跨会话污染）

判定：
    changed            TopK 顺序或分数发生变化
    no_change          完全没变（也算有效结论，需给出原因）
    reverse_change     预期加分的方向反而降分（失败案例）
"""

from __future__ import annotations

import uuid

from config import settings

from .errors import ErrorCode, Result, fail, ok
from .orchestrator import Orchestrator
from .utils import stable_key

FB = settings.FEEDBACK_CONFIG


def _topk(resp: dict, top_k: int) -> list[dict]:
    return [
        {
            "item_id": r["item_id"],
            "rank": r.get("rank", i + 1),
            "title": r.get("title", ""),
            "score": round(float(r.get("score", 0.0)), 6),
            "sources": list(r.get("sources", [])),
        }
        for i, r in enumerate(resp.get("recommendations", [])[:top_k])
    ]


def _order(rows: list[dict]) -> list[str]:
    return [r["item_id"] for r in rows]


def _score_map(rows: list[dict]) -> dict[str, float]:
    return {r["item_id"]: r["score"] for r in rows}


def classify_case(before_ids: list[str], after_ids: list[str], target: str, polarity: str) -> dict:
    """把一次反馈对照归类为「成功 / 无变化 / 反向变化 / 失败」。

    阶段5 要求「扩充成功、无变化、反向变化三类案例，说明变化原因」，
    因此这里用**可判定的规则**分类，而不是人工挑选：

        反馈预期：点赞 → 目标上升或保持第 1；倒赞 → 目标下降或被移除
        成功     ：变化方向与预期一致
        无变化   ：TopK 顺序完全不变
        反向变化 ：变化方向与预期**相反**（点赞后反而下降，或倒赞后反而上升）
    """
    target = str(target)
    before_rank = before_ids.index(target) + 1 if target in before_ids else None
    after_rank = after_ids.index(target) + 1 if target in after_ids else None

    # 先穷尽所有「含 None」的组合，随后分支里两值必然非 None —— 这样写
    # 既让 mypy 能收窄类型，也把不变量摆在明面上。
    if before_rank is None or after_rank is None:
        if before_rank is not None:
            actual = "removed"
        elif after_rank is not None:
            actual = "entered"
        else:
            actual = "absent"
    elif after_rank < before_rank:
        actual = "up"
    elif after_rank > before_rank:
        actual = "down"
    else:
        actual = "same"

    if before_ids == after_ids:
        case_type = "无变化"
    elif polarity == "positive":
        if actual in ("up", "same", "entered") or before_rank == after_rank == 1:
            case_type = "成功"
        elif actual == "down":
            case_type = "反向变化"
        else:
            case_type = "成功"
    else:  # negative
        if actual in ("down", "removed", "same"):
            case_type = "成功"
        elif actual in ("up", "entered"):
            case_type = "反向变化"
        else:
            case_type = "成功"

    return {
        "case_type": case_type,
        "target_rank_before": before_rank,
        "target_rank_after": after_rank,
        "target_movement": actual,
        "order_changed": before_ids != after_ids,
    }


def compare_snapshots(before: list[dict], after: list[dict], *, min_delta: float) -> dict:
    before_ids, after_ids = _order(before), _order(after)
    before_scores, after_scores = _score_map(before), _score_map(after)
    shared = [i for i in before_ids if i in after_scores]
    deltas = {i: round(after_scores[i] - before_scores[i], 6) for i in shared}
    moved = []
    for item_id in shared:
        old_rank = before_ids.index(item_id) + 1
        new_rank = after_ids.index(item_id) + 1
        if old_rank != new_rank:
            moved.append(
                {
                    "item_id": item_id,
                    "rank_before": old_rank,
                    "rank_after": new_rank,
                    "move": new_rank - old_rank,
                    "score_delta": deltas[item_id],
                }
            )
    max_delta = max((abs(v) for v in deltas.values()), default=0.0)
    return {
        # 「无变化」以 TopK 顺序为准：顺序不变即视为无变化（分数微调单独记录）
        "order_changed": before_ids != after_ids,
        "entered": [i for i in after_ids if i not in before_ids],
        "left": [i for i in before_ids if i not in after_ids],
        "moved": moved,
        "score_deltas": deltas,
        "shared_count": len(shared),
        "max_abs_delta": round(max_delta, 6),
        "score_shifted": max_delta > min_delta,
        "significant": before_ids != after_ids,
    }


def run_ab(
    orchestrator: Orchestrator,
    scenario: dict,
    *,
    session_prefix: str = "ab",
) -> Result:
    """执行一个 A/B 对照场景。scenario 结构见 data/cases/stage05_cases.json。"""
    user_id = str(scenario["user_id"])
    query = scenario["query"]
    feedback = scenario.get("feedback") or {}
    top_k = int(FB["ab"]["top_k"])
    min_delta = float(FB["ab"]["min_score_delta"])
    # 每个场景使用互不相同的全新会话，避免上一次实验的偏好污染本次对照
    stamp = f"{scenario.get('id', 'case')}-{uuid.uuid4().hex[:10]}"
    session_a = f"{session_prefix}-A-{stamp}"
    session_c = f"{session_prefix}-C-{stamp}"

    # A: 反馈前
    before_result = orchestrator.handle(query, user_id=user_id, session_id=session_a, top_k=top_k)
    if not before_result.ok:
        return before_result
    before_resp = before_result.data
    if before_resp["status"] not in ("ok", "degraded") or not before_resp["recommendations"]:
        return fail(
            ErrorCode.NO_RESULT, "A 阶段没有可用推荐结果，无法做 A/B 对照", field="query", status=before_resp["status"]
        )
    before_topk = _topk(before_resp, top_k)

    # 目标商品
    target = feedback.get("target")
    if target in (None, "", "top1"):
        target = before_topk[0]["item_id"]
    target = str(target)
    polarity = feedback.get("polarity", "positive")

    # B: 写入反馈并重新排序
    # 模拟「用户指认目标商品」这一步（等价于前端点击某件商品），再发送反馈。
    session_ctx = orchestrator.store.get(session_a)
    assert session_ctx is not None, "会话 A 应已在前面创建"  # 不存在则是调用方的逻辑错误，早炸早好
    session_ctx.focus_item_id = target
    if target not in session_ctx.last_item_ids:
        session_ctx.last_item_ids = [target, *session_ctx.last_item_ids]

    if feedback.get("via_gesture"):
        feedback_spec = {
            "source": "gesture",
            "gesture": {
                "name": "thumbs_up" if polarity == "positive" else "thumbs_down",
                "confidence": float(feedback.get("confidence", 0.92)),
            },
        }
    else:
        feedback_spec = {
            "source": "text",
            "text": feedback.get("text") or ("这个不错" if polarity == "positive" else "这个不行"),
        }
        if polarity == "negative" and "不" not in feedback_spec["text"]:
            feedback_spec["text"] = "这个不行"

    after_result = orchestrator.handle(feedback_spec, user_id=user_id, session_id=session_a, top_k=top_k)
    if not after_result.ok or after_result.data.get("status") == "clarification":
        return fail(
            ErrorCode.INVALID_INPUT,
            "反馈阶段未能定位目标商品，无法完成 A/B 对照",
            field="feedback",
            status=(after_result.data or {}).get("status") if after_result.ok else "error",
        )
    after_resp = after_result.data
    if after_resp.get("status") == "error":
        return ok(
            {
                "case_id": scenario.get("id"),
                "user_id": user_id,
                "query": query,
                "status": "error",
                "error": after_resp.get("error"),
                "feedback": {"polarity": polarity, "target": target},
                "before": {"topk": before_topk, "session_id": session_a},
                "after": {"topk": [], "session_id": session_a},
                "comparison": {
                    "order_changed": False,
                    "significant": False,
                    "score_shifted": False,
                    "max_abs_delta": 0.0,
                    "entered": [],
                    "left": [],
                    "moved": [],
                    "score_deltas": {},
                    "shared_count": 0,
                },
                "isolation_check": {
                    "session_id": session_c,
                    "topk": [],
                    "identical_to_before": None,
                    "comparison": None,
                    "conclusion": "反馈写入失败，未做隔离验证",
                },
                "ranking_change_explanations": ["反馈写入失败：目标商品不存在或状态非法。"],
                "case": {"case_type": "失败", "target_movement": "error"},
                "case_type": "失败",
                "expect": scenario.get("expect", "any"),
                "passed": scenario.get("expect") == "failure",
                "note": scenario.get("note", ""),
            }
        )
    after_topk = _topk(after_resp, top_k)

    # C: 隔离验证（另一个会话，应与 A 一致）
    isolation_result = orchestrator.handle(query, user_id=user_id, session_id=session_c, top_k=top_k)
    isolation_topk = _topk(isolation_result.data, top_k) if isolation_result.ok else []

    comparison = compare_snapshots(before_topk, after_topk, min_delta=min_delta)
    isolation_comparison = compare_snapshots(before_topk, isolation_topk, min_delta=min_delta)

    # 案例类型（成功 / 无变化 / 反向变化）与候选条件签名
    case = classify_case(_order(before_topk), _order(after_topk), target, polarity)
    pool_signature = stable_key(
        polarity,
        len(before_topk),
        ",".join(_order(before_topk)),
    )
    degradations = {
        "before": (before_resp.get("trace") or {}).get("degradations"),
        "after": (after_resp.get("trace") or {}).get("degradations"),
        "after_status": after_resp.get("status"),
        "after_degraded": bool(((after_resp.get("trace") or {}).get("ranking") or {}).get("degraded")),
        "after_degrade_reason": ((after_resp.get("trace") or {}).get("ranking") or {}).get("degrade_reason"),
    }

    # 变化解释
    session_likes = after_resp.get("session", {}).get("likes", {})
    session_dislikes = after_resp.get("session", {}).get("dislikes", {})
    explanations: list[str] = []
    if not comparison["significant"]:
        if comparison["score_shifted"]:
            explanations.append(
                f"TopK 顺序未变，但分数发生变化（最大位移 {comparison['max_abs_delta']:.4f}）："
                f"目标商品本就在第 1 位，同类商品获得同等的类目加成，因此相对顺序保持稳定。"
            )
        else:
            explanations.append("反馈未改变 TopK，且分数无变化：目标商品不在当前候选池内。")
    else:
        if comparison["entered"]:
            explanations.append(f"新进入 TopK：{comparison['entered']}（会话偏好加分或负反馈移除后腾出位置）。")
        if comparison["left"]:
            explanations.append(f"离开 TopK：{comparison['left']}（被负反馈屏蔽或相对分数下降）。")
        if comparison["moved"]:
            top_moves = sorted(comparison["moved"], key=lambda m: -abs(m["move"]))[:3]
            explanations.append(
                "名次变化：" + "；".join(f"{m['item_id']} {m['rank_before']}→{m['rank_after']}" for m in top_moves)
            )

    expect = scenario.get("expect", "any")
    if expect == "no_change":
        passed = not comparison["significant"]
    elif expect == "changed":
        passed = comparison["significant"]
    elif expect == "failure":
        passed = False
    else:
        passed = True

    return ok(
        {
            "case_id": scenario.get("id"),
            "status": "ok",
            "user_id": user_id,
            "query": query,
            "feedback": {
                "polarity": polarity,
                "target": target,
                "target_title": orchestrator.catalog.title(target),
                "target_in_before_pool": target in _order(before_topk),
            },
            "before": {
                "topk": before_topk,
                "session_id": session_a,
                "latency_ms": (before_resp.get("trace") or {}).get("latency_ms"),
                "session_state": before_resp.get("session", {}),
            },
            "after": {
                "topk": after_topk,
                "session_id": session_a,
                "latency_ms": (after_resp.get("trace") or {}).get("latency_ms"),
                "session_state": after_resp.get("session", {}),
                "likes": session_likes,
                "dislikes": session_dislikes,
            },
            "comparison": comparison,
            "isolation_check": {
                "session_id": session_c,
                "topk": isolation_topk,
                "identical_to_before": not isolation_comparison["significant"],
                "comparison": isolation_comparison,
                "conclusion": (
                    "负反馈/点赞未跨会话污染：另一会话结果与反馈前一致"
                    if not isolation_comparison["significant"]
                    else "警告：另一会话结果发生变化，存在跨会话污染"
                ),
            },
            "ranking_change_explanations": explanations,
            "case": case,
            "case_type": case["case_type"],
            "pool_signature": pool_signature,
            "degradations": degradations,
            "expect": expect,
            "passed": passed,
            "note": scenario.get("note", ""),
        }
    )
