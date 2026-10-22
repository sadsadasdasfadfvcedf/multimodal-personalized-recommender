"""
scripts/16_webui_click_path_check.py
====================================

网页点击路径回归（ecc:click-path-audit 的产物）。

为什么需要
----------
13_webui_acceptance.py 验证的是「页面长什么样」（视口、触屏、状态编码）；
本脚本验证的是「点下去之后状态对不对」—— 专门抓那些单个函数都正常、
组合起来却互相抵消的 bug（本脚本的 4 条用例都对应一次真实修复）。

检查项：
    1. 清空对比栏后，商品卡上的「✓ 已加入对比」必须复位
    2. 从相似商品详情点「加入对比」必须真的进入对比栏
    3. 新会话（还没聊过天）直接点赞，后端必须真的记录
    4. busy 时再次提交，输入框不能把用户刚打的字清空

前置条件（缺一即跳过，不影响离线可复现性）：
    · 本机装有 Microsoft Edge
    · webui.py 正在运行（默认 http://127.0.0.1:8020）

输出：outputs/webui_click_path.json
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


async def _run(url: str, port: int, edge: str) -> list[dict]:
    import websockets

    results: list[dict] = []

    def add(no: int, name: str, ok: bool, detail: dict) -> None:
        results.append({"no": no, "check": name, "passed": bool(ok), "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {no}. {name} —— {detail}")

    def hg(path: str):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    profile = tempfile.mkdtemp(prefix="webui-clickpath-")
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
    try:
        for _ in range(60):
            try:
                hg("/json/version")
                break
            except Exception:
                time.sleep(0.5)
        pages = [t for t in hg("/json/list") if t.get("type") == "page"]
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

            async def ev(expr: str):
                r = await cmd("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
                return r.get("result", {}).get("value")

            await cmd("Page.enable")
            await cmd("Runtime.enable")
            await cmd("Page.navigate", {"url": url})
            await asyncio.sleep(6)

            # 1) 清空对比栏 -> 卡片标签复位
            r1 = await ev("""(function(){
              const b = document.querySelectorAll('[data-cmp]');
              if (b.length < 2) return {ok:false, reason:'cards<2'};
              b[0].click(); b[1].click();
              const before = Array.from(document.querySelectorAll('[data-cmp]')).slice(0,2).map(x=>x.textContent);
              document.getElementById('trayClear').click();
              const after = Array.from(document.querySelectorAll('[data-cmp]')).slice(0,2).map(x=>x.textContent);
              const pill = document.getElementById('cmpPill').textContent;
              return {ok: before.every(t=>t.includes('已加入')) && after.every(t=>t==='加入对比') && pill==='0',
                      before: before, after: after, pill: pill};
            })()""")
            add(1, "清空对比栏后卡片标签复位", bool((r1 or {}).get("ok")), r1 or {})

            # 2) 相似商品详情 -> 加入对比生效
            await ev("document.querySelector('.pcard [data-open]').click()")
            await asyncio.sleep(1.5)
            mini = await ev("document.querySelectorAll('#sheet .mini [data-item]').length")
            if mini:
                await ev("document.querySelector('#sheet .mini [data-item]').click()")
                await asyncio.sleep(2.0)
                r2 = await ev("""(function(){
                  const c = document.querySelector('[data-cmp2]');
                  if (!c) return {ok:false, reason:'no cmp2'};
                  const before = Number(document.getElementById('cmpPill').textContent || 0);
                  c.click();
                  const after = Number(document.getElementById('cmpPill').textContent || 0);
                  return {ok: after === before + 1, before: before, after: after};
                })()""")
            else:
                r2 = {"ok": False, "reason": "no similar items"}
            add(2, "相似商品可加入对比", bool((r2 or {}).get("ok")), r2 or {})

            # 3) 新会话直接点赞必须真的记录（reset 后会话不存在）
            await ev(
                "fetch('/api/reset',{method:'POST',headers:{'Content-Type':'application/json',"
                "'X-Requested-With':'integrated-ai'},body:'{}'})"
            )
            await asyncio.sleep(2)
            await ev("document.querySelector('.pcard [data-like=up]').click()")
            await asyncio.sleep(16)
            r3 = await ev("""(async function(){
              const d = await (await fetch('/api/state',{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'integrated-ai'},body:'{}'})).json();
              const likes = (d.state || {}).likes || {};
              const first = document.querySelector('.pcard').dataset.item;
              const card = document.querySelector('.pcard');
              return {ok: Object.prototype.hasOwnProperty.call(likes, first),
                      first: first, likes: likes,
                      cardOn: !!card.querySelector('[data-like].on'),
                      pending: (d.state || {}).pending_clarification};
            })()""")
            add(3, "新会话直接点赞被真实记录", bool((r3 or {}).get("ok")), r3 or {})

            # 4) busy 时再次提交不清空输入（放最后：会挂起 post）
            r4 = await ev("""(function(){
              window.post = () => new Promise(()=>{});
              const q = document.getElementById('q');
              q.value = '第一句';
              document.getElementById('searchForm').requestSubmit();
              const first = q.value;
              q.value = '第二句';
              document.getElementById('searchForm').requestSubmit();
              return {ok: first === '' && q.value === '第二句', first: first, second: q.value,
                      live: document.getElementById('liveTxt').textContent};
            })()""")
            add(4, "busy 时提交不丢输入", bool((r4 or {}).get("ok")), r4 or {})
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except Exception:
            proc.kill()
    return results


def main() -> int:
    banner("网页点击路径回归（click-path audit）")
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    parser.add_argument("--port", type=int, default=9339)
    args = parser.parse_args()

    report: dict = {"checked_at": now_iso(), "url": args.url, "mode": "real", "checks": []}
    edge = _edge()
    if edge is None:
        report.update({"mode": "skipped", "reason": "未找到 Microsoft Edge"})
        path = write_json(settings.PATHS["outputs"] / "webui_click_path.json", report)
        print("跳过：未找到 Microsoft Edge")
        print("[16_webui_click_path_check] 证据已写入 " + path.name)
        return 0
    if not _reachable(args.url):
        report.update({"mode": "skipped", "reason": "webui 未运行（请先 python webui.py）"})
        path = write_json(settings.PATHS["outputs"] / "webui_click_path.json", report)
        print("跳过：webui 未运行")
        print("[16_webui_click_path_check] 证据已写入 " + path.name)
        return 0

    results = asyncio.run(_run(args.url, args.port, edge))
    report["checks"] = results
    passed = sum(1 for r in results if r["passed"])
    report["summary"] = {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "verdict": "PASS" if passed == len(results) else "FAIL",
    }
    path = write_json(settings.PATHS["outputs"] / "webui_click_path.json", report)
    print(f"\n合计 {passed}/{len(results)} 通过")
    print("[16_webui_click_path_check] 证据已写入 " + path.name)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
