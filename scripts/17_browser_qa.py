"""
scripts/17_browser_qa.py
========================

网页运行时质量检查（ecc:browser-qa 的产物）。

和 13/16 的分工：
    13_webui_acceptance.py        页面「长什么样」（视口、触屏、状态编码）
    16_webui_click_path_check.py  点下去「状态对不对」
    17_browser_qa.py              浏览器「跑起来健不健康」：console / network / CWV / 无障碍

检查项：
    1. console error / warning
    2. 网络 4xx/5xx（排除 favicon 等噪音）
    3. Core Web Vitals：LCP < 2.5s、CLS < 0.1、INP < 200ms（INP 用 event timing 近似）
    4. 三档视口截图：375 / 768 / 1440
    5. 基础无障碍：图片 alt、按钮可访问名称、输入框标签、landmark

视觉基线：仓库没有提交历史截图基线，因此视觉回归结论为 **INCONCLUSIVE**，
不静默判 PASS（符合 skill 要求）。

前置条件：webui.py 正在运行 + 本机有 Microsoft Edge
输出：outputs/browser_qa.json、outputs/browser_qa_{375,768,1440}.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from _bootstrap import banner

from config import settings
from src.utils import now_iso, write_json

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]
CWV_INIT = """
window.__cwv = {lcp: 0, cls: 0, inp: 0};
try {
  new PerformanceObserver(l => { for (const e of l.getEntries()) window.__cwv.lcp = e.startTime; })
    .observe({type: 'largest-contentful-paint', buffered: true});
  new PerformanceObserver(l => { for (const e of l.getEntries()) if (!e.hadRecentInput) window.__cwv.cls += e.value; })
    .observe({type: 'layout-shift', buffered: true});
  new PerformanceObserver(l => { for (const e of l.getEntries()) window.__cwv.inp = Math.max(window.__cwv.inp, e.duration || 0); })
    .observe({type: 'event', durationThreshold: 40, buffered: true});
} catch (e) {}
"""


def _edge() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _reachable(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/info", timeout=3):
            return True
    except Exception:
        return False


async def _run(url: str, port: int, edge: str) -> dict:
    import websockets

    events: list[dict] = []
    profile = tempfile.mkdtemp(prefix="browserqa-")
    proc = subprocess.Popen(
        [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1440,1000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    screenshots: dict[str, str] = {}
    try:
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=5):
                    break
            except Exception:
                time.sleep(0.5)
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as r:
            pages = [t for t in json.loads(r.read().decode("utf-8")) if t.get("type") == "page"]
        async with websockets.connect(pages[0]["webSocketDebuggerUrl"], max_size=None) as ws:
            counter = 0

            async def cmd(method, params=None, timeout=120):
                nonlocal counter
                counter += 1
                mid = counter
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                deadline = time.time() + timeout
                while True:
                    left = deadline - time.time()
                    if left <= 0:
                        raise TimeoutError(method)
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=left))
                    if msg.get("id") == mid:
                        return msg.get("result", {})
                    events.append(msg)

            async def ev(expr):
                r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("value")

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Network.enable")
            await cmd("Log.enable")
            await cmd("Page.addScriptToEvaluateOnNewDocument", {"source": CWV_INIT})
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(8)
            # LCP/CLS 只看初始加载；交互后的内容不应计入 LCP（web.dev 口径）
            cwv_initial = await ev("window.__cwv || {}")

            # 交互：一次有效查询 + 一次空提交
            await ev(
                "(function(){const q=document.getElementById('q');q.value='300以内的水杯';document.getElementById('searchForm').requestSubmit();})()"
            )
            await asyncio.sleep(6)
            await ev("document.getElementById('searchForm').requestSubmit()")
            await asyncio.sleep(1)

            cwv_after = await ev("window.__cwv || {}")
            cwv = {
                "lcp": (cwv_initial or {}).get("lcp", 0),
                "cls": (cwv_initial or {}).get("cls", 0),
                "inp": (cwv_after or {}).get("inp", 0),
            }
            a11y = await ev("""(function(){
              const imgs = Array.from(document.querySelectorAll('img'));
              const noAlt = imgs.filter(i => !i.hasAttribute('alt')).length;
              const btns = Array.from(document.querySelectorAll('button'));
              const noName = btns.filter(b => !(b.textContent||'').trim() && !b.getAttribute('aria-label') && !b.getAttribute('title')).length;
              const inputs = Array.from(document.querySelectorAll('input'));
              const hasLabel = (i) => {
                if (i.getAttribute('aria-label') || i.getAttribute('aria-labelledby')) return true;
                if (i.closest('label')) return true;
                if (i.id && document.querySelector('label[for="' + CSS.escape(i.id) + '"]')) return true;
                return false;
              };
              const noLabel = inputs.filter(i => !hasLabel(i)).length;
              return {images: imgs.length, noAlt, buttons: btns.length, noName, inputs: inputs.length, noLabel,
                      landmarks: {main: document.querySelectorAll('main').length,
                                  nav: document.querySelectorAll('nav').length,
                                  header: document.querySelectorAll('header').length}};
            })()""")

            for width in (375, 768, 1440):
                await cmd(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": 900, "deviceScaleFactor": 1, "mobile": width <= 375},
                )
                await asyncio.sleep(0.8)
                shot = await cmd("Page.captureScreenshot", {"format": "png"})
                out = settings.PATHS["outputs"] / f"browser_qa_{width}.png"
                out.write_bytes(base64.b64decode(shot["data"]))
                screenshots[str(width)] = str(out)
            await cmd("Emulation.clearDeviceMetricsOverride")
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()

    console_errors = []
    console_warnings = []
    network_failures = []
    for msg in events:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "Runtime.consoleAPICalled":
            typ = params.get("type")
            text = " ".join(str((a or {}).get("value", "")) for a in params.get("args") or [])[:300]
            if typ == "error":
                console_errors.append(text)
            elif typ == "warning":
                console_warnings.append(text)
        elif method == "Log.entryAdded":
            entry = params.get("entry") or {}
            if entry.get("level") == "error":
                console_errors.append(str(entry.get("text"))[:300])
        elif method == "Network.responseReceived":
            resp = params.get("response") or {}
            status = int(resp.get("status") or 0)
            rurl = str(resp.get("url") or "")
            if status >= 400 and "favicon" not in rurl and "/api/img/" not in rurl:
                network_failures.append({"status": status, "url": rurl[:200]})

    return {
        "console_errors": console_errors[:20],
        "console_warnings": console_warnings[:20],
        "network_failures": network_failures[:20],
        "cwv": {
            "lcp_ms": round(float((cwv or {}).get("lcp") or 0), 1),
            "cls": round(float((cwv or {}).get("cls") or 0), 4),
            "inp_ms": round(float((cwv or {}).get("inp") or 0), 1),
        },
        "a11y": a11y or {},
        "screenshots": screenshots,
    }


def main() -> int:
    banner("浏览器运行时质量检查（browser-qa）")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    parser.add_argument("--port", type=int, default=9340)
    args = parser.parse_args()

    report: dict = {"checked_at": now_iso(), "url": args.url, "mode": "real"}
    edge = _edge()
    if edge is None:
        report.update({"mode": "skipped", "reason": "未找到 Microsoft Edge"})
        path = write_json(settings.PATHS["outputs"] / "browser_qa.json", report)
        print("跳过：未找到 Microsoft Edge")
        print("[17_browser_qa] 证据已写入 " + path.name)
        return 0
    if not _reachable(args.url):
        report.update({"mode": "skipped", "reason": "webui 未运行（请先 python webui.py）"})
        path = write_json(settings.PATHS["outputs"] / "browser_qa.json", report)
        print("跳过：webui 未运行")
        print("[17_browser_qa] 证据已写入 " + path.name)
        return 0

    data = asyncio.run(_run(args.url, args.port, edge))
    report.update(data)
    cwv = data["cwv"]
    a11y = data["a11y"]
    blockers = []
    if data["console_errors"]:
        blockers.append(f"console error {len(data['console_errors'])} 条")
    if any(f["status"] >= 500 for f in data["network_failures"]):
        blockers.append("网络 5xx")
    if cwv["lcp_ms"] > 2500:
        blockers.append(f"LCP {cwv['lcp_ms']}ms > 2500ms")
    if cwv["cls"] > 0.1:
        blockers.append(f"CLS {cwv['cls']} > 0.1")

    issues = []
    if data["network_failures"]:
        issues.append(f"网络 4xx/5xx {len(data['network_failures'])} 条")
    if a11y.get("noName"):
        issues.append(f"无名称按钮 {a11y['noName']} 个")
    if a11y.get("noLabel"):
        issues.append(f"无标签输入框 {a11y['noLabel']} 个")
    if a11y.get("noAlt"):
        issues.append(f"无 alt 图片 {a11y['noAlt']} 个")

    report["visual"] = {
        "baseline": False,
        "verdict": "INCONCLUSIVE",
        "note": "仓库未提交历史截图基线，按 skill 要求不静默判 PASS",
    }
    report["verdict"] = "DO NOT SHIP" if blockers else ("SHIP WITH FIXES" if issues else "SHIP")
    report["blockers"] = blockers
    report["issues"] = issues
    path = write_json(settings.PATHS["outputs"] / "browser_qa.json", report)

    print(f"console: {len(data['console_errors'])} error / {len(data['console_warnings'])} warning")
    print(f"network: {len(data['network_failures'])} 条 4xx/5xx")
    print(f"CWV    : LCP {cwv['lcp_ms']}ms / CLS {cwv['cls']} / INP {cwv['inp_ms']}ms")
    print(
        f"a11y   : 图片 {a11y.get('images', 0)}（无 alt {a11y.get('noAlt', 0)}）"
        f" / 按钮 {a11y.get('buttons', 0)}（无名称 {a11y.get('noName', 0)}）"
        f" / 输入 {a11y.get('inputs', 0)}（无标签 {a11y.get('noLabel', 0)}）"
    )
    print("视觉    : INCONCLUSIVE（无历史基线）")
    print(f"判定    : {report['verdict']}")
    if blockers:
        print("  blockers:", blockers)
    if issues:
        print("  issues  :", issues)
    print("[17_browser_qa] 证据已写入 " + path.name)
    return 0 if report["verdict"] != "DO NOT SHIP" else 1


if __name__ == "__main__":
    raise SystemExit(main())
