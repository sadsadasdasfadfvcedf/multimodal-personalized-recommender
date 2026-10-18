"""
scripts/18_contract_check.py
============================

网页 API 契约核验（对应 docs/protocol.md §10）。

contract-first 的核心要求：**provider 的真实响应必须对着同一份边界契约验证**，
而不是「两边各自跑通自己的测试」。本脚本对运行中的 webui 逐个端点发请求，
校验：

    - 成功响应的必填字段
    - 失败响应必须带 ok/error/code/field
    - /api/chat 的 response 必须满足 §7 的统一信封
    - 未知路径 / 未知接口 / 参数错误 / 商品不存在 都要结构化

前置条件：webui.py 正在运行（默认 http://127.0.0.1:8020）
输出：outputs/contract_check.json
"""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

from _bootstrap import banner

from config import settings
from src.utils import now_iso, write_json

ENVELOPE_REQUIRED = (
    "schema_version",
    "trace_id",
    "user_id",
    "timestamp",
    "source",
    "session_id",
    "status",
    "event",
    "recommendations",
    "error",
    "trace",
    "session",
)
ITEM_REQUIRED = ("item_id", "title", "price", "score", "rank", "sources", "explanation")


def _call(base: str, path: str, body=None, method: str = "POST", timeout: float = 180.0):
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(
        base + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            if "json" not in ctype:
                return resp.status, {"_bytes": len(raw), "_ctype": ctype}
            return resp.status, json.loads(raw.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, {"_raw": raw[:200].decode("utf-8", "replace")}


def main() -> int:
    banner("网页 API 契约核验（protocol §10）")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    base = args.url.rstrip("/")

    report: dict = {"checked_at": now_iso(), "url": base, "checks": []}

    def add(no: int, name: str, ok: bool, detail: dict) -> None:
        report["checks"].append({"no": no, "check": name, "passed": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {no}. {name} —— {json.dumps(detail, ensure_ascii=False)[:160]}")

    # 0) 可达 + 关闭真实 LLM（否则 chat 会加载模型，太慢）
    try:
        status, info = _call(base, "/api/info", method="GET", timeout=8)
    except Exception as exc:
        report.update({"mode": "skipped", "reason": f"webui 不可达: {exc}"})
        path = write_json(settings.PATHS["outputs"] / "contract_check.json", report)
        print("跳过：webui 不可达")
        print("[18_contract_check] 证据已写入 " + path.name)
        return 0

    if info.get("llm"):
        _call(base, "/api/llm")
        status, info = _call(base, "/api/info", method="GET", timeout=8)
    add(
        1,
        "/api/info 字段",
        status == 200 and all(k in info for k in ("ok", "user_id", "session_id", "modes", "voice", "llm")),
        {"status": status, "keys": sorted(info)},
    )

    # 2) /api/home
    status, home = _call(base, "/api/home", method="GET", timeout=30)
    items = home.get("items") or []
    add(
        2,
        "/api/home 字段",
        status == 200 and home.get("ok") is True and isinstance(items, list) and bool(items),
        {"status": status, "n": len(items), "keys": sorted(home)},
    )

    # 3) /api/chat 成功信封
    status, chat = _call(base, "/api/chat", {"text": "300以内的水杯"}, timeout=180)
    resp = chat.get("response") or {}
    missing = [k for k in ENVELOPE_REQUIRED if k not in resp]
    recs = resp.get("recommendations") or []
    item_missing = [k for k in ITEM_REQUIRED if k not in (recs[0] if recs else {})]
    add(
        3,
        "/api/chat 成功信封",
        bool(status == 200 and chat.get("ok") is True and not missing and recs and not item_missing),
        {
            "status": status,
            "resp_status": resp.get("status"),
            "recs": len(recs),
            "missing": missing,
            "item_missing": item_missing,
        },
    )

    # 4) /api/chat 空输入 → 结构化错误
    status, chat2 = _call(base, "/api/chat", {"text": ""}, timeout=60)
    r2 = chat2.get("response") or {}
    e2 = r2.get("error") or {}
    add(
        4,
        "/api/chat 空输入错误结构",
        status == 200 and r2.get("status") == "error" and e2.get("code") == "E_EMPTY_TRANSCRIPT" and "field" in e2,
        {"status": status, "resp_status": r2.get("status"), "code": e2.get("code"), "field": e2.get("field")},
    )

    # 5/6) 商品详情：命中 + 未命中
    valid_id = str(items[0]["item_id"])
    status, prod = _call(base, f"/api/product/{valid_id}", method="GET", timeout=60)
    p = prod.get("product") or {}
    add(
        5,
        "/api/product 命中",
        status == 200 and prod.get("ok") is True and "item_id" in p and isinstance(prod.get("similar"), list),
        {"status": status, "keys": sorted(p)},
    )
    status, miss = _call(base, "/api/product/__nope__", method="GET", timeout=30)
    add(
        6,
        "/api/product 未命中结构化",
        miss.get("ok") is False and miss.get("code") == "E_ITEM_NOT_FOUND" and "field" in miss,
        {"status": status, "code": miss.get("code"), "field": miss.get("field")},
    )

    # 7) 批量商品
    status, batch = _call(base, "/api/products", {"item_ids": [str(it["item_id"]) for it in items[:2]]}, timeout=60)
    add(
        7,
        "/api/products",
        status == 200 and batch.get("ok") is True and len(batch.get("products") or []) == min(2, len(items)),
        {"status": status, "n": len(batch.get("products") or [])},
    )

    # 8) 反馈
    status, fb = _call(base, "/api/feedback", {"item_id": valid_id, "polarity": "up"}, timeout=180)
    add(
        8,
        "/api/feedback",
        status == 200 and fb.get("ok") is True and (fb.get("response") or {}).get("status") in ("ok", "degraded"),
        {"status": status, "resp_status": (fb.get("response") or {}).get("status")},
    )

    # 9/10) undo / state
    status, undo = _call(base, "/api/undo", timeout=60)
    add(9, "/api/undo", status == 200 and undo.get("ok") is True and "response" in undo, {"status": status})
    status, st = _call(base, "/api/state", timeout=30)
    add(
        10,
        "/api/state",
        status == 200 and st.get("ok") is True and isinstance(st.get("state"), dict),
        {"status": status},
    )

    # 11) 未知路径 / 未知接口
    sg, ug = _call(base, "/api/not-a-route", method="GET", timeout=15)
    sp, up = _call(base, "/api/not-a-route", {}, timeout=15)
    ok_get = sg == 404 and ug.get("ok") is False and ug.get("code") and "field" in ug
    ok_post = sp == 404 and up.get("ok") is False and up.get("code") and "field" in up
    add(11, "未知路径 404 结构化", ok_get and ok_post, {"get": [sg, ug], "post": [sp, up]})

    # 12) 图片：200 或结构化 404
    si, img = _call(base, f"/api/img/{valid_id}", method="GET", timeout=60)
    ok_img = si == 200 or (si == 404 and img.get("code") == "E_ITEM_NOT_FOUND")
    add(12, "/api/img 响应", ok_img, {"status": si, "keys": sorted(img)})

    # 13) 音频缺失 → 结构化 404
    sa, au = _call(base, "/audio/__missing__.wav", method="GET", timeout=15)
    add(
        13,
        "/audio 未命中结构化",
        sa == 404 and au.get("code") == "E_ITEM_NOT_FOUND",
        {"status": sa, "code": au.get("code")},
    )

    # 14) 参数类型错误
    sb, bad = _call(base, "/api/products", {"item_ids": "not-a-list"}, timeout=15)
    add(
        14,
        "/api/products 参数校验",
        sb == 400 and bad.get("ok") is False and bad.get("code") == "E_INVALID_INPUT",
        {"status": sb, "code": bad.get("code"), "field": bad.get("field")},
    )

    # 15) /api/user 切换账号（切换后恢复默认用户）
    s15, sw = _call(base, "/api/user", {"user_id": "14156"}, timeout=30)
    b15 = _call(base, "/api/user", {"user_id": "__nope__"}, timeout=15)[1]
    restored = _call(base, "/api/user", {"user_id": "15547"}, timeout=30)[1]
    ok15 = (
        s15 == 200
        and sw.get("ok") is True
        and (sw.get("account") or {}).get("name") == "陈小姐"
        and b15.get("code") == "E_INVALID_INPUT"
        and restored.get("ok") is True
    )
    add(
        15,
        "/api/user 切换账号",
        ok15,
        {
            "status": s15,
            "account": (sw.get("account") or {}).get("name"),
            "bad_code": b15.get("code"),
            "restored": restored.get("ok"),
        },
    )

    # 16) 寒暄/身份问答：直接回答，不返回商品
    s16, chat3 = _call(base, "/api/chat", {"text": "你是谁"}, timeout=60)
    r16 = chat3.get("response") or {}
    intent16 = (r16.get("intent_result") or {}).get("intent")
    ok16 = (
        s16 == 200
        and r16.get("status") == "ok"
        and bool(r16.get("answer"))
        and not (r16.get("recommendations") or [])
        and intent16 == "chitchat"
    )
    add(
        16,
        "寒暄/身份问答",
        ok16,
        {
            "status": s16,
            "intent": intent16,
            "answer_len": len(r16.get("answer") or ""),
            "recs": len(r16.get("recommendations") or []),
        },
    )

    passed = sum(1 for c in report["checks"] if c["passed"])
    report["summary"] = {
        "total": len(report["checks"]),
        "passed": passed,
        "failed": len(report["checks"]) - passed,
        "verdict": "PASS" if passed == len(report["checks"]) else "FAIL",
    }
    path = write_json(settings.PATHS["outputs"] / "contract_check.json", report)
    print(f"\n合计 {passed}/{len(report['checks'])} 通过")
    print("[18_contract_check] 证据已写入 " + path.name)
    return 0 if passed == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
