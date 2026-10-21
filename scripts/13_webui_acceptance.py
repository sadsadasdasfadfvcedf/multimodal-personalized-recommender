"""
scripts/13_webui_acceptance.py
==============================

网页改版的验收清单核验（对应《页面改版设计README》表 6-1，当前 17 项）。

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
  10. 未新增第三方前端依赖（由项目内静态资源与标准库共同保证）
  11. 对比不足 2 件时有可见提示
  12. 反馈只锁当前商品，不锁整页
  13. 桌面为商品瀑布流 + 推荐助手两栏布局
  14. 商品详情含推荐理由、证据、对比与继续追问
  15. 识图请求不锁整页，失败原因可见
  16. 追问携带焦点商品并直接展示推荐理由，不覆盖当前货架
  17. 点赞不改位置；倒赞移除后撤销恢复原位，再次点赞仍不改位置

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
                r = await cmd(
                    "Runtime.evaluate",
                    {"expression": expr, "returnByValue": True, "awaitPromise": True},
                )
                payload = r.get("result") or {}
                if payload.get("exceptionDetails"):
                    detail = payload["exceptionDetails"]
                    raise RuntimeError(f"JS evaluation failed: {detail}")
                return payload.get("result", {}).get("value")

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

            layout = await ev(
                "(function(){var s=document.getElementById('shelf'),"
                "a=document.getElementById('assistant');"
                "return s&&a?s.getBoundingClientRect().width>a.getBoundingClientRect().width:false})()"
            )
            add(13, "电商两栏布局（瀑布流宽于助手）", bool(layout), f"瀑布流更宽={layout}")

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

            feedback_state = await ev(
                "(function(){document.querySelector('.pcard [data-like=\"up\"]').click();"
                "return {busy:busy,pending:document.querySelector('.pcard.pending')!==null,"
                "cards:document.querySelectorAll('.pcard').length}})()"
            )
            busy_after = (feedback_state or {}).get("busy")
            card_pending = (feedback_state or {}).get("pending")
            cards_before_feedback = (feedback_state or {}).get("cards")
            for _ in range(80):
                if await ev("document.querySelector('.pcard.pending')===null"):
                    break
                await asyncio.sleep(0.1)
            cards_after_feedback = await ev("document.querySelectorAll('.pcard').length")
            add(
                12,
                "反馈不锁整页且货架数量不缩水",
                busy_after is False
                and bool(card_pending)
                and (cards_after_feedback or 0) >= (cards_before_feedback or 0),
                f"反馈期间 busy={busy_after}，当前卡片 pending={card_pending}，"
                f"货架 {cards_before_feedback}→{cards_after_feedback}",
            )

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

            await ev("document.getElementById('trayClear').click()")
            await ev("document.querySelector('[data-act=\"compare\"]').click()")
            await asyncio.sleep(0.2)
            toast_text = await ev("document.querySelector('.toast.show')?.textContent || ''")
            add(
                11,
                "对比不足 2 件时可见提示",
                "至少选择 2 件" in str(toast_text),
                f"toast={toast_text}",
            )

            sugg = await ev("actionSuggestions('voice')")
            add(
                7,
                "失败给出可点击的下一步",
                "<button" in str(sugg or ""),
                "失败建议组件可生成重试/改用打字按钮",
            )

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
                "document.getElementById('assistant')!==null && "
                "document.getElementById('toolbar').querySelector('[data-act=\"gesture\"]')===null"
            )
            add(
                9,
                "原有能力保留（证据条数 / 详情 / 回到最新 / 主页面无手势按钮）",
                (evd or 0) > 0 and bool(keep),
                f"证据标签 {evd or 0} 个，关键组件齐全={keep}",
            )

            await ev("document.querySelector('.pcard [data-open]').click()")
            for _ in range(40):
                if await ev("!!document.querySelector('#modal [data-reason]')"):
                    break
                await asyncio.sleep(0.25)
            detail = await ev("document.getElementById('modal').classList.contains('show')")
            has_reason = await ev("!!document.querySelector('#modal [data-reason]')")
            has_evidence = await ev("!!document.querySelector('#modal .evidence-list')")
            has_actions = await ev(
                "document.querySelectorAll('#modal [data-rate]').length===2 && "
                "!!document.querySelector('#modal [data-cmp2]') && "
                "!!document.querySelector('#modal [data-ask-product]') && "
                "!document.querySelector('#modal .mini')"
            )
            humanized = await ev(
                "!/(historical_mode|category_median|catalog_default|offline_snapshot|offline_stub)/"
                ".test(document.getElementById('modal').innerText)"
            )
            await ev("document.querySelector('#modal [data-ask-product]').click()")
            await asyncio.sleep(0.15)
            ask_focus = await ev(
                "!document.getElementById('modal').classList.contains('show') && "
                "document.activeElement && document.activeElement.id==='q2'"
            )
            add(
                14,
                "商品详情含推荐理由、证据、对比与继续追问",
                bool(detail)
                and bool(has_reason)
                and bool(has_evidence)
                and bool(has_actions)
                and bool(humanized)
                and bool(ask_focus),
                f"详情={detail}，理由={has_reason}，证据={has_evidence}，操作={has_actions}，"
                f"术语已翻译={humanized}，追问聚焦={ask_focus}",
            )
            if await ev("document.getElementById('modal').classList.contains('show')"):
                await ev("document.querySelector('#modal [data-close]').click()")

            await ev("setDeviceAllowed(true, false)")
            await ev(
                "(function(){window.__visionOrigFetch=window.fetch;"
                "window.fetch=async function(url,opts){"
                "if(String(url)==='/api/vision'){await new Promise(r=>setTimeout(r,900));"
                "return new Response(JSON.stringify({ok:false,description:'眼镜',"
                "error:'识别到「眼镜」，但未能确定商品品类。请把物品放近、避免遮挡后重试。'}),"
                "{status:200,headers:{'Content-Type':'application/json'}});}"
                "return window.__visionOrigFetch(url,opts)};})()"
            )
            vision_state = await ev(
                "(function(){document.querySelector('[data-act=\"vision\"]').click();"
                "return {busy:busy,visionBusy:visionBusy}})()"
            )
            await asyncio.sleep(1.1)
            vision_error_visible = await ev("document.getElementById('stream').innerText.includes('未能确定商品品类')")
            await ev("window.fetch=window.__visionOrigFetch")
            add(
                15,
                "识图请求不锁整页且失败原因可见",
                (vision_state or {}).get("busy") is False
                and (vision_state or {}).get("visionBusy") is True
                and bool(vision_error_visible),
                f"请求中 busy={(vision_state or {}).get('busy')}，"
                f"visionBusy={(vision_state or {}).get('visionBusy')}，错误可见={vision_error_visible}",
            )

            await ev(
                "(function(){window.__chatOrigFetch=window.fetch;"
                "window.__testItemId=document.querySelector('.pcard').dataset.item;"
                "window.__cardsBefore=document.querySelectorAll('.pcard').length;"
                "focusItemId=window.__testItemId;"
                "window.fetch=async function(url,opts){"
                "if(String(url)==='/api/chat'){window.__chatPayload=JSON.parse(opts.body||'{}');"
                "return new Response(JSON.stringify({ok:true,response:{status:'ok',"
                "intent_result:{intent:'explain',slots:{target:{value:window.__testItemId}}},"
                "recommendations:[{item_id:window.__testItemId,title:'当前商品',"
                "explanation:'它符合你当前的品类和预算条件，并且有可核验的商品证据。',"
                "evidence_count:7}],trace:{latency_ms:{},modes:{},recall:{},ranking:{}},"
                "presentation:{}}}),{status:200,headers:{'Content-Type':'application/json'}});}"
                "return window.__chatOrigFetch(url,opts)};"
                "document.getElementById('q2').value='为什么推荐它';"
                "document.getElementById('sendbtn2').click();})()"
            )
            await asyncio.sleep(0.35)
            explain_state = await ev(
                "(function(){var bots=document.querySelectorAll('.bub.bot');"
                "return {text:bots[bots.length-1]?.innerText||'',"
                "focus:window.__chatPayload?.focus_item_id||'',before:window.__cardsBefore,"
                "after:document.querySelectorAll('.pcard').length}})()"
            )
            await ev("window.fetch=window.__chatOrigFetch")
            explain_ok = (
                "可核验的商品证据" in str((explain_state or {}).get("text", ""))
                and (explain_state or {}).get("focus") == (await ev("window.__testItemId"))
                and (explain_state or {}).get("before") == (explain_state or {}).get("after")
            )
            add(
                16,
                "追问携带焦点商品并展示推荐理由",
                explain_ok,
                f"payload.focus={((explain_state or {}).get('focus'))}，"
                f"理由已显示={'可核验的商品证据' in str((explain_state or {}).get('text', ''))}，"
                f"货架卡片 {(explain_state or {}).get('before')}→{(explain_state or {}).get('after')}",
            )

            position_state = await ev(
                "(async function(){"
                "const sleep=ms=>new Promise(r=>setTimeout(r,ms));"
                "const order=()=>Array.from(document.querySelectorAll('.pcard')).map(x=>x.dataset.item);"
                "const wait=async()=>{for(let i=0;i<200;i++){if(pendingItems.size===0&&!busy)return;await sleep(25);}};"
                "const card=Array.from(document.querySelectorAll('.pcard')).find(c=>"
                "!ratings.has(c.dataset.item)&&!document.querySelector('[data-undo-feedback=\"'+c.dataset.item+'\"]'));"
                "if(!card)return {ok:false,reason:'no unrated card'};"
                "const id=card.dataset.item;const initialIndex=order().indexOf(id);"
                "card.querySelector('[data-like=\"up\"]').click();await wait();"
                "const afterLike=order().indexOf(id);"
                "document.querySelector('.pcard[data-item=\"'+id+'\"] [data-like=\"down\"]').click();await wait();"
                "const afterDown=order().indexOf(id);"
                "document.querySelector('[data-undo-feedback=\"'+id+'\"][data-feedback-polarity=\"down\"]').click();await wait();"
                "const afterUndo=order().indexOf(id);"
                "document.querySelector('.pcard[data-item=\"'+id+'\"] [data-like=\"up\"]').click();await wait();"
                "const afterLikeAgain=order().indexOf(id);"
                "return {ok:true,id,initialIndex,afterLike,afterDown,afterUndo,afterLikeAgain};})()"
            )
            add(
                17,
                "点赞不改位置；倒赞撤销恢复原位",
                bool((position_state or {}).get("ok"))
                and (position_state or {}).get("afterLike") == (position_state or {}).get("initialIndex")
                and (position_state or {}).get("afterDown") == -1
                and (position_state or {}).get("afterUndo") == (position_state or {}).get("initialIndex")
                and (position_state or {}).get("afterLikeAgain") == (position_state or {}).get("initialIndex"),
                str(position_state),
            )

            root = settings.PROJECT_ROOT
            src = (root / "webui.py").read_text(encoding="utf-8")
            src += "\n".join(
                (root / "web" / name).read_text(encoding="utf-8") for name in ("index.html", "styles.css", "app.js")
            )
            external = [u for u in ("cdn.", "unpkg", "jsdelivr", "googleapis") if u in src]
            add(
                10,
                "未新增第三方前端依赖（项目内静态资源 + 标准库）",
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

    banner("网页改版验收核验（设计文档表 6-1，17 项）")
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
