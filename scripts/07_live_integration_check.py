"""
scripts/07_live_integration_check.py
====================================

在线联调验证（可选）：当外部「推荐系统 v146」前端正在运行时，
验证集成层的 **HTTP 适配器** 能真实对接其对外接口。

行为：
    - 服务可达 + 已配置演示账号环境变量 → 以 auto 模式跑通三轮主链路，
      并把 HTTP 结果与内置快照结果做同一查询的对照；
    - 服务不可达 / 未配置账号 → 记录「跳过」并正常退出（不破坏离线可复现性）。

账号来源：仅环境变量 `INTEGRATED_DEMO_USER` / `INTEGRATED_DEMO_PASSWORD`，
本脚本与配置文件都不含任何密钥。

输出：outputs/live_integration.json
"""

from __future__ import annotations

import os

from _bootstrap import banner

from config import settings
from src.adapters import RecommenderAdapter
from src.orchestrator import Orchestrator
from src.utils import now_iso, write_json


def _probe(base_url: str, timeout: float) -> dict:
    try:
        import requests

        resp = requests.get(base_url + "/", timeout=timeout)
        return {"reachable": resp.status_code == 200, "status_code": resp.status_code}
    except Exception as exc:
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    banner("在线联调验证 · HTTP 适配器对接推荐系统")

    http_cfg = settings.RECOMMENDER_ADAPTER["http"]
    base_url = http_cfg["base_url"].rstrip("/")
    probe = _probe(base_url, http_cfg["timeout_s"])
    user = os.getenv(http_cfg["username_env"])
    password = os.getenv(http_cfg["password_env"])

    report = {
        "checked_at": now_iso(),
        "base_url": base_url,
        "probe": probe,
        "credentials_configured": bool(user and password),
        "credential_env_names": [http_cfg["username_env"], http_cfg["password_env"]],
        "mode": "skipped",
        "results": [],
    }

    if not probe.get("reachable"):
        report["reason"] = "推荐系统前端未运行；离线链路（local_artifacts / offline_snapshot）不受影响"
        print(f"跳过：{base_url} 不可达 —— {report['reason']}")
        path = write_json(settings.PATHS["outputs"] / "live_integration.json", report)
        print(f"[07_live_integration_check] 证据已写入 {path}")
        return 0

    if not report["credentials_configured"]:
        report["reason"] = f"服务可达，但未设置 {http_cfg['username_env']} / {http_cfg['password_env']}，跳过 HTTP 登录"
        print(f"跳过：{report['reason']}")
        path = write_json(settings.PATHS["outputs"] / "live_integration.json", report)
        print(f"[07_live_integration_check] 证据已写入 {path}")
        return 0

    report["mode"] = "http"

    # 适配器层验证
    adapter = RecommenderAdapter(mode="auto")
    report["adapter"] = {
        "active_mode": adapter.active_mode,
        "degradations": adapter.degradation_report()["events"],
        "health": {k: v for k, v in adapter.health().data.items() if k != "degradations"},
    }
    http_result = adapter.personalized_candidates(user, 8)
    report["http_candidates"] = {
        "ok": http_result.ok,
        "count": len(http_result.data) if http_result.ok else 0,
        "sample": [
            {"item_id": r["item_id"], "score": r["score"], "reason": r["detail"].get("reason")}
            for r in (http_result.data or [])[:5]
        ],
        "error": http_result.error.to_dict() if http_result.error else None,
    }
    print(f"适配器 active_mode={adapter.active_mode}，HTTP 候选 {report['http_candidates']['count']} 条")

    # 端到端三轮（HTTP 模式）
    orch_http = Orchestrator(mode="auto", with_voice=False)
    turns = [
        ("voice", {"source": "voice", "text": "我要给朋友挑一个结婚礼物，500以内"}),
        ("gesture", {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.92}}),
        ("text", {"source": "text", "text": "为什么推荐第一个"}),
    ]
    session_id = "live-http-session"
    for label, spec in turns:
        result = orch_http.handle(spec, user_id=user, session_id=session_id)
        if not result.ok:
            report["results"].append(
                {"turn": label, "status": "error", "error": result.error.to_dict() if result.error else None}
            )
            continue
        resp = result.data
        report["results"].append(
            {
                "turn": label,
                "trace_id": resp.get("trace_id"),
                "status": resp.get("status"),
                "intent": (resp.get("intent_result") or {}).get("intent"),
                "top": [
                    {
                        "rank": r.get("rank"),
                        "item_id": r.get("item_id"),
                        "title": r.get("title"),
                        "price": r.get("price"),
                        "score": r.get("score"),
                        "sources": r.get("sources"),
                    }
                    for r in resp.get("recommendations", [])[:3]
                ],
                "recall_sources": [
                    {"source": s["source"], "status": s["status"], "count": s["count"]}
                    for s in ((resp.get("trace") or {}).get("recall") or {}).get("sources", [])
                ],
                "latency_ms": (resp.get("trace") or {}).get("latency_ms"),
            }
        )
        print(f"  [{label}] status={resp.get('status')} top1={(resp.get('recommendations') or [{}])[0].get('title')}")

    # 同一查询：HTTP 模式 vs 内置快照模式的对照
    snapshot_orch = Orchestrator(mode="offline", with_voice=False)
    query = {"source": "text", "text": "推荐一些家居装饰"}
    http_resp = orch_http.handle(query, user_id=user, session_id="live-cmp-http").data
    snap_resp = snapshot_orch.handle(query, user_id=user, session_id="live-cmp-snap").data
    report["comparison"] = {
        "query": query,
        "http": {
            "mode": http_resp["trace"]["modes"]["recommender"],
            "top5": [r["item_id"] for r in http_resp["recommendations"][:5]],
        },
        "snapshot": {
            "mode": snap_resp["trace"]["modes"]["recommender"],
            "top5": [r["item_id"] for r in snap_resp["recommendations"][:5]],
        },
        "note": "两种模式的召回来源与分数口径不同（在线实时接口 vs 离线快照），"
        "TopK 不完全一致属预期；两者共用同一套排序与解释逻辑。",
    }
    print(f"对照：http={report['comparison']['http']['top5']}")
    print(f"      snapshot={report['comparison']['snapshot']['top5']}")

    path = write_json(settings.PATHS["outputs"] / "live_integration.json", report)
    print(f"[07_live_integration_check] 证据已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
