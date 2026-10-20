# 电商化体验重做 Implementation Plan

> 完成状态：2026-10-20 六个任务全部完成，并补充「举起物品 → 识图 → 推荐」链路；
> 验证结果 234 passed、网页验收 16/16、浏览器 QA SHIP。

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 在不改变六个阶段业务边界的前提下，把现有页面重做成“商品瀑布流 + 推荐助手”的电商导购体验，并消除反馈卡顿、隐藏提示和全局锁屏。

**Architecture:** 先把当前 `webui.py` 内嵌的 HTML/CSS/JS 无损拆成 `web/index.html`、`web/styles.css`、`web/app.js`，再把页面交互改成局部状态更新：反馈只锁当前商品，倒赞即时移除，点赞/倒赞可取消，详情承载证据、对比和继续追问。后端推荐、会话、多模态和六阶段证据链路保持不变。

**Tech Stack:** Python 3.12 标准库 `http.server`、原生 HTML/CSS/JavaScript、现有 Orchestrator/Adapter/EvidencePack，不引入 npm、CDN 或前端框架。

**Spec:** `docs/superpowers/specs/2026-10-20-ecommerce-redesign-design.md`

## Global Constraints

- 终点是阶段5反馈闭环 + 阶段6端到端验收；不新增购物车、结算、支付、订单、物流、地址、会员、优惠券。
- “收藏”用现有点赞/喜欢表达，不新增独立收藏夹。
- 不新增推荐模型或排序算法；模型策略按 spec §5.4 执行。
- 主页面不提供手势按钮；手势只在商品详情内使用，且只接受 👍/👎。
- 保留现有 API 路径和六阶段脚本/证据口径。
- 反馈请求不能锁整个页面，不能同步等待 TTS，不能返回整包解释证据。
- 所有新增前端资源只从项目内 `web/` 读取，禁止路径穿越和 CDN。
- 关键路径从 `config/settings.py` 读取；不硬编码绝对路径、密钥和商品 ID。
- 每次任务结束运行：`ruff check`、`ruff format --check`、`mypy`、目标测试；涉及页面时再跑浏览器验收。

---

### Task 1: 无损拆分前端静态资源

**Files:**
- Create: `web/index.html`
- Create: `web/styles.css`
- Create: `web/app.js`
- Modify: `webui.py:50-1595`（删除内嵌 `PAGE`，改为静态资源服务）
- Test: `tests/test_unit_webui_static.py`

**Interfaces:**
- Consumes: 现有 `PAGE` 中的 HTML、`<style>` 内容和 `<script>` 内容
- Produces: `GET /`、`GET /styles.css`、`GET /app.js`；静态文件只从 `PROJECT_ROOT / "web"` 读取

- [x] **Step 1: 写静态服务失败测试**

```python
# tests/test_unit_webui_static.py
from __future__ import annotations

import threading
import urllib.request
from http.server import ThreadingHTTPServer

from webui import make_handler


def _get(port: int, path: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_static_page_assets_are_served():
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(None))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_port
        status, ctype, body = _get(port, "/")
        assert status == 200
        assert "text/html" in ctype
        assert b'<div id="pgrid"' in body
        status, ctype, body = _get(port, "/styles.css")
        assert status == 200
        assert "text/css" in ctype
        assert b"--brand" in body
        status, ctype, body = _get(port, "/app.js")
        assert status == 200
        assert "javascript" in ctype
        assert b"function openCompare" in body
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
```

- [x] **Step 2: 运行测试确认失败**

Run:

```powershell
python -m pytest tests/test_unit_webui_static.py -q
```

Expected: `/styles.css` 或 `/app.js` 返回 404，测试失败。

- [x] **Step 3: 一次性拆出 PAGE**

在项目根目录临时运行以下脚本（执行后删除脚本）：

```python
from pathlib import Path
import webui

page = webui.PAGE
head, rest = page.split("<style>", 1)
style, rest = rest.split("</style>", 1)
before_script, rest = rest.split("<script>", 1)
script, tail = rest.split("</script>", 1)

index = (
    head.replace("</head>", '<link rel="stylesheet" href="/styles.css">\n</head>')
    + before_script
    + '<script src="/app.js" defer></script>\n'
    + tail
)
Path("web").mkdir(exist_ok=True)
Path("web/index.html").write_text(index, encoding="utf-8")
Path("web/styles.css").write_text(style.strip() + "\n", encoding="utf-8")
Path("web/app.js").write_text(script.strip() + "\n", encoding="utf-8")
```

删除 `webui.py` 中从 `PAGE = r"""` 到对应结束 `"""` 的整段内嵌页面。

- [x] **Step 4: 实现静态文件服务**

在 `webui.py` 顶部加入：

```python
WEB_ROOT = PROJECT_ROOT / "web"
```

在 `Handler` 中加入：

```python
def _static(self, name: str, ctype: str) -> None:
    target = (WEB_ROOT / name).resolve()
    if not target.is_relative_to(WEB_ROOT.resolve()) or not target.is_file():
        self._json(_err(ErrorCode.ITEM_NOT_FOUND, "静态文件不存在", field="path"), 404)
        return
    self._send(200, target.read_bytes(), ctype)
```

在 `do_GET` 的首页分支替换为：

```python
if self.path in ("/", "/index.html"):
    self._static("index.html", "text/html; charset=utf-8")
    return
if self.path == "/styles.css":
    self._static("styles.css", "text/css; charset=utf-8")
    return
if self.path == "/app.js":
    self._static("app.js", "application/javascript; charset=utf-8")
    return
```

- [x] **Step 5: 运行静态测试**

Run:

```powershell
python -m pytest tests/test_unit_webui_static.py -q
```

Expected: PASS。

- [x] **Step 6: 跑完整回归**

Run:

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

Expected: 全部通过；页面行为与拆分前一致。

- [x] **Step 7: 提交**

```powershell
git add web/index.html web/styles.css web/app.js webui.py tests/test_unit_webui_static.py
git commit -m "重构：拆分网页静态资源"
```

---

### Task 2: 对比按钮可见反馈

**Files:**
- Modify: `web/app.js`（`toast`/`openCompare`/`act`）
- Modify: `web/styles.css`（`.toast`）
- Modify: `scripts/13_webui_acceptance.py`
- Test: `scripts/13_webui_acceptance.py`

**Interfaces:**
- Consumes: `openCompare()`、`toast(message)`
- Produces: 可见 toast；0/1 件对比时显示明确提示，2~4 件时打开对比表

- [x] **Step 1: 写验收失败测试**

在 `scripts/13_webui_acceptance.py` 的对比检查前加入：

```python
await ev("document.getElementById('trayClear').click()")
await ev("document.querySelector('[data-act=\"compare\"]').click()")
toast_text = await ev("document.querySelector('.toast.show')?.textContent || ''")
add(
    11,
    "对比不足 2 件时可见提示",
    "至少选择 2 件" in str(toast_text),
    f"toast={toast_text}",
)
```

Expected: 当前 `.sr` toast 不可见，测试失败。

- [x] **Step 2: 实现可见 toast**

在 `web/index.html` 顶部/底部加入：

```html
<div class="toast" id="toast" role="status" aria-live="polite"></div>
```

在 `web/styles.css` 加入：

```css
.toast{
  position:fixed;left:50%;bottom:24px;transform:translate(-50%,12px);
  z-index:120;max-width:min(520px,calc(100vw - 32px));
  padding:10px 16px;border-radius:10px;background:#1b1f30;color:#fff;
  box-shadow:0 12px 30px rgba(15,20,45,.22);font-size:13px;
  opacity:0;pointer-events:none;transition:.18s ease;
}
.toast.show{opacity:1;transform:translate(-50%,0)}
```

在 `web/app.js` 修改 `toast`：

```js
function toast(msg){
  const t = $('toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(window.__tt);
  window.__tt = setTimeout(() => t.classList.remove('show'), 2600);
}
```

- [x] **Step 3: 对比不足时给出明确引导**

在 `openCompare` 开头替换为：

```js
async function openCompare(){
  if (picked.size < 2){
    const need = picked.size === 0 ? '还没有选择商品' : '还需再选 1 件商品';
    toast(need + '：请先在商品卡上点「加入对比」');
    addBot('<div class="slots">要对比商品，请先在商品卡上点「加入对比」，至少选择 2 件。</div>');
    return;
  }
  // ...原有打开对比表逻辑
}
```

- [x] **Step 4: 运行验收与回归**

```powershell
python scripts/13_webui_acceptance.py
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
```

Expected: 对比提示检查 PASS；后续任务全部完成后网页验收应为 16/16。

- [x] **Step 5: 提交**

```powershell
git add web/index.html web/styles.css web/app.js scripts/13_webui_acceptance.py
git commit -m "修复：对比按钮可见反馈"
```

---

### Task 3: 反馈局部 pending 与乐观更新

**Files:**
- Modify: `web/app.js`（`rate`、`handleLiveEvent`、`renderShelf`）
- Modify: `web/styles.css`（`.pcard.pending`）
- Test: `tests/test_unit_webui_hint.py`、`scripts/13_webui_acceptance.py`

**Interfaces:**
- Consumes: `POST /api/feedback`、`POST /api/gesture`、`ratings` Map
- Produces: `setCardPending(itemId, on)`、`applyFeedbackOptimistic(itemId, polarity)`；反馈期间全局 `busy` 保持不变

- [x] **Step 1: 写浏览器回归**

在 `scripts/13_webui_acceptance.py` 的点赞检查中加入：

```python
await ev("document.querySelector('.pcard [data-like=\"up\"]').click()")
busy_after = await ev("busy")
add(
    12,
    "反馈不锁整页",
    busy_after is False,
    f"点赞请求期间 busy={busy_after}",
)
```

Expected: 当前 `rate()` 会调用 `setBusy(true)`，测试失败。

- [x] **Step 2: 实现局部状态**

在 `web/app.js` 增加：

```js
const pendingItems = new Set();

function setCardPending(itemId, on){
  const key = String(itemId);
  if (on) pendingItems.add(key); else pendingItems.delete(key);
  const card = document.querySelector('.pcard[data-item="' + key + '"]');
  if (!card) return;
  card.classList.toggle('pending', on);
  card.querySelectorAll('[data-like],[data-cmp]').forEach(b => { b.disabled = on; });
}
```

`rate` 中移除 `setBusy(true)` / `setBusy(false)`，改为：

```js
async function rate(itemId, polarity, forceCancel){
  const key = String(itemId);
  if (busy || pendingItems.has(key)) return;
  const cancel = !!forceCancel || ratings.get(key) === polarity;
  focusItemId = key;
  setCardPending(key, true);
  const previous = ratings.get(key);
  if (cancel) ratings.delete(key); else ratings.set(key, polarity);
  syncRatingUI(key);
  try {
    const d = await post('/api/feedback', {
      item_id: key,
      polarity: cancel ? 'none' : polarity,
      top_k: pageSize
    });
    if (!d.ok) throw new Error(d.error || '反馈未生效');
    const r = d.response || {};
    if (r.feedback && r.feedback.item_id) {
      ratings.set(String(r.feedback.item_id), r.feedback.polarity === 'positive' ? 'up' : 'down');
    }
    renderBot(r, true);
  } catch (e) {
    if (previous) ratings.set(key, previous); else ratings.delete(key);
    syncRatingUI(key);
    toast('反馈失败：' + e.message);
  } finally {
    setCardPending(key, false);
  }
}
```

新增 `syncRatingUI`：

```js
function syncRatingUI(itemId){
  const key = String(itemId);
  const card = document.querySelector('.pcard[data-item="' + key + '"]');
  if (!card) return;
  card.querySelectorAll('[data-like]').forEach(b => {
    b.classList.toggle('on', b.dataset.like === ratings.get(key));
  });
}
```

倒赞成功时，`renderBot` 会使用后端返回的推荐列表自动移除商品；取消倒赞时会重新出现。

- [x] **Step 3: 样式与提示**

在 `web/styles.css` 加入：

```css
.pcard.pending{opacity:.65;pointer-events:none}
```

在反馈结果气泡中保留「撤销点赞/撤销倒赞」按钮：

```js
if (fb.polarity !== 'none' && fb.item_id){
  h += '<button class="cmpbtn" data-undo-feedback="' + esc(fb.item_id) +
       '" data-feedback-polarity="' + (fb.polarity === 'positive' ? 'up' : 'down') +
       '">撤销' + (fb.polarity === 'positive' ? '点赞' : '倒赞') + '</button>';
}
```

- [x] **Step 4: 运行回归**

```powershell
python -m pytest tests/test_unit_webui_hint.py tests/test_unit_feedback_toggle.py -q
python scripts/13_webui_acceptance.py
python -m pytest -q
```

Expected: 反馈不设置全局 `busy`；点赞/取消/倒赞都通过。

- [x] **Step 5: 提交**

```powershell
git add web/app.js web/styles.css scripts/13_webui_acceptance.py
git commit -m "优化：反馈改局部 pending 与乐观更新"
```

---

### Task 4: 电商页面骨架

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `web/app.js`（只改选择器和布局相关初始化）
- Test: `scripts/13_webui_acceptance.py`

**Interfaces:**
- Consumes: 现有 `#pgrid`、`#stream`、`#toolbar`、`#tray`、`#modal`、`#cmpModal`
- Produces: 桌面两栏布局 `#shelf` + `#assistant`；窄屏底部 Tab；详情模态、对比栏保持

- [x] **Step 1: 写布局验收**

在 `scripts/13_webui_acceptance.py` 检查：

```python
layout = await ev(
    "(function(){var s=document.getElementById('shelf'),a=document.getElementById('assistant');"
    "return s&&a?s.getBoundingClientRect().width>a.getBoundingClientRect().width:false})()"
)
add(13, "电商两栏布局", bool(layout), f"瀑布流宽于助手={layout}")
```

Expected: 当前布局不具备 `#shelf` / `#assistant` 结构，测试失败。

- [x] **Step 2: 重写 HTML 骨架**

`web/index.html` 的主体调整为：

```html
<header class="appbar">...</header>
<main class="commerce">
  <section class="shelf" id="shelf">
    <div class="resultbar">...</div>
    <div class="pgrid" id="pgrid"></div>
    <button id="loadMore">加载更多商品</button>
  </section>
  <aside class="assistant" id="assistant" aria-label="推荐助手">
    <div class="assistant-head">推荐助手</div>
    <div class="stream" id="stream"></div>
    <form class="composer">...</form>
  </aside>
</main>
<div class="tray" id="tray">...</div>
<div class="modal" id="modal">...</div>
<div class="modal" id="cmpModal">...</div>
<div class="toast" id="toast" role="status" aria-live="polite"></div>
```

- [x] **Step 3: 布局样式**

在 `web/styles.css` 设置：

```css
.commerce{
  display:grid;grid-template-columns:minmax(0,1fr) minmax(320px,380px);
  gap:18px;max-width:1440px;margin:0 auto;padding:18px;
}
.shelf{min-width:0}
.assistant{position:sticky;top:72px;height:calc(100vh - 92px);min-width:0}
.pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
@media(max-width:1023px){
  .commerce{grid-template-columns:1fr;padding-bottom:76px}
  .assistant{position:fixed;inset:auto 0 0 0;height:70vh;transform:translateY(100%);z-index:80}
  .assistant.open{transform:translateY(0)}
}
```

- [x] **Step 4: 更新 JS 选择器**

保持 `renderShelf` 目标仍是 `#pgrid`，消息流仍是 `#stream`。把旧布局中依赖 `.shelf`/`.panel` 的引用统一到 `#shelf`/`#assistant`，并保留 `#toolbar`、`#tray`、`#modal`、`#cmpModal` id。

- [x] **Step 5: 运行视觉与功能回归**

```powershell
python scripts/13_webui_acceptance.py
python scripts/17_browser_qa.py
```

Expected: 桌面两栏、窄屏抽屉、无横向溢出；对比和反馈仍可用。

- [x] **Step 6: 提交**

```powershell
git add web/index.html web/styles.css web/app.js scripts/13_webui_acceptance.py
git commit -m "界面：重构为电商两栏骨架"
```

---

### Task 5: 详情、对比与继续追问

**Files:**
- Modify: `web/index.html`
- Modify: `web/styles.css`
- Modify: `web/app.js`（`openDetail`、`openCompare`、`toggleCompare`）
- Test: `scripts/13_webui_acceptance.py`

**Interfaces:**
- Consumes: `POST /api/view`、`GET /api/product/{id}`、`POST /api/products`、`POST /api/feedback`
- Produces: 详情模态中的证据、解释、点赞/倒赞、加入对比、追问按钮

- [x] **Step 1: 详情验收**

```python
detail = await ev(
    "document.querySelector('.pcard [data-open]').click(); "
    "document.getElementById('modal').classList.contains('show')"
)
has_reason = await ev("!!document.querySelector('#modal [data-reason]')")
add(14, "商品详情可打开且含推荐理由", bool(detail) and bool(has_reason), f"detail={detail}, reason={has_reason}")
```

- [x] **Step 2: 详情结构**

详情模态使用：

```html
<div class="detail">
  <div class="detail-media">...</div>
  <div class="detail-info">
    <h2>...</h2>
    <div class="price">...</div>
    <div data-reason>...</div>
    <div class="evidence-list">...</div>
    <div class="detail-actions">
      <button data-rate="up">👍 喜欢</button>
      <button data-rate="down">👎 不喜欢</button>
      <button data-cmp2>加入对比</button>
      <button data-ask-product>追问此商品</button>
    </div>
  </div>
</div>
```

- [x] **Step 3: 追问行为**

```js
const ask = $('sheet').querySelector('[data-ask-product]');
if (ask) ask.onclick = () => {
  focusItemId = String(itemId);
  closeAll();
  $('q2').focus();
  toast('已选中当前商品，可以继续追问');
};
```

- [x] **Step 4: 详情内点赞/倒赞复用 `rate`**

```js
$('sheet').querySelectorAll('[data-rate]').forEach(el => {
  el.onclick = ev => {
    ev.stopPropagation();
    rate(itemId, el.dataset.rate);
  };
});
```

- [x] **Step 5: 对比保持当前字段表**

保留 `openCompare` 的 7 行字段；把差异高亮和底部对比栏保留。对比不足 2 件时使用 Task 2 的可见提示。

- [x] **Step 6: 运行回归**

```powershell
python scripts/13_webui_acceptance.py
python -m pytest -q
```

Expected: 详情、对比、追问、反馈全部可用。

- [x] **Step 7: 提交**

```powershell
git add web/index.html web/styles.css web/app.js
git commit -m "界面：完善详情、对比与继续追问"
```

---

### Task 6: 六阶段回归、文档与收尾

**Files:**
- Modify: `README.md`
- Modify: `docs/manual.md`
- Modify: `docs/deployment.md`
- Modify: `scripts/13_webui_acceptance.py`
- Test: 全部六阶段脚本 + pytest + 浏览器验收

**Interfaces:**
- Consumes: 前五个任务的页面与反馈行为
- Produces: 可复现的六阶段交付包与更新后的页面说明

- [x] **Step 1: 更新页面说明**

README 和 docs 更新：

- 主页面是商品瀑布流 + 推荐助手
- 对比需要 2 件，最多 4 件
- 收藏用点赞表达
- 倒赞即时移除，消息中可撤销
- 主页面无手势按钮，手势只在详情
- 不提供购物车/支付/订单

- [x] **Step 2: 跑完整验收**

```powershell
python -B scripts/run_all_offline.py
python -m pytest -q
python -m ruff check .
python -m ruff format --check .
python -m mypy
python scripts/18_contract_check.py
python scripts/13_webui_acceptance.py
python scripts/17_browser_qa.py
```

Expected: 9/9、全部 pytest、16/16、16/16、浏览器无 console error。

- [x] **Step 3: 清理可再生文件**

```powershell
python scripts/clean_workspace.py
```

不要删除 `web/`、源码、文档或数据。

- [x] **Step 4: 提交**

```powershell
git add README.md docs/manual.md docs/deployment.md scripts/13_webui_acceptance.py
git commit -m "文档：更新电商化页面与验收说明"
```

## Self-Review

- [x] 设计文档 §3.1 两栏布局由 Task 4 覆盖。
- [x] 设计文档 §3.3 商品卡由 Task 4 覆盖。
- [x] 设计文档 §3.4 详情与追问由 Task 5 覆盖。
- [x] 设计文档 §3.5 对比与可见提示由 Task 2、Task 5 覆盖。
- [x] 设计文档 §4 反馈局部化、取消、倒赞移除由 Task 3 覆盖。
- [x] 设计文档 §5 静态拆分/API 复用由 Task 1、Task 4 覆盖。
- [x] 设计文档 §5.4 不换主模型、VL 按需加载由 Task 3/6 的回归口径保留。
- [x] 设计文档 §8 六阶段自动/手工验收由 Task 6 覆盖。
- [x] 不新增购物车、支付、订单、独立收藏夹；全文无相关实现任务。
