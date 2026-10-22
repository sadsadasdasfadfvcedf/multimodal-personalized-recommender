"""
scripts/19_security_check.py
============================

网页服务安全基线与输入边界检查（ecc:security-review 的产物）。

检查项：
    1. 安全响应头与禁止内联脚本的 CSP
    2. POSIX / Windows 路径穿越与监听 Host 校验
    3. Origin 与同源请求标记、JSON Content-Type
    4. 超大、负数、非数字 Content-Length
    5. 图片代理固定安全 Content-Type
    6. 付费接口限流
    7. 源码中无硬编码密钥/口令
    8. 错误响应不泄露 traceback

前置条件：webui.py 正在运行（默认 http://127.0.0.1:8020）
输出：outputs/security_check.json
"""

from __future__ import annotations

import argparse
import json
import re
import socket
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


def _call(
    base: str,
    path: str,
    body=None,
    method: str = "POST",
    timeout: float = 30.0,
    headers: dict | None = None,
    *,
    content_type: str = "application/json; charset=utf-8",
    secure: bool = True,
):
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    hdrs = {"Content-Type": content_type}
    if secure and method == "POST":
        hdrs["X-Requested-With"] = "integrated-ai"
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


def _raw_request(base: str, request: bytes, timeout: float = 5.0) -> str:
    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(request)
        sock.shutdown(socket.SHUT_WR)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = sock.recv(65536)
            except TimeoutError:
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8", "replace")


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
    add(
        2,
        "CSP 已配置且禁止内联脚本",
        "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "script-src 'self';" in csp,
        {"csp": csp[:220]},
    )

    # 3/4) 路径穿越（含 Windows 绝对路径与反斜杠）
    evil = urllib.parse.quote("../../config/settings.py", safe="")
    s3, _, b3 = _call(base, f"/api/img/{evil}", method="GET", timeout=8)
    evil_win = urllib.parse.quote(r"C:\Windows\win.ini", safe="")
    s3b, _, b3b = _call(base, f"/api/img/{evil_win}", method="GET", timeout=8)
    add(
        3,
        "图片接口拒绝路径穿越与 Windows 绝对路径",
        s3 == 400 and b3.get("code") == "E_INVALID_INPUT" and s3b == 400,
        {"posix": [s3, b3], "windows": [s3b, b3b]},
    )
    s4, _, b4 = _call(base, f"/audio/{evil}", method="GET", timeout=15)
    add(4, "音频接口路径穿越被拒", s4 == 404 and b4.get("code") == "E_ITEM_NOT_FOUND", {"status": s4, "body": b4})

    # 5-8) Host / Origin / 同源标记 / JSON Content-Type
    s5, _, b5 = _call(base, "/api/info", method="GET", headers={"Host": "evil.example"})
    add(
        5,
        "DNS rebinding 非白名单 Host 被拒",
        s5 == 403 and b5.get("code") == "E_INVALID_INPUT",
        {"status": s5, "body": b5},
    )
    s6, _, b6 = _call(
        base,
        "/api/user",
        {"user_id": "15547"},
        headers={"Origin": "http://evil.example"},
    )
    add(
        6,
        "跨站 Origin 被拒",
        s6 == 403 and b6.get("code") == "E_INVALID_INPUT",
        {"status": s6, "body": b6},
    )
    s7, _, b7 = _call(base, "/api/user", {"user_id": "15547"}, secure=False)
    add(
        7,
        "缺少同源请求标记被拒",
        s7 == 403 and b7.get("code") == "E_INVALID_INPUT",
        {"status": s7, "body": b7},
    )
    s8, _, b8 = _call(
        base,
        "/api/user",
        {"user_id": "15547"},
        content_type="text/plain",
    )
    add(
        8,
        "非 application/json POST 被拒",
        s8 == 415 and b8.get("code") == "E_INVALID_INPUT",
        {"status": s8, "body": b8},
    )

    # 9) 超大/非法 Content-Length 在读取正文前拒绝
    parsed = urllib.parse.urlsplit(base)
    host = parsed.netloc
    raw_big = (
        f"POST /api/chat HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
        "X-Requested-With: integrated-ai\r\nContent-Length: 99999999\r\nConnection: close\r\n\r\n"
    ).encode()
    raw_big_resp = _raw_request(base, raw_big)
    raw_neg = (
        f"POST /api/chat HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
        "X-Requested-With: integrated-ai\r\nContent-Length: -1\r\nConnection: close\r\n\r\n"
    ).encode()
    raw_neg_resp = _raw_request(base, raw_neg)
    raw_bad = (
        f"POST /api/chat HTTP/1.1\r\nHost: {host}\r\nContent-Type: application/json\r\n"
        "X-Requested-With: integrated-ai\r\nContent-Length: nope\r\nConnection: close\r\n\r\n"
    ).encode()
    raw_bad_resp = _raw_request(base, raw_bad)
    add(
        9,
        "超大/负数/非数字 Content-Length 被快速拒绝",
        " 413 " in raw_big_resp.splitlines()[0]
        and " 400 " in raw_neg_resp.splitlines()[0]
        and " 400 " in raw_bad_resp.splitlines()[0],
        {
            "big": raw_big_resp.splitlines()[0] if raw_big_resp else "",
            "negative": raw_neg_resp.splitlines()[0] if raw_neg_resp else "",
            "non_numeric": raw_bad_resp.splitlines()[0] if raw_bad_resp else "",
        },
    )

    # 10) 图片代理固定安全类型，不透传上游 HTML/SVG
    s10, h10, _ = _call(base, "/api/img/ZZZ999", method="GET", timeout=15)
    ctype10 = str(h10.get("Content-Type") or "").split(";", 1)[0].lower()
    add(
        10,
        "图片代理固定返回安全图片类型",
        s10 == 200 and ctype10 in {"image/png", "image/jpeg", "image/webp"},
        {"status": s10, "content_type": ctype10},
    )

    # 11) 付费接口限流（连续空 TTS 请求应触发 429，不会真正合成音频）
    rate_statuses: list[int] = []
    for _ in range(15):
        sr, _, _ = _call(base, "/api/tts", {"text": ""}, timeout=8)
        rate_statuses.append(sr)
        if sr == 429:
            break
    add(11, "付费对话接口限流生效", 429 in rate_statuses, {"statuses": rate_statuses})

    # 12) 源码无硬编码密钥
    hits = _scan_secrets()
    add(12, "无硬编码密钥", not hits, {"hits": hits[:5], "scanned_dirs": SCAN_DIRS})

    # 13) 错误响应不含 traceback
    s13, _, b13 = _call(base, "/api/not-a-route", method="GET", timeout=10)
    raw13 = json.dumps(b13, ensure_ascii=False)
    add(
        13,
        "错误响应不泄露 traceback",
        "Traceback" not in raw13 and 'File "' not in raw13,
        {"status": s13, "body": b13},
    )

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
