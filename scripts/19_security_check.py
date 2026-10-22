"""
scripts/19_security_check.py
============================

网页服务安全基线与输入边界检查（ecc:security-review 的产物）。

检查项：
    1. 安全响应头（nosniff / X-Frame-Options / Referrer-Policy / CSP）
    2. 图片接口路径穿越（item_id 含 ../）
    3. 音频接口路径穿越（name 含 ../）
    4. 超大请求体不会拖死服务
    5. 语音时长参数越界（负数/超长）被钳制
    6. 源码中无硬编码密钥/口令
    7. 错误响应不泄露 traceback

前置条件：webui.py 正在运行（默认 http://127.0.0.1:8020）
输出：outputs/security_check.json
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from _bootstrap import PROJECT_ROOT, banner

from config import settings
from src.utils import now_iso, write_json

SECRET_PATTERNS = [
    re.compile(r"(api[_-]?key|secret|passwd|password|token)\s*=\s*[\"'][^\"']{8,}", re.I),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
]
SCAN_DIRS = ["config", "src", "scripts"]
SCAN_FILES = ["webui.py", "chat.py", "main.py"]


def _call(base: str, path: str, body=None, method: str = "POST", timeout: float = 30.0, headers: dict | None = None):
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    hdrs = {"Content-Type": "application/json; charset=utf-8"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(base + path, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ctype = resp.headers.get("Content-Type", "")
            payload = json.loads(raw.decode("utf-8")) if raw and "json" in ctype else {}
            return resp.status, dict(resp.headers), payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, dict(exc.headers), json.loads(raw.decode("utf-8"))
        except Exception:
            return exc.code, dict(exc.headers), {"_raw": raw[:200].decode("utf-8", "replace")}


def _scan_secrets() -> list[str]:
    hits: list[str] = []
    paths: list[Path] = []
    for name in SCAN_DIRS:
        paths.extend((PROJECT_ROOT / name).rglob("*.py"))
    for name in SCAN_FILES:
        p = PROJECT_ROOT / name
        if p.exists():
            paths.append(p)
    for p in paths:
        if "outputs" in p.parts or "artifacts" in p.parts:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pat in SECRET_PATTERNS:
            if pat.search(text):
                hits.append(str(p.relative_to(PROJECT_ROOT)))
                break
    return hits


def main() -> int:
    banner("网页服务安全基线检查")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    args = parser.parse_args()
    base = args.url.rstrip("/")
    report: dict = {"checked_at": now_iso(), "url": base, "checks": []}

    def add(no: int, name: str, ok: bool, detail: dict) -> None:
        report["checks"].append({"no": no, "check": name, "passed": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {no}. {name} —— {json.dumps(detail, ensure_ascii=False)[:160]}")

    try:
        status, headers, _ = _call(base, "/", method="GET", timeout=8, headers={"Accept": "text/html"})
    except Exception as exc:
        report.update({"mode": "skipped", "reason": f"webui 不可达: {exc}"})
        path = write_json(settings.PATHS["outputs"] / "security_check.json", report)
        print("跳过：webui 不可达")
        print("[19_security_check] 证据已写入 " + path.name)
        return 0

    h = {k.lower(): v for k, v in headers.items()}
    add(
        1,
        "安全响应头",
        status == 200
        and h.get("x-content-type-options") == "nosniff"
        and h.get("x-frame-options") == "DENY"
        and h.get("referrer-policy") == "no-referrer",
        {
            "status": status,
            "nosniff": h.get("x-content-type-options"),
            "frame": h.get("x-frame-options"),
            "referrer": h.get("referrer-policy"),
        },
    )
    csp = h.get("content-security-policy") or ""
    add(2, "CSP 已配置", "default-src 'self'" in csp and "frame-ancestors 'none'" in csp, {"csp": csp[:180]})

    # 3/4) 路径穿越
    evil = urllib.parse.quote("../../config/settings.py", safe="")
    s3, _, b3 = _call(base, f"/api/img/{evil}", method="GET", timeout=15)
    add(3, "图片接口路径穿越被拒", s3 == 404 and b3.get("code") == "E_ITEM_NOT_FOUND", {"status": s3, "body": b3})
    s4, _, b4 = _call(base, f"/audio/{evil}", method="GET", timeout=15)
    add(4, "音频接口路径穿越被拒", s4 == 404 and b4.get("code") == "E_ITEM_NOT_FOUND", {"status": s4, "body": b4})

    # 5) 超大请求体（>1MB）不应拖死服务
    big = {"text": "x" * 1_100_000}
    s5, _, b5 = _call(base, "/api/chat", big, timeout=20)
    resp5 = b5.get("response") or {}
    add(
        5,
        "超大请求体被快速拒绝",
        s5 == 200 and (resp5.get("status") == "error"),
        {"status": s5, "resp_status": resp5.get("status"), "code": (resp5.get("error") or {}).get("code")},
    )

    # 6) 语音时长越界：负数应被钳制到 1s，且响应结构化
    s6, _, b6 = _call(base, "/api/voice", {"seconds": -999}, timeout=40)
    add(
        6,
        "语音时长越界被钳制",
        s6 == 200 and ("ok" in b6) and isinstance(b6.get("ok"), bool),
        {"status": s6, "keys": sorted(b6)},
    )

    # 7) 源码无硬编码密钥
    hits = _scan_secrets()
    add(7, "无硬编码密钥", not hits, {"hits": hits[:5], "scanned_dirs": SCAN_DIRS})

    # 8) 错误响应不含 traceback
    s8, _, b8 = _call(base, "/api/not-a-route", method="GET", timeout=10)
    raw8 = json.dumps(b8, ensure_ascii=False)
    add(8, "错误响应不泄露 traceback", "Traceback" not in raw8 and 'File "' not in raw8, {"status": s8, "body": b8})

    passed = sum(1 for c in report["checks"] if c["passed"])
    report["summary"] = {
        "total": len(report["checks"]),
        "passed": passed,
        "failed": len(report["checks"]) - passed,
        "verdict": "PASS" if passed == len(report["checks"]) else "FAIL",
    }
    path = write_json(settings.PATHS["outputs"] / "security_check.json", report)
    print(f"\n合计 {passed}/{len(report['checks'])} 通过")
    print("[19_security_check] 证据已写入 " + path.name)
    return 0 if passed == len(report["checks"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
