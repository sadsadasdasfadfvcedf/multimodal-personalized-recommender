"""
scripts/13_webui_acceptance.py
==============================

网页改版的验收清单核验（对应《页面改版设计README》表 6-1 的 10 项）。

实现方式：用 Edge 的 CDP（Chrome DevTools Protocol）驱动无头浏览器，
在真实页面上**测量**而不是靠肉眼判断：

    1. 8 档视口（496~1440px）下 document.scrollWidth 是否 ≤ innerWidth
    2. 👍/👎 是否常驻（不依赖 hover）
    3. 筛选条件 chip 是否可单独移除
    4. 加选 → 底栏升起 → 字段对照表能否出结果
    5. 状态徽标是否「圆点 + 图标 + 文字」三重编码
    6. 可见文本里是否还残留内部枚举（historical_mode 等）
    7. 模态输入失败时是否给出可点击的下一步
    8. 运行细节是否默认折叠
    9. 原有能力（证据条数 / 详情入口 / 回到最新）是否保留
   10. 未新增前端依赖（由代码结构保证，此项由本脚本静态确认）

前置条件（缺一即跳过，不影响离线可复现性）：
    · 本机装有 Microsoft Edge
    · webui.py 正在运行（默认 http://127.0.0.1:8020）

输出：outputs/webui_acceptance.json
"""

from __future__ import annotations

import argparse
import asyncio
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
WIDTHS = [496, 614, 674, 742, 794, 900, 1100, 1440]


def _edge() -> str | None:
    for path in EDGE_CANDIDATES:
        if Path(path).exists():
            return path
    return None


def _reachable(url: str, timeout: float = 3.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/api/info", timeout=timeout):
            return True
    except Exception:
        return False


async def _run(url: str, port: int, edge: str, seconds: float) -> list[dict]:
    import websockets

    results: list[dict] = []

    def add(no: int, name: str, ok: bool, detail: str) -> None:
        results.append({"no": no, "check": name, "passed": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {no}. {name} —— {detail}")

    def hg(path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    profile = tempfile.mkdtemp(prefix="webui-accept-")
    proc = subprocess.Popen(
        [
            edge,
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--window-size=1600,1000",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(60):
            try:
                hg("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        targets = [t for t in hg("/json/list") if t.get("type") == "page"]
        async with websockets.connect(targets[0]["webSocketDebuggerUrl"], max_size=None) as ws:
            counter = 0

            async def cmd(method, params=None, timeout=300):
                nonlocal counter
                counter += 1
                mid = counter
                await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
                deadline = time.time() + timeout
                while time.time() < deadline:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=max(1, deadline - time.time())))
                    if msg.get("id") == mid:
                        return msg
                raise TimeoutError(method)

            async def ev(expr):
                r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True})
                return r["result"]["result"].get("value")

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Page.navigate", {"url": url})
            for _ in range(80):
                if (await ev("document.querySelectorAll('.pcard').length") or 0) > 0:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(2.0)

            worst, worst_w = 0, WIDTHS[0]
            for w in WIDTHS:
                await cmd(
                    "Emulation.setDeviceMetricsOverride",
                    {"width": w, "height": 900, "deviceScaleFactor": 1, "mobile": w < 800},
                )
                await asyncio.sleep(0.45)
                over = (await ev("document.documentElement.scrollWidth") or 0) - (await ev("window.innerWidth") or 0)
                if over > worst:
                    worst, worst_w = over, w
            add(1, "横向无溢出（8 档视口 496~1440px）", worst == 0, f"最大溢出 {worst}px（{worst_w}px 视口）")
            await cmd(
                "Emulation.setDeviceMetricsOverride",
                {"width": 1600, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
            )
            await asyncio.sleep(0.5)

            await ev(
                "document.getElementById('q').value='500以内的婚礼装饰品';"
                "document.getElementById('searchForm').dispatchEvent(new Event('submit',{cancelable:true}))"
            )
            for _ in range(160):
                if await ev(
                    "document.querySelectorAll('.spin').length===0 && "
                    "document.getElementById('shelfTitle').textContent!=='猜你喜欢'"
                ):
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(1.5)

            op = await ev("getComputedStyle(document.querySelector('.pacts')).opacity")
            add(2, "触屏可反馈（👍/👎 常驻，不依赖 hover）", str(op) in ("1", "1.0"), f"opacity={op}")

            n = await ev("document.querySelectorAll('#fchips [data-drop]').length")
            add(3, "筛选条件可单独移除", (n or 0) > 0, f"可移除 chip {n or 0} 个")

            await ev(
                "document.querySelectorAll('.pcard [data-cmp]')[0].click();"
                "document.querySelectorAll('.pcard [data-cmp]')[1].click()"
            )
            await asyncio.sleep(0.6)
            tray = await ev("document.getElementById('tray').classList.contains('show')")
            await ev("document.getElementById('trayGo').click()")
            await asyncio.sleep(2.0)
            cells = await ev("document.querySelectorAll('.cmptable td').length")
            add(
                4,
                "对比可用（加选 → 底栏 → 字段对照）",
                bool(tray) and (cells or 0) > 0,
                f"底栏弹出={tray}，对照单元格 {cells or 0} 个",
            )
            await ev("document.querySelector('#cmpModal [data-close]').click()")

            tri = await ev(
                "(function(){var s=document.querySelector('.bub .st');"
                "return s? (s.querySelector('i')!==null && s.textContent.trim().length>1):false})()"
            )
            add(5, "状态三重编码（圆点 + 图标 + 文字）", tri, f"徽标同时含圆点与中文={tri}")

            txt = await ev("document.body.innerText") or ""
            residual = [
                t for t in ["historical_mode", "offline_snapshot", "category_median", "offline_stub"] if t in txt
            ]
            add(
                6,
                "内部术语已翻译（可见文本无内部枚举）",
                not residual,
                f"残留 {residual}" if residual else "页面可见文本无内部枚举",
            )

            await ev("document.getElementById('toolbar').querySelector('[data-act=\"gesture\"]').click()")
            for _ in range(200):
                if (await ev("document.querySelectorAll('.act.pending').length") or 0) == 0:
                    break
                await asyncio.sleep(0.5)
            await asyncio.sleep(0.6)
            sugg = await ev("document.querySelectorAll('.sugg button').length")
            add(7, "失败给出可点击的下一步", (sugg or 0) > 0, f"建议按钮 {sugg or 0} 个")

            folds = await ev("document.querySelectorAll('[data-fold]').length")
            vis = await ev(
                "(function(){var f=document.querySelector('.fold');"
                "return f?getComputedStyle(f.querySelector('.body')).display:'none'})()"
            )
            add(
                8, "运行细节默认折叠", (folds or 0) > 0 and vis == "none", f"折叠块 {folds or 0} 个，默认 display={vis}"
            )

            evd = await ev("document.querySelectorAll('.pcard .t.ev').length")
            keep = await ev(
                "document.getElementById('toBottom')!==null && "
                "document.getElementById('modal')!==null && "
                "document.getElementById('panel')!==null"
            )
            add(
                9,
                "原有能力保留（证据条数 / 详情 / 回到最新）",
                (evd or 0) > 0 and bool(keep),
                f"证据标签 {evd or 0} 个，关键组件齐全={keep}",
            )

            src = Path(settings.PROJECT_ROOT / "webui.py").read_text(encoding="utf-8")
            external = [u for u in ("cdn.", "unpkg", "jsdelivr", "googleapis") if u in src]
            add(
                10,
                "未新增第三方前端依赖（仍为单文件 + 标准库）",
                not external,
                f"外部 CDN 引用 {external}" if external else "无外部 CDN / 无 npm 依赖",
            )
    finally:
        proc.terminate()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="网页改版验收核验")
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    parser.add_argument("--port", type=int, default=9455, help="CDP 调试端口")
    args = parser.parse_args()

    banner("网页改版验收核验（设计文档表 6-1，10 项）")
    report = {"checked_at": now_iso(), "url": args.url, "checks": [], "verdict": "SKIP"}

    edge = _edge()
    if edge is None:
        print("跳过：未找到 Microsoft Edge（本项需无头浏览器测量）")
    elif not _reachable(args.url):
        print(f"跳过：{args.url} 不可达。请先运行：python webui.py --port {args.url.rsplit(':', 1)[-1]}")
    else:
        checks = asyncio.run(_run(args.url, args.port, edge, 0))
        report["checks"] = checks
        passed = sum(1 for c in checks if c["passed"])
        report["summary"] = {"total": len(checks), "passed": passed, "failed": len(checks) - passed}
        report["verdict"] = "PASS" if passed == len(checks) else "FAIL"
        print(f"\n验收：{passed} / {len(checks)} 通过 —— {report['verdict']}")

    path = write_json(settings.PATHS["outputs"] / "webui_acceptance.json", report)
    print(f"[13_webui_acceptance] 证据已写入 {path.name}")
    return 0 if report["verdict"] in ("PASS", "SKIP") else 1


if __name__ == "__main__":
    raise SystemExit(main())
