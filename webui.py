"""
webui.py —— 多模态个性化推荐助手 · 网页对话界面
=================================================

在浏览器里和系统对话（不再是终端，也不再是写死的脚本）。

零新增依赖：只用 Python 标准库 http.server 起服务，因此保持项目「离线可跑」的原则。
（对比：推荐系统前端用的是 FastAPI，需要额外装包；本页面不需要。）

页面能力：
    文本对话      输入框直接问
    🎤 语音      点按钮录 4 秒真实麦克风 → ASR → 对话（需 --real）
    ✋ 手势      商品详情内自动开启手势直播，👍→点赞、👎→倒赞（需 --real）
    👁 画面      抓帧做视觉理解（需 --real）
    ↩ 撤销 / ⟳ 重来 / 会话状态 / LLM 开关

用法：
    python webui.py                      # http://127.0.0.1:8020
    python webui.py --real               # 启用麦克风/摄像头/真实模型
    python webui.py --real --llm         # 再用真实 LLM 生成解释
    python webui.py --port 8020 --user 14156
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from src.errors import ErrorCode  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.presentation import synthesize  # noqa: E402

PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多模态个性化推荐助手</title>
<style>
  /* ============ 设计令牌 v2（表 3-2） ============ */
  :root{
    --brand:#4f46e5; --brand-ink:#3730a3; --brand-soft:#eef0fe; --brand-tint:#f6f7ff;
    --bg:#f6f7fb; --card:#fff;
    --line:#e8eaf2; --line-strong:#d7dbe8;
    --ink:#1b1f30; --muted:#8b91a6;
    --pos:#0f9d58; --pos-bg:#e8f6ee;
    --warn:#b45309; --warn-bg:#fdf4e6;
    --bad:#dc2626; --bad-bg:#fdeceb;
    --r-sm:8px; --r:10px; --r-md:12px; --r-lg:16px; --r-xl:18px;
    --sp:4px;
    --shadow:0 1px 2px rgba(18,22,50,.05),0 4px 16px rgba(18,22,50,.05);
    --shadow-lg:0 12px 40px rgba(15,20,45,.16);
    --appbar-h:56px;
  }
  *{box-sizing:border-box}
  html,body{margin:0;padding:0}
  body{background:var(--bg);color:var(--ink);
       font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif;
       -webkit-font-smoothing:antialiased}
  button,input,select{font-family:inherit;font-size:14px}
  button{cursor:pointer}
  :focus-visible{outline:2px solid var(--brand);outline-offset:2px;border-radius:4px}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

  /* ============ 顶栏 AppBar（单行 56px） ============ */
  .appbar{position:sticky;top:0;z-index:40;height:var(--appbar-h);
          display:flex;align-items:center;gap:12px;padding:0 20px;
          background:rgba(255,255,255,.94);backdrop-filter:blur(12px);
          border-bottom:1px solid var(--line)}
  .brand{display:flex;align-items:center;gap:9px;flex:0 1 auto;min-width:0}
  .logo{width:30px;height:30px;border-radius:9px;background:var(--brand);color:#fff;
        display:grid;place-items:center;font-weight:700;font-size:15px;flex:none}
  .brand b{font-size:15px;font-weight:650;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  /* P0：搜索框可收缩，不再用 min-width 撑开容器 */
  .search{flex:1 1 auto;display:flex;gap:8px;min-width:0}
  .search input{flex:1 1 auto;min-width:0;padding:9px 14px;border:1.5px solid var(--line);
        border-radius:var(--r);outline:none;transition:border-color .15s,box-shadow .15s}
  .search input:focus{border-color:#c7cbef;box-shadow:0 0 0 3px rgba(79,70,229,.1)}
  .search button{flex:none;padding:9px 18px;border-radius:var(--r);border:1px solid var(--brand);
        background:var(--brand);color:#fff;font-weight:600}
  .search button:hover:not(:disabled){background:#4338ca}
  .search button:disabled{opacity:.5;cursor:not-allowed}
  /* P0：chips 允许换行且可收缩 */
  .chips{display:flex;gap:6px;flex-wrap:wrap;flex:0 1 auto;min-width:0}
  .chip{background:var(--brand-soft);color:var(--brand-ink);border-radius:999px;
        padding:2px 10px;font-size:12px;white-space:nowrap}
  .chip.dim{background:#f1f2f7;color:var(--muted)}
  .appbar .right{display:flex;align-items:center;gap:8px;flex:none;position:relative}
  .usermenu{position:absolute;top:calc(100% + 8px);right:0;min-width:236px;background:#fff;
        border:1px solid var(--line);border-radius:12px;box-shadow:0 14px 36px rgba(20,24,45,.16);
        padding:6px;display:none;z-index:70}
  .usermenu.show{display:block}
  .usermenu button{display:flex;width:100%;align-items:center;gap:9px;padding:9px 10px;border:0;
        border-radius:9px;background:transparent;text-align:left;cursor:pointer}
  .usermenu button:hover{background:#f3f4fa}
  .usermenu .uava2{width:24px;height:24px;border-radius:50%;display:grid;place-items:center;
        color:#fff;font-size:12px;flex:none}
  .usermenu .nm{font-weight:600;color:var(--ink);font-size:13.5px}
  .usermenu .ps{font-size:11.5px;color:var(--muted)}
  .usermenu .cur{margin-left:auto;font-size:11.5px;color:var(--brand)}
  .userbtn{display:flex;align-items:center;gap:6px;padding:6px 11px;border-radius:999px;
        border:1px solid var(--line);background:#fff;white-space:nowrap}
  .userbtn:hover{background:#f3f4fa}
  .avatar{width:20px;height:20px;border-radius:50%;background:var(--brand);color:#fff;
        font-size:11px;display:grid;place-items:center;flex:none}
  .iconbtn{width:34px;height:34px;border-radius:var(--r);border:1px solid var(--line);
        background:#fff;display:grid;place-items:center;font-size:15px;color:#5b6178}
  .iconbtn:hover{background:#f3f4fa}

  /* ============ 工具条 ============ */
  .toolbar{position:sticky;top:var(--appbar-h);z-index:35;display:flex;align-items:center;
        gap:7px;flex-wrap:wrap;padding:9px 20px;background:rgba(255,255,255,.9);
        backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
  .toolbar button{display:flex;align-items:center;gap:5px;padding:6px 12px;border-radius:var(--r-sm);
        border:1px solid var(--line);background:#fff;color:var(--ink);transition:.14s}
  .toolbar button:hover:not(:disabled){background:#f3f4fa;border-color:var(--line-strong)}
  .toolbar button:disabled{opacity:.42;cursor:not-allowed}
  .toolbar button.on{background:var(--brand-soft);border-color:#cfd3f5;color:var(--brand-ink)}
  .toolbar .sep{width:1px;height:18px;background:var(--line);margin:0 3px}
  .toolbar .ghost{margin-left:auto;border-style:dashed;color:var(--muted)}
  .pill{min-width:18px;height:18px;padding:0 5px;border-radius:999px;background:var(--brand);
        color:#fff;font-size:11px;display:none;place-items:center}
  .pill.show{display:inline-grid}

  /* ============ 主体两栏 ============ */
  .layout{max-width:1560px;margin:0 auto;padding:16px 20px 96px;
          display:grid;grid-template-columns:minmax(0,1fr) 400px;gap:16px;align-items:start}
  .shelf{background:var(--card);border:1px solid var(--line);border-radius:var(--r-lg);
         box-shadow:var(--shadow);min-height:420px;overflow:hidden}

  /* ---- 结果条 ResultBar ---- */
  .resultbar{padding:13px 16px;border-bottom:1px solid var(--line);background:#fcfcfe}
  .rbtop{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  .rbtop h2{margin:0;font-size:16px;font-weight:660;white-space:nowrap}
  .rbtop .cnt{margin-left:auto;font-size:12.5px;color:var(--muted);white-space:nowrap}
  .fchips{display:flex;gap:6px;flex-wrap:wrap;min-width:0}
  .fchip{display:inline-flex;align-items:center;gap:5px;background:var(--brand-tint);
         border:1px solid #dfe3fb;color:var(--brand-ink);border-radius:999px;
         padding:2px 6px 2px 10px;font-size:12px;white-space:nowrap}
  .fchip button{border:none;background:none;color:#9aa0bd;font-size:13px;line-height:1;
         padding:0 2px;border-radius:50%}
  .fchip button:hover{color:var(--bad)}
  .rbbot{display:flex;align-items:center;gap:9px;margin-top:9px;flex-wrap:wrap}
  .rbbot select{padding:5px 9px;border:1px solid var(--line);border-radius:var(--r-sm);
         background:#fff;color:var(--ink);outline:none}
  .vswitch{display:flex;border:1px solid var(--line);border-radius:var(--r-sm);overflow:hidden}
  .vswitch button{border:none;background:#fff;padding:5px 10px;font-size:13px;color:var(--muted)}
  .vswitch button.on{background:var(--brand-soft);color:var(--brand-ink)}
  .rbbot .sp{flex:1}

  /* ---- 商品网格 ---- */
  .pgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(196px,1fr));gap:14px;padding:16px}
  .pgrid.list{grid-template-columns:1fr}
  .pgrid.list .pcard{flex-direction:row;align-items:stretch}
  .pgrid.list .pimg{width:132px;flex:none;aspect-ratio:auto}
  .pgrid.list .pbody{flex:1}
  .pgrid.list .pcard h3{-webkit-line-clamp:3;min-height:0}
  .loadmore{display:block;width:calc(100% - 32px);margin:0 16px 16px;padding:11px;border:1px dashed var(--line-strong);
            border-radius:10px;background:#fff;color:var(--brand-ink);font-size:13px;cursor:pointer}
  .loadmore:hover{background:var(--brand-soft)}
  .loadmore[hidden]{display:none}
  .loadmore:disabled{opacity:.6;cursor:wait}
  .pcard{position:relative;border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;
         background:#fff;display:flex;flex-direction:column;
         transition:box-shadow .16s,border-color .16s,transform .1s}
  .pcard:hover{border-color:var(--line-strong);box-shadow:0 8px 24px rgba(18,22,50,.1);transform:translateY(-2px)}
  .pcard.picked{border-color:var(--brand);box-shadow:0 0 0 2px var(--brand-soft)}
  .pimg{position:relative;aspect-ratio:1/1;background:#f4f5fa;overflow:hidden;cursor:pointer}
  .pimg .ph{position:absolute;inset:0;display:grid;place-items:center;font-size:38px;color:#c6cadd}
  .pimg img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;display:block}
  /* TOP N 缎带 */
  .rank{position:absolute;top:0;left:0;background:var(--brand);color:#fff;font-size:11px;
        font-weight:700;letter-spacing:.3px;padding:3px 10px 3px 8px;
        border-bottom-right-radius:var(--r-sm);box-shadow:0 2px 8px rgba(79,70,229,.35)}
  .rank.plain{background:rgba(27,31,48,.7);box-shadow:none}
  /* P1-3：操作常驻（触屏可达） */
  .pacts{position:absolute;top:7px;right:7px;display:flex;gap:5px}
  .pacts button{width:30px;height:30px;border-radius:50%;border:none;background:rgba(255,255,255,.95);
        font-size:14px;line-height:1;display:grid;place-items:center;
        box-shadow:0 1px 6px rgba(0,0,0,.18);transition:transform .12s,background .12s}
  .pacts button:hover{transform:scale(1.14)}
  .pacts button.on{background:var(--brand-soft);box-shadow:0 0 0 2px var(--brand)}
  .pbody{padding:10px 11px 11px;display:flex;flex-direction:column;gap:5px;flex:1;min-width:0}
  .pbody h3{margin:0;font-size:13.5px;font-weight:560;line-height:1.45;cursor:pointer;
        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;min-height:2.9em}
  .pbody h3:hover{color:var(--brand-ink)}
  .prow{display:flex;align-items:baseline;gap:7px;flex-wrap:wrap}
  .pprice{color:var(--bad);font-weight:750;font-size:16px}
  .pmatch{font-size:11.5px;color:var(--pos);background:var(--pos-bg);border-radius:var(--r-sm);
        padding:1px 7px;font-weight:600}
  .ptags{display:flex;gap:5px;flex-wrap:wrap;margin-top:auto;padding-top:5px}
  .t{border-radius:var(--r-sm);padding:1px 7px;font-size:11px;background:#f2f3f8;color:#5c6379}
  .t.ev{background:var(--pos-bg);color:var(--pos)}
  .t.hit{background:var(--warn-bg);color:var(--warn)}
  .t.more{background:#fff;border:1px dashed var(--line-strong);color:var(--muted)}
  .cmpbtn{margin-top:7px;width:100%;padding:6px;border-radius:var(--r-sm);
        border:1px solid var(--line);background:#fff;color:#5c6379;font-size:12.5px}
  .cmpbtn:hover{border-color:var(--brand);color:var(--brand-ink);background:var(--brand-tint)}
  .pcard.picked .cmpbtn{background:var(--brand-soft);border-color:var(--brand);color:var(--brand-ink)}

  /* ---- 空态与骨架 ---- */
  .empty{padding:56px 20px;text-align:center;color:var(--muted);font-size:14px}
  .examples{display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-top:14px}
  .examples button{border:1px solid var(--line);background:#fff;border-radius:999px;
        padding:7px 14px;font-size:13px;color:#4a5068}
  .examples button:hover{border-color:var(--brand);color:var(--brand-ink);background:var(--brand-tint)}
  .skel{border:1px solid var(--line);border-radius:var(--r-md);overflow:hidden;background:#fff}
  .sk1{aspect-ratio:1/1;background:linear-gradient(100deg,#f1f2f7 30%,#e7e9f3 50%,#f1f2f7 70%);
       background-size:200% 100%;animation:shim 1.3s infinite linear}
  .sk2{height:12px;margin:11px;border-radius:6px;background:#f1f2f7}
  .sk3{height:12px;margin:0 11px 14px;width:60%;border-radius:6px;background:#f1f2f7}
  @keyframes shim{from{background-position:200% 0}to{background-position:-200% 0}}
  .skb{display:flex;gap:8px;align-items:center;padding:11px 13px;border:1px solid var(--line);
       border-radius:var(--r-md);background:#fff}
  .skdot{width:9px;height:9px;border-radius:50%;background:#e2e4ee;animation:pulse 1.2s infinite}
  @keyframes pulse{0%,100%{opacity:.4}50%{opacity:1}}

  /* ============ 助手面板 ============ */
  .panel{background:var(--card);border:1px solid var(--line);border-radius:var(--r-lg);
         box-shadow:var(--shadow);display:flex;flex-direction:column;
         height:calc(100vh - var(--appbar-h) - 130px);min-height:440px;
         position:sticky;top:calc(var(--appbar-h) + 58px);overflow:hidden}
  .phead{display:flex;align-items:center;gap:8px;padding:12px 15px;border-bottom:1px solid var(--line)}
  .phead b{font-size:14.5px;font-weight:650}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--pos);flex:none;
       box-shadow:0 0 0 3px var(--pos-bg)}
  .dot.busy{background:var(--warn);box-shadow:0 0 0 3px var(--warn-bg);animation:pulse 1s infinite}
  .phead .live{font-size:11.5px;color:var(--muted);margin-left:2px}
  .phead .clr{margin-left:auto;border:none;background:none;color:var(--muted);font-size:12.5px;
       padding:4px 8px;border-radius:var(--r-sm)}
  .phead .clr:hover{background:#f3f4fa;color:var(--ink)}
  .stream{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:11px;
       scroll-behavior:smooth}
  .msg{display:flex;animation:pop .2s ease-out}
  @keyframes pop{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}
  .msg.user{justify-content:flex-end}
  .bub{max-width:93%;border-radius:var(--r-md);padding:9px 13px;font-size:13.5px;line-height:1.62;
       background:#f6f7fc;border:1px solid var(--line);word-break:break-word}
  .msg.user .bub{background:var(--brand);color:#fff;border-color:var(--brand);border-bottom-right-radius:4px}
  .bub.bot{background:#fff;border-bottom-left-radius:4px}
  /* 状态三重编码：圆点 + 图标 + 文字（P2-10） */
  .st{display:inline-flex;align-items:center;gap:5px;font-size:11.5px;border-radius:999px;
      padding:2px 9px;font-weight:600;white-space:nowrap}
  .st i{width:6px;height:6px;border-radius:50%;flex:none}
  .st.ok{background:var(--pos-bg);color:var(--pos)} .st.ok i{background:var(--pos)}
  .st.degraded,.st.warn{background:var(--warn-bg);color:var(--warn)} .st.degraded i,.st.warn i{background:var(--warn)}
  .st.clarification,.st.conflict{background:var(--warn-bg);color:var(--warn)} .st.clarification i,.st.conflict i{background:var(--warn)}
  .st.empty,.st.no_evidence{background:#f1f2f7;color:var(--muted)} .st.empty i,.st.no_evidence i{background:var(--muted)}
  .st.error{background:var(--bad-bg);color:var(--bad)} .st.error i{background:var(--bad)}
  .rh{display:flex;gap:6px;align-items:center;flex-wrap:wrap;margin-bottom:6px}
  .rh .k{font-size:11.5px;color:var(--muted)}
  .rh .k b{color:var(--ink);font-weight:600}
  .slots{display:flex;gap:5px;flex-wrap:wrap;margin-top:6px}
  .slotchip{border:1px solid var(--line);background:#fff;border-radius:999px;padding:2px 9px;
        font-size:11.5px;color:#4a5068}
  .slotchip:hover{border-color:var(--brand);color:var(--brand-ink);background:var(--brand-tint)}
  .errline{color:var(--bad);font-size:13px}
  .bub audio{width:100%;height:32px;margin-top:7px}
  .fold{margin-top:7px;border-top:1px dashed var(--line);padding-top:6px}
  .fold>button{border:none;background:none;color:var(--brand-ink);font-size:12px;padding:0}
  .fold .body{display:none;margin-top:6px;font-size:12px;color:var(--muted);line-height:1.75}
  .fold.open .body{display:block}
  .sugg{display:flex;gap:6px;flex-wrap:wrap;margin-top:8px}
  .sugg button{border:1px solid var(--line);background:#fff;border-radius:var(--r-sm);
        padding:5px 10px;font-size:12px;color:var(--brand-ink)}
  .sugg button:hover{background:var(--brand-tint);border-color:var(--brand)}
  .jump{position:absolute;right:16px;bottom:74px;border:1px solid var(--line);background:#fff;
        border-radius:999px;padding:6px 13px;font-size:12.5px;color:var(--brand-ink);
        box-shadow:var(--shadow);display:none}
  .jump.show{display:block}
  .pfoot{border-top:1px solid var(--line);padding:10px 13px 12px;display:flex;gap:8px}
  .pfoot input{flex:1;min-width:0;padding:9px 12px;border:1px solid var(--line);
        border-radius:var(--r);outline:none}
  .pfoot input:focus{border-color:#c7cbef;box-shadow:0 0 0 3px rgba(79,70,229,.09)}
  .pfoot button{padding:9px 15px;border-radius:var(--r);border:1px solid var(--brand);
        background:var(--brand);color:#fff;font-weight:600}
  .pfoot button:hover:not(:disabled){background:#4338ca}
  .pfoot button:disabled{opacity:.5;cursor:not-allowed}

  /* 操作卡 */
  .act{display:flex;gap:10px;align-items:flex-start;background:#fff;border:1px solid var(--line);
       border-left:3px solid var(--muted);border-radius:var(--r-md);padding:10px 12px;
       font-size:13px;width:100%}
  .act.pending{border-left-color:var(--brand)} .act.ok{border-left-color:var(--pos)}
  .act.warn{border-left-color:var(--warn)} .act.err{border-left-color:var(--bad)}
  .act .t{font-weight:600} .act .d{color:var(--muted);font-size:12.5px;margin-top:2px}
  .act .d b{color:var(--ink)}
  .act .shotwrap{display:block;line-height:0;margin-top:8px;cursor:zoom-in}
  .act .shot{display:block;width:100%;max-width:520px;border-radius:10px;border:1px solid var(--line);
             background:#0f1117}
  .act .shotcap{font-size:11.5px;color:var(--muted);margin-top:3px}
  .ttsbtn{border:1px solid #d9d1ff;border-radius:8px;padding:5px 10px;background:#f5f2ff;color:#5b3fd6;
          font-size:12px;cursor:pointer;margin-top:6px}
  .ttsbtn:disabled{opacity:.6;cursor:wait}
  .mgesture{margin-top:10px;font-size:12.5px}
  .mgesture .mstatus{padding:6px 9px;border-radius:8px;background:#f7f8fc;color:var(--muted)}
  .mgesture .mstatus.ok{background:#eafaf0;color:#0a7b43}
  .mgesture .mstatus.err{background:#fff1f1;color:#b42318}
  .mgesture .shot{display:block;width:100%;max-width:420px;margin-top:6px;border-radius:8px;border:1px solid var(--line)}
  .mgesture .livehead{display:flex;justify-content:space-between;align-items:center;font-weight:600;margin-bottom:6px}
  .mgesture .livecam{display:block;width:100%;max-width:560px;border-radius:10px;border:1px solid var(--line);background:#000}
  .mgesture .livestat{font-size:12px;color:var(--muted);margin-top:5px}
  .mgesture .livehint{font-size:11.5px;color:var(--muted);margin-top:2px}
  .mgesture button[data-stop-live]{border:1px solid var(--line);border-radius:8px;background:#fff;color:var(--muted);padding:4px 10px;font-size:12px;cursor:pointer}
  .badge{margin-left:auto;flex:none;font-size:11px;border-radius:999px;padding:1.5px 8px;
         background:#f1f2f7;color:var(--muted);white-space:nowrap}
  .badge.pending{background:var(--brand-soft);color:var(--brand-ink)}
  .badge.ok{background:var(--pos-bg);color:var(--pos)}
  .badge.warn{background:var(--warn-bg);color:var(--warn)}
  .badge.err{background:var(--bad-bg);color:var(--bad)}
  .spin{width:12px;height:12px;border:2px solid #dcdff6;border-top-color:var(--brand);
        border-radius:50%;display:inline-block;vertical-align:-2px;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  /* ============ 对比栏 CompareTray ============ */
  .tray{position:fixed;left:0;right:0;bottom:0;z-index:45;background:rgba(255,255,255,.97);
        backdrop-filter:blur(12px);border-top:1px solid var(--line-strong);
        box-shadow:0 -6px 24px rgba(15,20,45,.1);padding:11px 20px;
        transform:translateY(110%);transition:transform .24s cubic-bezier(.4,0,.2,1)}
  .tray.show{transform:none}
  .tray .in{max-width:1560px;margin:0 auto;display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .tray .lab{font-weight:650;font-size:13.5px;white-space:nowrap}
  .tray .items{display:flex;gap:8px;flex:1;min-width:0;flex-wrap:wrap}
  .titem{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:var(--r);
        padding:4px 8px 4px 5px;background:#fff;max-width:230px}
  .titem .th{width:30px;height:30px;border-radius:6px;background:#f1f2f8;overflow:hidden;
        display:grid;place-items:center;font-size:15px;flex:none;position:relative}
  .titem .th img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .titem .nm{font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .titem .pr{color:var(--bad);font-weight:700;font-size:12.5px}
  .titem button{border:none;background:none;color:#9aa0bd;font-size:13px;padding:0 2px}
  .titem button:hover{color:var(--bad)}
  .tray .acts{display:flex;gap:8px;flex:none}
  .tray .acts button{padding:8px 16px;border-radius:var(--r);border:1px solid var(--line);background:#fff}
  .tray .acts .go{background:var(--brand);border-color:var(--brand);color:#fff;font-weight:600}
  .tray .acts .go:hover:not(:disabled){background:#4338ca}
  .tray .acts button:disabled{opacity:.45;cursor:not-allowed}

  /* ============ 底部 Tab（窄屏） ============ */
  .tabbar{display:none;position:fixed;left:0;right:0;bottom:0;z-index:50;
        background:rgba(255,255,255,.97);backdrop-filter:blur(12px);
        border-top:1px solid var(--line)}
  .tabbar.is-on{display:flex}
  .tabbar button{flex:1 1 0;min-width:0;border:none;background:none;padding:8px 4px 10px;
        color:var(--muted);font-size:11.5px;display:flex;flex-direction:column;align-items:center;
        gap:1px;line-height:1.25}
  .tabbar button b{font-size:17px;font-weight:400}
  .tabbar button.on{color:var(--brand-ink)}

  /* 悬浮抽屉按钮（≤1023px） */
  .fab{display:none;position:fixed;right:16px;bottom:78px;z-index:48;width:50px;height:50px;
       border-radius:50%;border:none;background:var(--brand);color:#fff;font-size:21px;
       box-shadow:0 8px 24px rgba(79,70,229,.4);place-items:center}
  .fab.show{display:grid}

  /* ============ 抽屉（运行状态 / 窄屏助手） ============ */
  .drawer{position:fixed;top:0;right:0;bottom:0;width:420px;max-width:92vw;z-index:60;
        background:#fff;box-shadow:var(--shadow-lg);transform:translateX(105%);
        transition:transform .26s cubic-bezier(.4,0,.2,1);display:flex;flex-direction:column}
  .drawer.show{transform:none}
  .drawer .dh{display:flex;align-items:center;gap:9px;padding:14px 16px;border-bottom:1px solid var(--line)}
  .drawer .dh b{font-size:15px;font-weight:650}
  .drawer .dh button{margin-left:auto;border:none;background:#f1f2f7;width:30px;height:30px;
        border-radius:50%;font-size:15px;color:#6b7189}
  .drawer .db{padding:14px 16px;overflow:auto;flex:1}
  .kvrow{display:flex;gap:10px;padding:7px 0;border-bottom:1px dashed var(--line);font-size:13px}
  .kvrow:last-child{border-bottom:none}
  .kvrow .k{color:var(--muted);flex:none;width:96px}
  .kvrow .v{font-weight:560;word-break:break-all}
  .mask{position:fixed;inset:0;z-index:55;background:rgba(20,24,46,.42);display:none;
        backdrop-filter:blur(2px)}
  .mask.show{display:block}

  /* ============ 弹窗 ============ */
  .modal{position:fixed;inset:0;z-index:70;background:rgba(20,24,46,.5);display:none;
        align-items:center;justify-content:center;padding:24px;backdrop-filter:blur(3px)}
  .modal.show{display:flex}
  .sheet{background:#fff;border-radius:var(--r-xl);max-width:900px;width:100%;max-height:90vh;
        overflow:auto;box-shadow:var(--shadow-lg);position:relative}
  .sheet .top{display:flex;gap:22px;padding:22px 24px 18px}
  .sheet .big{position:relative;width:290px;height:290px;border-radius:var(--r-lg);
        background:#f4f5fa;flex:none;overflow:hidden}
  .sheet .big .ph{position:absolute;inset:0;display:grid;place-items:center;font-size:74px;color:#c6cadd}
  .sheet .big img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .sheet .meta{flex:1;min-width:0}
  .sheet h2{margin:0 0 4px;font-size:20px;line-height:1.4}
  .sheet .en{color:var(--muted);font-size:12.5px;margin-bottom:13px}
  .kv{display:grid;grid-template-columns:auto 1fr;gap:7px 16px;font-size:13.5px;
      border-top:1px solid var(--line);padding-top:13px}
  .kv dt{color:var(--muted)} .kv dd{margin:0;font-weight:560}
  .kv .p{color:var(--bad);font-size:19px;font-weight:750}
  .sec{padding:15px 24px;border-top:1px solid var(--line)}
  .sec h3{margin:0 0 11px;font-size:13.5px;color:var(--muted);font-weight:600}
  .reason{background:#f7f8fd;border-left:3px solid #dfe2f2;border-radius:0 9px 9px 0;
        padding:11px 14px;font-size:13.5px;line-height:1.72;color:#3a3f56}
  .mini{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}
  .mini .m{border:1px solid var(--line);border-radius:var(--r);overflow:hidden;cursor:pointer;background:#fff}
  .mini .m:hover{border-color:var(--line-strong);box-shadow:0 4px 14px rgba(18,22,50,.08)}
  .mini .mi{position:relative;aspect-ratio:1/1;background:#f4f5fa;overflow:hidden}
  .mini .mi .ph{position:absolute;inset:0;display:grid;place-items:center;font-size:32px;color:#c6cadd}
  .mini .mi img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
  .mini p{margin:0;padding:7px 9px 3px;font-size:12.5px;line-height:1.45;
        display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  .mini span{display:block;padding:0 9px 8px;font-size:13px;color:var(--bad);font-weight:700}
  .x{position:absolute;top:12px;right:14px;border:none;background:#f1f2f7;width:32px;height:32px;
     border-radius:50%;font-size:15px;color:#6b7189;z-index:2}
  .x:hover{background:#e7e9f2}
  /* 对比表 */
  .cmptable{width:100%;border-collapse:collapse;font-size:13px}
  .cmptable th,.cmptable td{border:1px solid var(--line);padding:8px 10px;text-align:left;vertical-align:top}
  .cmptable th{background:#fafbfd;font-weight:600;color:#4a5068;white-space:nowrap}
  .cmptable td.diff{background:var(--warn-bg)}
  .cmpimg{width:56px;height:56px;border-radius:var(--r-sm);object-fit:cover;background:#f1f2f8}

  /* ============ 响应式（P0 的关键修复） ============ */
  /* 641–815px 溢出的根因：该区间没隐藏品牌文字与 chips，又放不下三者 */
  @media (max-width:1180px){
    .brand b{display:none}
    .chips{display:none}
  }
  @media (max-width:1023px){
    .layout{grid-template-columns:minmax(0,1fr)}
    .panel{position:fixed;top:0;right:0;bottom:0;width:420px;max-width:92vw;z-index:60;
       height:auto;border-radius:0;transform:translateX(105%);
       transition:transform .26s cubic-bezier(.4,0,.2,1)}
    .panel.show{transform:none}
    .fab{display:grid}
  }
  @media (max-width:639px){
    .appbar{padding:0 12px;gap:8px}
    .layout{padding:12px 12px 132px}
    .toolbar{padding:8px 12px}
    .toolbar .ghost{display:none}
    .pgrid{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:11px;padding:12px}
    .tabbar.is-on{display:flex}
    .pgrid{padding-bottom:12px}
    .fab{bottom:132px}
    /* 窄屏下对比栏与底部 Tab 都在底部：让对比栏浮在 Tab 之上，避免互相遮挡 */
    .tray{padding:10px 12px;bottom:56px}
    .tray.show{box-shadow:0 -6px 24px rgba(15,20,45,.16)}
    body{padding-bottom:56px}
    .tray .lab{display:none}
    .sheet .top{flex-direction:column}
    .sheet .big{width:100%;height:210px}
    .modal{padding:12px}
    .drawer{width:100%;max-width:100%}
  }
</style>
</head>
<body>
<header class="appbar">
  <div class="brand"><span class="logo">拾</span><b>多模态推荐助手</b></div>
  <form class="search" id="searchForm" role="search">
    <label class="sr" for="q">搜索或提问</label>
    <input id="q" type="text" autocomplete="off"
           placeholder="说一句需求，或直接提问…">
    <button type="submit" id="sendbtn">搜索</button>
  </form>
  <div class="chips" id="chips"></div>
  <div class="right">
    <button class="userbtn" id="userBtn" aria-label="当前用户" aria-haspopup="menu" aria-expanded="false">
      <span class="avatar" id="uava">林</span><span id="uname">林先生</span><span style="color:#9aa0bd">▾</span>
    </button>
    <button class="iconbtn" id="gearBtn" aria-label="运行状态与设置" title="运行状态与设置">⚙</button>
    <div class="usermenu" id="userMenu" role="menu" aria-label="切换演示账号"></div>
  </div>
</header>

<div class="toolbar" id="toolbar" role="toolbar" aria-label="操作">
  <button data-act="voice">🎤 语音</button>
  <button data-act="vision">👁 画面</button>
  <span class="sep"></span>
  <button data-act="undo">↩ 撤销</button>
  <button data-act="reset">⟳ 重来</button>
  <button data-act="compare">▦ 对比<span class="pill" id="cmpPill">0</span></button>
  <span class="sep"></span>
  <button data-act="llm" id="llmbtn">LLM 解释：关</button>
  <button class="ghost" id="statusBtn">运行状态 →</button>
</div>

<main class="layout">
  <section class="shelf">
    <div class="resultbar">
      <div class="rbtop">
        <h2 id="shelfTitle">猜你喜欢</h2>
        <div class="fchips" id="fchips"></div>
        <span class="cnt" id="shelfCnt"></span>
      </div>
      <div class="rbbot">
        <label class="sr" for="sortSel">排序方式</label>
        <select id="sortSel">
          <option value="match">按匹配度</option>
          <option value="priceAsc">价格从低到高</option>
          <option value="priceDesc">价格从高到低</option>
        </select>
        <label class="sr" for="pageSize">显示数量</label>
        <select id="pageSize" aria-label="显示数量">
          <option value="8">显示 8 件</option>
          <option value="16">显示 16 件</option>
          <option value="24">显示 24 件</option>
          <option value="40">显示 40 件</option>
        </select>
        <div class="vswitch" id="viewSwitch" role="group" aria-label="视图切换">
          <button data-view="grid" class="on" aria-label="网格视图">▦</button>
          <button data-view="list" aria-label="列表视图">☰</button>
        </div>
        <span class="sp"></span>
      </div>
    </div>
    <div class="pgrid" id="pgrid"></div>
    <button class="loadmore" id="loadMore" hidden>加载更多商品</button>
    <div class="empty" id="empty" style="display:none"></div>
  </section>

  <aside class="panel" id="panel" aria-label="推荐助手">
    <div class="phead">
      <span class="dot" id="liveDot"></span><b>推荐助手</b>
      <span class="live" id="liveTxt">实时</span>
      <button class="clr" id="clearChat">清空</button>
    </div>
    <div class="stream" id="stream" aria-live="polite"></div>
    <button class="jump" id="toBottom">↓ 回到最新</button>
    <div class="pfoot">
      <label class="sr" for="q2">继续对话</label>
      <input id="q2" type="text" autocomplete="off" placeholder="继续说，例如：为什么推荐第一个">
      <button id="sendbtn2">发送</button>
    </div>
  </aside>
</main>

<div class="tray" id="tray" role="region" aria-label="商品对比">
  <div class="in">
    <span class="lab">商品对比</span>
    <div class="items" id="trayItems"></div>
    <div class="acts">
      <button id="trayClear">清空</button>
      <button class="go" id="trayGo" disabled>开始对比</button>
    </div>
  </div>
</div>

<nav class="tabbar" id="tabbar" role="tablist">
  <button data-tab="shelf" class="on"><b>▦</b>货架</button>
  <button data-tab="chat"><b>💬</b>助手</button>
  <button data-tab="status"><b>⚙</b>状态</button>
</nav>

<button class="fab" id="fab" aria-label="打开助手">💬</button>

<div class="mask" id="mask"></div>

<aside class="drawer" id="statusDrawer" aria-label="运行状态">
  <div class="dh"><b>运行状态与设置</b><button id="statusClose" aria-label="关闭">✕</button></div>
  <div class="db" id="statusBody"></div>
</aside>

<div class="modal" id="modal" role="dialog" aria-modal="true" aria-label="商品详情">
  <div class="sheet" id="sheet"></div>
</div>

<div class="modal" id="cmpModal" role="dialog" aria-modal="true" aria-label="商品对比">
  <div class="sheet" id="cmpSheet"></div>
</div>

<div class="sr" id="toast" role="status" aria-live="polite"></div>
<script>
const $ = id => document.getElementById(id);
const grid = $('pgrid'), stream = $('stream');
let busy = false, needReal = false, currentItems = [], picked = new Map(), ratings = new Map();
let activeFilters = [], sortMode = 'match', viewMode = 'grid', userSaid = [];
let pageSize = 8, lastQuery = null;   // null = 首页货架；字符串 = 最近一次文字需求
let liveTimer = null, liveItem = null, liveEventId = 0, liveBusy = false;
let currentLimit = 8, loadingMore = false, hasMore = true;
let focusItemId = null;   // 手势反馈默认目标：最近打开的商品，否则货架 Top1

/* ============ 术语翻译（表 3-3「说人话」） ============ */
const INTENT_ZH = {discover:'随便看看', similar:'找相似', filter:'条件筛选',
                   compare:'商品对比', feedback:'反馈', explain:'求解释', browse:'浏览', chitchat:'对话',
                   chat:'问答'};
const STATUS_ZH = {ok:'完成', degraded:'降级返回', clarification:'需要补充信息',
                   conflict:'条件冲突', empty:'没有结果', no_evidence:'无可核验依据',
                   error:'出错了', warn:'未识别'};
const STATUS_IC = {ok:'✓', degraded:'⚑', clarification:'?', conflict:'!',
                   empty:'∅', no_evidence:'⊘', error:'✕', warn:'!'};
const SRC_ZH = {'historical_mode':'历史成交价', 'category_median':'类目参考价',
                'catalog_default':'默认参考价', 'missing':'无价格记录'};
const MODE_ZH = {offline_snapshot:'内置离线快照', local_artifacts:'推荐系统发布制品',
                 http:'推荐系统实时接口', offline_stub:'离线场景桩', real:'真实模型'};

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const zh = (map, k, d) => map[k] || d || k || '';
const money = v => v == null ? '—' : '£' + v;
/* 匹配度：把模型原始分映射成用户可读刻度（表 3-3） */
const matchPct = s => s == null ? null : Math.max(1, Math.min(99, Math.round(Number(s) * 100)));
const intentZh = r => zh(INTENT_ZH, ((r.intent_result||{}).intent), '—');
const statusZh = r => zh(STATUS_ZH, r.status || 'ok');

function media(item, cls, eager){
  const emoji = item.emoji || '🛍️';
  const load = eager ? ' loading="eager" fetchpriority="high"' : ' loading="lazy"';
  return '<div class="' + cls + '"><span class="ph">' + emoji + '</span>' +
    (item.img_url ? '<img' + load + ' src="' + esc(item.img_url) + '" alt="" onerror="this.remove()">' : '') +
    '</div>';
}
function badge(status, extra){
  const s = status || 'ok';
  return '<span class="st ' + s + '"><i></i>' + (STATUS_IC[s] || '·') + ' ' + esc(statusZh({status:s})) +
         (extra ? ' ' + esc(extra) : '') + '</span>';
}
function toast(msg){
  const t = $('toast'); t.textContent = msg;
  clearTimeout(window.__tt); window.__tt = setTimeout(() => { t.textContent = ''; }, 2600);
}

/* ============ 筛选口径 → 可移除 chip（P1-5） ============ */
function filtersFrom(resp){
  const s = ((resp || {}).intent_result || {}).slots || {};
  const out = [];
  if (s.scene && s.scene.value)      out.push({key:'scene',    label:'场景 · ' + s.scene.value,    frag:s.scene.value});
  if (s.category && s.category.value) out.push({key:'category', label:'品类 · ' + s.category.value, frag:s.category.value});
  if (s.colors && s.colors.value && s.colors.value.length)
    out.push({key:'colors', label:'颜色 · ' + s.colors.value.join('/'), frag:s.colors.value.join('/')});
  if (s.budget_max && s.budget_max.value != null)
    out.push({key:'budget', label:'预算 · ≤ £' + s.budget_max.value, frag:s.budget_max.value + '以内'});
  if (s.budget_min && s.budget_min.value != null)
    out.push({key:'budget_min', label:'预算 · ≥ £' + s.budget_min.value, frag:s.budget_min.value + '以上'});
  return out;
}
function renderFilters(){
  $('fchips').innerHTML = activeFilters.map(f =>
    '<span class="fchip">' + esc(f.label) +
    '<button data-drop="' + esc(f.key) + '" aria-label="移除 ' + esc(f.label) + '">✕</button></span>').join('');
}
function dropFilter(key){
  if (busy){ toast('正在处理，请稍后再移除条件'); return; }
  const keep = activeFilters.filter(f => f.key !== key);
  activeFilters = keep; renderFilters();
  if (!keep.length){ toast('已移除全部条件，重新为你推荐'); ask('推荐一些热门商品', {silent:true}); return; }
  const q = '只看' + keep.map(f => f.frag).join('，');
  toast('已按剩余条件重新筛选');
  ask(q, {silent:true});
}

/* ============ 商品货架 ============ */
function sorted(items){
  const a = items.slice();
  if (sortMode === 'priceAsc')  a.sort((x,y) => (x.price ?? 1e9) - (y.price ?? 1e9));
  if (sortMode === 'priceDesc') a.sort((x,y) => (y.price ?? -1) - (x.price ?? -1));
  return a;   // match：保持后端返回顺序
}
function renderShelf(items, title, subtitle, resp, append){
  if (append){
    const seen = new Set(currentItems.map(x => String(x.item_id)));
    currentItems = currentItems.concat((items || []).filter(x => !seen.has(String(x.item_id))));
  } else {
    currentItems = items || [];
    currentLimit = currentItems.length;
    hasMore = currentItems.length > 0;
    if (!focusItemId || !currentItems.some(x => String(x.item_id) === focusItemId)){
      focusItemId = currentItems.length ? String(currentItems[0].item_id) : null;
    }
  }
  if (resp) activeFilters = filtersFrom(resp);
  renderFilters();
  $('shelfTitle').textContent = title || '推荐结果';
  $('shelfCnt').textContent = currentItems.length ? ('共 ' + currentItems.length + ' 件') : '';
  grid.className = 'pgrid' + (viewMode === 'list' ? ' list' : '');
  const lm = $('loadMore');
  if (lm) lm.hidden = !hasMore || !currentItems.length;
  if (!currentItems.length){
    grid.innerHTML = '';
    const e = $('empty'); e.style.display = 'block';
    e.innerHTML = '没有符合条件的商品。<div class="examples">' +
      ['换个大一点的预算试试','去掉颜色限制','看看同类热门'].map(t => '<button data-ex="'+esc(t)+'">'+esc(t)+'</button>').join('') +
      '</div>';
    e.querySelectorAll('[data-ex]').forEach(b => b.onclick = () => ask(b.dataset.ex));
    return;
  }
  $('empty').style.display = 'none';
  grid.innerHTML = sorted(currentItems).map((it, i) => {
    const rank = it.rank || (i + 1);
    const pct = matchPct(it.score);
    const hits = ((it.evidence || {}).matched_constraints || []).length;
    const tags = [];
    if (it.evidence_count) tags.push('<span class="t ev">' + it.evidence_count + ' 条依据</span>');
    if (hits) tags.push('<span class="t hit">命中 ' + hits + ' 项条件</span>');
    else if (it.category) tags.push('<span class="t">' + esc(it.category) + '</span>');
    const extra = tags.length > 2 ? tags.slice(0,2).join('') +
      '<span class="t more">+' + (tags.length - 2) + '</span>' : tags.join('');
    const isPicked = picked.has(it.item_id);
    return '<article class="pcard' + (isPicked ? ' picked' : '') + '" data-item="' + esc(it.item_id) + '">' +
      '<div class="rank' + (rank > 3 ? ' plain' : '') + '">TOP ' + rank + '</div>' +
      media(it, 'pimg', rank <= 2).replace('class="pimg"', 'class="pimg" data-open') +
      '<div class="pacts">' +
        '<button data-like="up" class="' + (ratings.get(it.item_id) === 'up' ? 'on' : '') + '" title="喜欢" aria-label="喜欢">👍</button>' +
        '<button data-like="down" class="' + (ratings.get(it.item_id) === 'down' ? 'on' : '') + '" title="不喜欢" aria-label="不喜欢">👎</button>' +
      '</div>' +
      '<div class="pbody">' +
        '<h3 data-open>' + esc(it.title) + '</h3>' +
        '<div class="prow"><span class="pprice">' + money(it.price) + '</span>' +
          (pct == null ? '' : '<span class="pmatch" title="模型原始分 ' + Number(it.score).toFixed(4) + '">' +
            pct + '% 匹配</span>') +
        '</div>' +
        '<div class="ptags">' + extra + '</div>' +
        '<button class="cmpbtn" data-cmp>' + (isPicked ? '✓ 已加入对比' : '加入对比') + '</button>' +
      '</div></article>';
  }).join('');
}

async function loadMore(){
  if (loadingMore || !hasMore || !currentItems.length) return;
  loadingMore = true;
  const btn = $('loadMore');
  btn.hidden = false; btn.disabled = true; btn.textContent = '正在加载更多商品…';
  const target = Math.min(120, currentLimit + 12);
  const text = lastQuery || '推荐一些热门商品';
  try{
    const d = await post('/api/more', {text, top_k: target});
    if (d.ok){
      const before = currentItems.length;
      renderShelf(d.items || [], $('shelfTitle').textContent, '', null, true);
      currentLimit = Math.max(target, currentItems.length);
      hasMore = currentItems.length > before && (d.items || []).length >= target && currentLimit < 120;
    } else {
      hasMore = false;
    }
  }catch(e){ hasMore = false; }
  btn.disabled = false; btn.textContent = '加载更多商品'; btn.hidden = !hasMore;
  loadingMore = false;
}

/* ============ 助手对话 ============ */
function addUser(text){
  userSaid.push(text);
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg user"><div class="bub">' + esc(text) + '</div></div>');
  bottom();
}
function addBot(html){
  stream.insertAdjacentHTML('beforeend', '<div class="msg"><div class="bub bot">' + html + '</div></div>');
  bottom();
}
function addSkeleton(){
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg" id="sk"><div class="bub bot" style="width:100%">' +
    '<div class="skb"><span class="skdot"></span><span style="color:#8b91a6">正在理解你的需求…</span></div>' +
    '</div></div>');
  bottom();
}
function addAction(icon, title, detail){
  const id = 'act-' + Math.random().toString(36).slice(2);
  stream.insertAdjacentHTML('beforeend',
    '<div class="msg"><div class="act pending" id="' + id + '"><span aria-hidden="true">' + icon + '</span>' +
    '<div style="flex:1;min-width:0"><div class="t">' + esc(title) + '</div>' +
    '<div class="d">' + esc(detail) + '</div></div>' +
    '<span class="badge pending"><span class="spin"></span></span></div></div>');
  bottom(); return id;
}
function finishAction(id, state, label, detail){
  const el = $(id); if (!el) return;
  el.className = 'act ' + state;
  const b = el.querySelector('.badge'); b.className = 'badge ' + state; b.textContent = label;
  if (detail) el.querySelector('.d').innerHTML = detail;
  bottom();
}
function updateAction(id, detail){
  const el = $(id); if (!el) return;
  const d = el.querySelector('.d'); if (d) d.textContent = detail;
  bottom();
}
/* P1-8：失败时给至少一个可点击的下一步 */
function actionSuggestions(kind){
  const how = {voice:'请靠近麦克风、在安静环境说一句完整的话，说完停顿 1 秒。',
               vision:'确保画面有光、主体在取景框内，再点一次。'}[kind] || '';
  return '<div class="sugg">' +
    '<button data-retry="' + kind + '">重试一次</button>' +
    '<button data-type><span>改用打字</span></button>' +
    (how ? '<button data-help="' + esc(how) + '">怎么用？</button>' : '') + '</div>';
}
function renderBot(resp, updateShelf){
  const st = resp.status || 'ok';
  if (resp.event && resp.event.text) lastQuery = resp.event.text;
  let h = '<div class="rh">' + badge(st) +
          '<span class="k">意图 <b>' + esc(intentZh(resp)) + '</b></span></div>';
  if (resp.error && (!resp.clarification || resp.error.message !== resp.clarification))
    h += '<div class="errline">' + esc(resp.error.message) + '</div>';
  if (resp.clarification) h += '<div>' + esc(resp.clarification) + '</div>';
  if (resp.answer) h += '<div style="margin-top:6px;line-height:1.65">' + esc(resp.answer) + '</div>';
  const ws = resp.web_search || null;
  if (ws && ws.sources && ws.sources.length){
    h += '<div style="margin-top:8px;padding:8px 10px;border:1px solid #e5e8f0;border-radius:8px;background:#fafbff;font-size:12.5px">' +
         '🌐 已联网检索 <b>' + ws.count + '</b> 条来源' +
         (ws.latency_ms != null ? ' · ' + ws.latency_ms + 'ms' : '') +
         '<div style="margin-top:5px;display:grid;gap:3px">' +
         ws.sources.map(s => '<div>[' + s.index + '] <a href="' + esc(s.url) + '" target="_blank" rel="noopener noreferrer">' +
           esc(s.title || s.url) + '</a>' + (s.site_name ? ' · ' + esc(s.site_name) : '') + '</div>').join('') +
         '</div></div>';
  } else if (ws && ws.error){
    h += '<div class="errline" style="margin-top:6px">联网搜索失败：' + esc(ws.error) + '</div>';
  }

  const slots = ((resp.intent_result || {}).slots) || {};
  const sv = Object.entries(slots).filter(([k, v]) => v && v.value !== undefined &&
    k !== 'budget_raw' && k !== 'category_alt');
  if (sv.length) h += '<div class="slots">' + sv.map(([k, v]) =>
    '<button class="slotchip" data-slot="' + esc(String(v.value)) + '">' + esc(k) + ' · ' +
    esc(String(v.value)) + '</button>').join('') + '</div>';

  const n = (resp.recommendations || []).length;
  if (n) h += '<div style="margin-top:6px">为你找到 <b>' + n + '</b> 件商品，已更新左侧货架。</div>';
  if (resp.browse) h += '<div style="margin-top:4px">已记录浏览：<b>' + esc(resp.browse.title || '') +
    '</b>（弱反馈，仅轻微上浮）</div>';
  if (resp.feedback){
    const fb = resp.feedback;
    const verb = fb.polarity === 'positive' ? '点赞' : fb.polarity === 'negative' ? '倒赞' : '';
    h += '<div style="margin-top:4px">' +
      (fb.polarity === 'none' || fb.cleared ? '已取消反馈' : '已记录 <b>' + esc(verb) + '</b>') +
      ' → ' + esc(fb.target_title || '') + '</div>';
    if (fb.polarity !== 'none' && fb.item_id){
      const p = fb.polarity === 'positive' ? 'up' : 'down';
      h += '<button class="cmpbtn" style="margin-top:5px" data-undo-feedback="' + esc(fb.item_id) +
        '" data-feedback-polarity="' + p + '">撤销' + (p === 'up' ? '点赞' : '倒赞') + '</button>';
    }
  }
  if (resp.before_after) h += '<div style="margin-top:4px;font-size:12.5px;color:#8b91a6">排序变化：<b style="color:#1b1f30">' +
    (resp.before_after.changed ? '有' : '无') + '</b></div>';

  const pres = resp.presentation || {};
  if (pres.audio_url) h += '<audio controls preload="none" src="' + esc(pres.audio_url) + '"></audio>';
  else if (pres.spoken_text) h += '<button class="ttsbtn" data-tts="' + encodeURIComponent(pres.spoken_text) + '">🔊 语音播报</button>';

  /* P1-7：运行细节默认折叠（寒暄回答不需要召回/排序依据） */
  if (!resp.answer) {
  const t = resp.trace || {}, lat = t.latency_ms || {}, modes = t.modes || {};
  const rec = t.recall || {}, rk = t.ranking || {};
  const detail = [
    '召回来源：' + ((rec.sources || []).map(s => s.source + ' ' + s.count).join(' · ') || '—'),
    '候选：合并 ' + (rec.merged_unique ?? '—') + ' → 保留 ' + (rec.kept ?? '—') +
      ' → 硬约束过滤后 ' + (rk.after_hard_filter ?? '—') + '（淘汰 ' + (rk.hard_dropped ?? '—') + '）',
    '模式：推荐 ' + zh(MODE_ZH, modes.recommender) + ' · 多模态 ' + zh(MODE_ZH, modes.multimodal),
    '耗时：意图 ' + (lat.intent_ms ?? '—') + 'ms · 召回 ' + (lat.recall_ms ?? '—') +
      'ms · 排序 ' + (lat.rank_ms ?? '—') + 'ms · 合计 ' + (lat.total_ms ?? '—') + 'ms',
  ].join('<br>');
  h += '<div class="fold"><button data-fold>▸ 展开依据</button>' +
       '<div class="body">' + detail + '</div></div>';
  }
  addBot(h);
  if (updateShelf && n) {
    renderShelf(resp.recommendations, shelfTitle(resp), shelfSub(resp), resp);
  }
}
function shelfTitle(resp){
  const i = (resp.intent_result || {}).intent;
  if (i === 'similar') return '相似商品';
  if (i === 'filter') return '筛选结果';
  if (i === 'discover') return '为你推荐';
  if (i === 'feedback' || i === 'browse') return '为你推荐';
  return '推荐结果';
}
function shelfSub(resp){
  const s = ((resp.intent_result || {}).slots) || {};
  const p = [];
  if (s.scene && s.scene.value) p.push(s.scene.value);
  if (s.category && s.category.value) p.push(s.category.value);
  if (s.budget_max && s.budget_max.value != null) p.push('≤£' + s.budget_max.value);
  return p.join(' · ');
}
function bottom(){ stream.scrollTop = stream.scrollHeight; }

/* ============ 请求 ============ */
async function post(path, body){
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify(body || {})});
  return await r.json();
}
function setBusy(b){
  busy = b;
  document.querySelectorAll('button').forEach(x => {
    if (['voice','gesture','vision'].includes(x.dataset.act)) x.disabled = b || needReal;
    else if (x.dataset.act || x.dataset.view || x.dataset.like || x.dataset.cmp || x.dataset.retry || x.dataset.drop ||
             ['sendbtn','sendbtn2','trayGo','trayClear','userBtn','gearBtn','clearChat'].includes(x.id)) x.disabled = b;
  });
  $('liveDot').classList.toggle('busy', b);
  $('liveTxt').textContent = b ? '处理中…' : '实时';
}
async function ask(text, opt){
  opt = opt || {};
  if (!text || busy) return;
  lastQuery = text;
  if (!opt.silent) addUser(text);
  setBusy(true); addSkeleton();
  try{
    const d = await post('/api/chat', {text, top_k: pageSize});
    $('sk')?.remove();
    if (d.ok) renderBot(d.response, true);
    else addBot('<div class="errline">' + esc(d.error) + '</div>' +
                '<div class="sugg"><button data-type><span>改用打字</span></button></div>');
  }catch(e){
    $('sk')?.remove();
    addBot('<div class="errline">网络请求失败：' + esc(e.message) + '</div>' +
           '<div class="sugg"><button data-type><span>重试</span></button></div>');
  }
  setBusy(false); $('q').focus();
}

/* ============ 点赞 / 倒赞（常驻，触屏可达） ============ */
async function rate(itemId, polarity, forceCancel){
  if (busy) return;
  focusItemId = String(itemId);
  const cancel = !!forceCancel || ratings.get(String(itemId)) === polarity;
  const id = addAction(polarity === 'up' ? '👍' : '👎',
                       cancel ? '取消反馈' : (polarity === 'up' ? '点赞' : '倒赞'),
                       cancel ? '正在撤销该商品的反馈并重新排序…' : '正在写入会话偏好并重新排序…');
  setBusy(true);
  try{
    const d = await post('/api/feedback', {item_id: itemId, polarity: cancel ? 'none' : polarity, top_k: pageSize});
    const r = d.response || {};
    const okStatus = r.status === 'ok' || r.status === 'degraded';
    if (d.ok && okStatus){
      const fb = r.feedback || {};
      finishAction(id, 'ok', cancel ? '已取消' : '已记录', esc(cancel ? '反馈' : fb.polarity === 'positive' ? '点赞' : '倒赞') +
        '：<b>' + esc(fb.target_title || itemId) + '</b>');
      if (cancel){
        ratings.delete(String(itemId));
        document.querySelectorAll('[data-undo-feedback="' + itemId + '"]').forEach(b => b.remove());
      } else {
        ratings.set(String(itemId), polarity);
      }
      renderBot(r, true);
      const card = document.querySelector('.pcard[data-item="' + itemId + '"]');
      if (card) card.querySelectorAll('[data-like]').forEach(b => b.classList.toggle('on',
        !cancel && b.dataset.like === polarity));
    } else {
      const msg = (r.error && r.error.message) || r.clarification || d.error || '反馈未生效';
      finishAction(id, 'warn', '未生效', esc(msg) + actionSuggestions('feedback'));
    }
  }catch(e){ finishAction(id, 'err', '失败', esc(e.message) + actionSuggestions('feedback')); }
  setBusy(false);
}

/* ============ 商品对比（P1-6） ============ */
function renderTray(){
  const items = Array.from(picked.values());
  $('trayItems').innerHTML = items.map(it =>
    '<span class="titem"><span class="th">' + (it.emoji || '🛍️') +
    (it.img_url ? '<img src="' + esc(it.img_url) + '" alt="" onerror="this.remove()">' : '') + '</span>' +
    '<span class="nm">' + esc(it.title) + '</span><span class="pr">' + money(it.price) + '</span>' +
    '<button data-unpick="' + esc(it.item_id) + '" aria-label="移除">✕</button></span>').join('');
  $('tray').classList.toggle('show', items.length > 0);
  $('cmpPill').textContent = items.length;
  $('cmpPill').classList.toggle('show', items.length > 0);
  $('trayGo').disabled = items.length < 2;
  const top = $('toolbar').querySelector('[data-act="compare"]');
  if (top) top.classList.toggle('on', items.length > 0);
}
function syncPickUI(){
  document.querySelectorAll('.pcard').forEach(card => {
    const on = picked.has(card.dataset.item);
    card.classList.toggle('picked', on);
    const b = card.querySelector('[data-cmp]');
    if (b) b.textContent = on ? '✓ 已加入对比' : '加入对比';
  });
}
function toggleCompare(itemId, fallback){
  const it = currentItems.find(x => String(x.item_id) === String(itemId)) || fallback;
  if (!it) return false;
  if (picked.has(itemId)) picked.delete(itemId);
  else {
    if (picked.size >= 4){ toast('最多同时对比 4 件商品'); return false; }
    picked.set(itemId, {item_id: itemId, title: it.title, price: it.price,
                        emoji: it.emoji, img_url: it.img_url});
  }
  renderTray();
  syncPickUI();
  return true;
}
async function openCompare(){
  if (picked.size < 2){ toast('至少选择 2 件商品才能对比'); return; }
  $('cmpModal').classList.add('show');
  $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal" aria-label="关闭">✕</button>' +
    '<div class="sec"><span class="spin"></span> 正在读取商品字段…</div>';
  try{
    const d = await post('/api/products', {item_ids: Array.from(picked.keys())});
    if (!d.ok){ $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal">✕</button>' +
      '<div class="sec errline">' + esc(d.error || '读取失败') + '</div>'; return; }
    const rows = [
      ['商品', p => '<img class="cmpimg" src="' + esc(p.img_url) + '" alt="" onerror="this.remove()">'],
      ['标题', p => esc(p.title)],
      ['参考价', p => '<b style="color:#dc2626">' + money(p.price) + '</b>'],
      ['类目', p => esc(p.emoji + ' ' + p.category)],
      ['价格来源', p => esc(zh(SRC_ZH, p.price_source, p.price_source || '—'))],
      ['历史成交', p => (p.transactions ?? '—') + ' 笔'],
      ['商品编号', p => esc(p.item_id)],
    ];
    const items = d.products || [];
    const body = rows.map(([label, fn]) => {
      const cells = items.map(p => fn(p));
      const diff = new Set(cells.map(c => String(c).replace(/<[^>]+>/g, ''))).size > 1;
      return '<tr><th>' + label + '</th>' + cells.map(c =>
        '<td class="' + (diff ? 'diff' : '') + '">' + c + '</td>').join('') + '</tr>';
    }).join('');
    $('cmpSheet').innerHTML =
      '<button class="x" data-close="#cmpModal" aria-label="关闭">✕</button>' +
      '<div class="sec"><h3 style="margin:0 0 10px;font-size:15px;color:#1b1f30">商品对比（' +
        items.length + ' 件）</h3>' +
      '<div style="overflow:auto"><table class="cmptable"><tr><th></th>' +
        items.map(p => '<th>' + esc(p.title) + '</th>').join('') + '</tr>' + body + '</table></div>' +
      '<div style="margin-top:10px;font-size:12.5px;color:#8b91a6">高亮单元格表示该项在两件商品间存在差异。</div>' +
      '</div>';
  }catch(e){
    $('cmpSheet').innerHTML = '<button class="x" data-close="#cmpModal">✕</button>' +
      '<div class="sec errline">' + esc(e.message) + '</div>';
  }
}

/* ============ 商品详情 ============ */
async function stopLiveGesture(){
  // 停止前先处理一次最后的状态，避免「刚比完手势就点停止」丢掉最后一次事件
  if (liveItem !== null){
    try { await pollLiveGesture(true); } catch(e){}
  }
  if (liveTimer !== null){ clearInterval(liveTimer); liveTimer = null; }
  if (liveItem !== null){
    liveItem = null;
    try { await post('/api/camera/stop'); } catch(e){}
  }
}

function setModalRating(dir){
  const up = $('sheet').querySelector('[data-rate="up"]');
  const down = $('sheet').querySelector('[data-rate="down"]');
  if (up){
    up.style.background = dir === 'up' ? '#dcfce7' : '#eef0fe';
    up.style.color = dir === 'up' ? '#166534' : '#3730a3';
    up.textContent = dir === 'up' ? '👍 已喜欢' : '👍 喜欢';
  }
  if (down){
    down.style.background = dir === 'down' ? '#fee2e2' : '#f1f2f7';
    down.style.color = dir === 'down' ? '#b91c1c' : '#5c6379';
    down.textContent = dir === 'down' ? '👎 已不喜欢' : '👎 不喜欢';
  }
}

async function handleLiveEvent(g){
  const stat = $('liveStat');
  if (g === 'thumbs_up' || g === 'thumbs_down'){
    const target = liveItem;
    if (!target) return;
    const dir = g === 'thumbs_up' ? 'up' : 'down';
    const cancel = ratings.get(String(target)) === dir;
    const r = await post('/api/feedback', {item_id: target, polarity: cancel ? 'none' : dir, top_k: pageSize});
    if (r.ok){
      if (cancel){
        ratings.delete(String(target));
        document.querySelectorAll('[data-undo-feedback="' + target + '"]').forEach(b => b.remove());
      } else {
        ratings.set(String(target), dir);
      }
      setModalRating(cancel ? null : dir);
      if (r.response) renderBot(r.response, true);
      const card = document.querySelector('.pcard[data-item="' + target + '"]');
      if (card) card.querySelectorAll('[data-like]').forEach(b => b.classList.toggle('on',
        !cancel && b.dataset.like === dir));
      if (stat) stat.textContent = cancel ? '已取消反馈，商品已恢复' : '已识别 ' + g + '，已更新当前商品';
    } else if (stat) stat.textContent = '识别到 ' + g + '，但反馈失败：' + (r.error || '');
  } else if (g){
    if (stat) stat.textContent = '已识别 ' + g + '，当前仅支持 👍/👎，已忽略';
  }
}

async function pollLiveGesture(force){
  if ((!force && liveTimer === null) || liveItem === null) return;
  try{
    const d = await (await fetch('/api/camera/state')).json();
    const s = d.state || {};
    const stat = $('liveStat');
    if (stat){
      const g = s.current_gesture;
      const conf = Math.round((s.current_confidence || 0) * 100);
      stat.textContent = (s.running === false ? '摄像头已停止' :
        ((g ? '当前：' + g + ' ' + conf + '%' : '未检测到手') + ' · ' + Number(s.fps || 0).toFixed(1) + ' fps'));
    }
    const events = Array.isArray(s.events) ? s.events : [];
    let pending = events.filter(e => Number(e.id) > liveEventId);
    if (!pending.length && s.event_id && s.event_id !== liveEventId){
      pending = [{id: s.event_id, gesture: s.last_event_gesture}];
    }
    if (!pending.length || liveBusy) return;
    liveBusy = true;
    try{
      for (const e of pending){
        liveEventId = Math.max(liveEventId, Number(e.id) || 0);
        await handleLiveEvent(e.gesture);
      }
    } finally { liveBusy = false; }
  }catch(e){ /* 轮询失败时保留上一状态 */ }
}

function renderLivePanel(itemId){
  const box = $('modalGesture'); if (!box) return;
  box.innerHTML =
    '<div class="livehead"><span>📷 手势直播</span><button data-stop-live="1">停止</button></div>' +
    '<img class="livecam" id="liveCam" src="/camera/stream?t=' + Date.now() + '" alt="手势直播画面">' +
    '<div class="livestat" id="liveStat">正在识别手势…</div>' +
    '<div class="livehint">👍 点赞 · 👎 倒赞 · 保持约 1 秒；换手势不需要把手移出画面</div>' +
    '<button class="cmpbtn" data-single-gesture="1" style="margin-top:6px">改用单次识别</button>';
  const stop = box.querySelector('[data-stop-live]');
  if (stop) stop.onclick = async () => { await stopLiveGesture(); box.innerHTML = '<div class="mstatus">已停止手势直播</div>'; };
  const single = box.querySelector('[data-single-gesture]');
  if (single) single.onclick = async () => { await stopLiveGesture(); modalGesture(itemId); };
}

async function startLiveGesture(itemId){
  const box = $('modalGesture'); if (!box) return;
  liveItem = String(itemId);
  if (liveTimer !== null){
    // 摄像头已经在直播：只切换目标商品，不重新打开设备
    try{
      const d = await (await fetch('/api/camera/state')).json();
      liveEventId = Number((d.state || {}).event_id || liveEventId);
    }catch(e){}
    renderLivePanel(itemId);
    return;
  }
  box.innerHTML = '<div class="mstatus">正在打开摄像头…</div>';
  try{
    const d = await post('/api/camera/start');
    if (!d.ok){
      liveItem = null;
      box.innerHTML = '<div class="mstatus err">' + esc(d.error || '摄像头打开失败') +
        '</div><button class="cmpbtn" data-single-gesture="1">改用单次识别</button>';
      const b = box.querySelector('[data-single-gesture]');
      if (b) b.onclick = () => modalGesture(itemId);
      return;
    }
    liveEventId = Number((d.state || {}).event_id || 0);
    renderLivePanel(itemId);
    liveTimer = setInterval(pollLiveGesture, 300);
  }catch(e){
    liveItem = null;
    box.innerHTML = '<div class="mstatus err">摄像头启动失败：' + esc(e.message) + '</div>';
  }
}

async function modalGesture(itemId){
  const box = $('modalGesture'); if (!box || box.dataset.busy === '1') return;
  box.dataset.busy = '1';
  const status = (text, cls) => {
    box.innerHTML = '<div class="mstatus' + (cls ? ' ' + cls : '') + '">' + text + '</div>';
  };
  for (let left = 3; left > 0; left--){
    status('✋ 请把手举到脸旁边，保持手势… ' + left);
    await new Promise(r => setTimeout(r, 1000));
  }
  status('正在连拍并识别…');
  try{
    const d = await post('/api/gesture', {item_id: itemId, top_k: pageSize});
    const shot = d.preview_url ? '<a class="shotwrap" href="' + esc(d.preview_url) +
      '" target="_blank" rel="noopener"><img class="shot" src="' + esc(d.preview_url) +
      '" alt="手势抓拍画面"></a><div class="shotcap">抓拍画面 · 点击查看大图</div>' : '';
    if (d.ok && (d.gesture === 'thumbs_up' || d.gesture === 'thumbs_down')){
      const pol = (((d.response || {}).feedback) || {}).polarity ||
                  (d.gesture === 'thumbs_up' ? 'positive' : 'negative');
      const dir = pol === 'positive' ? 'up' : 'down';
      const cancel = ratings.get(String(itemId)) === dir;
      if (cancel){
        const c = await post('/api/feedback', {item_id: itemId, polarity: 'none', top_k: pageSize});
        if (c.ok){
          ratings.delete(String(itemId));
          document.querySelectorAll('[data-undo-feedback="' + itemId + '"]').forEach(b => b.remove());
          setModalRating(null);
          if (c.response) renderBot(c.response, true);
          box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) + '，已取消该商品反馈</div>' + shot;
        } else {
          box.innerHTML = '<div class="mstatus err">取消反馈失败：' + esc(c.error || '') + '</div>' + shot;
        }
        box.dataset.busy = '0';
        return;
      }
      ratings.set(String(itemId), dir);
      setModalRating(dir);
      const card = document.querySelector('.pcard[data-item="' + itemId + '"]');
      if (card) card.querySelectorAll('[data-like]').forEach(b => b.classList.toggle('on', b.dataset.like === dir));
      if (d.response) renderBot(d.response, true);
      box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) + '，已更新该商品评价</div>' + shot;
    } else if (d.ok){
      box.innerHTML = '<div class="mstatus ok">已识别 ' + esc(d.gesture) +
        (d.message ? ' · ' + esc(d.message) : '') + '</div>' + shot;
    } else {
      box.innerHTML = '<div class="mstatus err">' + esc(d.error || '手势未识别') + '</div>' + shot;
    }
  }catch(e){
    box.innerHTML = '<div class="mstatus err">网络失败：' + esc(e.message) + '</div>';
  }
  box.dataset.busy = '0';
}

async function openDetail(itemId){
  focusItemId = String(itemId);
  $('modal').classList.add('show');
  $('sheet').innerHTML = '<button class="x" data-close="#modal" aria-label="关闭">✕</button>' +
    '<div class="sec"><span class="spin"></span> 正在读取商品详情…</div>';
  fetch('/api/view', {method:'POST', headers:{'Content-Type':'application/json'},
                      body: JSON.stringify({item_id: itemId})}).catch(() => {});
  try{
    const d = await (await fetch('/api/product/' + encodeURIComponent(itemId))).json();
    if (!d.ok){
      $('sheet').innerHTML = '<button class="x" data-close="#modal">✕</button>' +
        '<div class="sec errline">' + esc(d.error) + '</div>'; return;
    }
    const p = d.product;
    const local = currentItems.find(x => String(x.item_id) === String(itemId));
    const reason = local && local.explanation
      ? '<div class="sec"><h3>推荐理由</h3><div class="reason">' + esc(local.explanation) + '</div></div>' : '';
    const sim = (d.similar || []).map(s =>
      '<div class="m" data-item="' + esc(s.item_id) + '">' + media(s, 'mi') +
      '<p>' + esc(s.title) + '</p><span>' + money(s.price) + '</span></div>').join('');
    $('sheet').innerHTML =
      '<button class="x" data-close="#modal" aria-label="关闭">✕</button>' +
      '<div class="top">' + media({img_url:p.img_url, emoji:p.emoji}, 'big', true) +
        '<div class="meta"><h2>' + esc(p.title) + '</h2>' +
        '<div class="en">' + esc(p.english_title || '') + '</div>' +
        '<dl class="kv">' +
          '<dt>参考价</dt><dd class="p">' + money(p.price) + ' <span style="font-size:12px;color:#8b91a6">' + esc(p.currency || '') + '</span></dd>' +
          '<dt>商品编号</dt><dd>' + esc(p.item_id) + '</dd>' +
          '<dt>类目</dt><dd>' + esc(p.emoji + ' ' + p.category) + '</dd>' +
          '<dt>价格来源</dt><dd>' + esc(zh(SRC_ZH, p.price_source, p.price_source || '—')) + '</dd>' +
          '<dt>历史成交</dt><dd>' + (p.transactions ?? '—') + ' 笔</dd>' +
          '<dt>我的评价</dt><dd>' +
            '<button data-rate="up" style="border:none;border-radius:8px;padding:6px 12px;background:#eef0fe;color:#3730a3;margin-right:6px">👍 喜欢</button>' +
            '<button data-rate="down" style="border:none;border-radius:8px;padding:6px 12px;background:#f1f2f7;color:#5c6379">👎 不喜欢</button>' +
            '<button data-cmp2="' + esc(p.item_id) + '" style="border:1px solid #e8eaf2;border-radius:8px;padding:6px 12px;background:#fff;color:#5c6379;margin-left:6px">' +
              (picked.has(p.item_id) ? '✓ 已加入对比' : '▦ 加入对比') + '</button>' +
          '</dd>' +
        '</dl><div id="modalGesture" class="mgesture"></div></div></div>' + reason +
      '<div class="sec"><h3>相似商品（点开继续看）</h3><div class="mini">' + sim + '</div></div>';
    $('sheet').querySelectorAll('[data-item]').forEach(el => {
      el.onclick = ev => { ev.stopPropagation(); openDetail(el.dataset.item); };
    });
    $('sheet').querySelectorAll('[data-rate]').forEach(el => {
      el.onclick = ev => { ev.stopPropagation(); closeAll(); rate(itemId, el.dataset.rate); };
    });
    setModalRating(ratings.get(String(itemId)) || null);
    const c2 = $('sheet').querySelector('[data-cmp2]');
    if (c2) c2.onclick = ev => {
      ev.stopPropagation();
      if (toggleCompare(itemId, p)){
        c2.textContent = picked.has(itemId) ? '✓ 已加入对比' : '▦ 加入对比';
        toast('已更新对比栏');
      }
    };
    await startLiveGesture(itemId);   // 进入商品详情自动开启摄像头直播
  }catch(e){
    $('sheet').innerHTML = '<button class="x" data-close="#modal">✕</button>' +
      '<div class="sec errline">' + esc(e.message) + '</div>';
  }
}
function closeAll(){
  stopLiveGesture();
  $('modal').classList.remove('show');
  $('cmpModal').classList.remove('show');
}

/* ============ 工具动作 ============ */
async function act(kind){
  if (busy) return;
  if (kind === 'compare'){ openCompare(); return; }
  if (needReal && ['voice','vision'].includes(kind)){
    addBot('<div class="slots">该功能需要真实设备，请以 <code>python webui.py --real</code> 启动。</div>');
    return;
  }
  setBusy(true);
  if (kind === 'llm'){
    try{
      const d = await post('/api/llm');
      $('llmbtn').textContent = 'LLM 解释：' + (d.enabled ? '开' : '关');
      addBot('<div class="slots">' + (d.enabled
        ? '已开启真实 LLM 生成解释（每条约 10~25 秒）。解释须连过事实 / 格式 / 流畅度三道门控，否则回退模板。'
        : '已关闭 LLM，解释回退为模板（毫秒级、逐句有据）。') + '</div>');
    }catch(e){ addBot('<div class="errline">' + esc(e.message) + '</div>'); }
    setBusy(false); return;
  }
  if (kind === 'undo' || kind === 'reset'){
    const id = addAction(kind === 'undo' ? '↩' : '⟳',
      kind === 'undo' ? '撤销上一轮' : '重新开始会话', '正在处理…');
    try{
      const d = await post('/api/' + kind);
      if (d.ok) finishAction(id, 'ok', '完成', kind === 'undo' ? '已回滚到上一轮状态' : '已开启全新会话');
      else finishAction(id, 'err', '失败', esc(d.error));
      if (d.ok && kind === 'reset'){
        picked.clear(); renderTray(); syncPickUI();
        ratings.clear();
        activeFilters = []; renderFilters();
        await loadHomeShelf(true);
      }
    }catch(e){ finishAction(id, 'err', '失败', esc(e.message)); }
    setBusy(false); return;
  }
  const meta = {voice:{i:'🎤', t:'语音输入', d:'正在录音 4 秒…'},
                vision:{i:'👁', t:'画面理解', d:'正在抓取画面并做视觉理解（约 20~40 秒）…'}}[kind];
  const id = addAction(meta.i, meta.t, meta.d);
  try{
    const payload = kind === 'voice' ? {seconds: 4, top_k: pageSize} : {top_k: pageSize};
    const d = await post('/api/' + kind, payload);
    if (kind === 'voice'){
      if (d.transcript){
        finishAction(id, d.ok ? 'ok' : 'warn', d.ok ? '已识别' : '未识别',
                     '识别结果：<b>' + esc(d.transcript) + '</b>' + (d.ok ? '' : actionSuggestions('voice')));
        if (d.ok) renderBot(d.response, true);
      } else {
        finishAction(id, 'err', '未识别到有效语音',
                     esc(d.error || '没听清内容') + actionSuggestions('voice'));
      }
    } else if (kind === 'vision'){
      if (d.description){
        finishAction(id, d.ok ? 'ok' : 'warn', d.ok ? '已完成' : '部分完成',
                     '画面：<b>' + esc(d.description) + '</b>' + (d.ok ? '' : actionSuggestions('vision')));
        if (d.ok) renderBot(d.response, true);
      } else finishAction(id, 'err', '失败', esc(d.error) + actionSuggestions('vision'));
    } else if (!d.ok){
      finishAction(id, 'err', '失败', esc(d.error) + actionSuggestions(kind));
    } else { finishAction(id, 'ok', '完成', ''); if (d.response) renderBot(d.response, true); }
  }catch(e){ finishAction(id, 'err', '失败', esc(e.message) + actionSuggestions(kind)); }
  setBusy(false);
}

/* ============ 运行状态抽屉 ============ */
async function openStatus(){
  $('statusDrawer').classList.add('show'); $('mask').classList.add('show');
  $('statusBody').innerHTML = '<span class="spin"></span> 读取中…';
  try{
    const info = await (await fetch('/api/info')).json();
    const st = await post('/api/state');
    const s = st.state || {};
    const m = info.modes || {};
    const row = (k, v) => '<div class="kvrow"><span class="k">' + k + '</span><span class="v">' + esc(v) + '</span></div>';
    $('statusBody').innerHTML =
      '<h3 style="margin:0 0 6px;font-size:13px;color:#8b91a6">运行模式</h3>' +
      row('用户', info.user_id) + row('会话', info.session_id) +
      row('推荐适配器', zh(MODE_ZH, m.recommender)) + row('多模态适配器', zh(MODE_ZH, m.multimodal)) +
      row('LLM 解释', info.llm ? '已开启' : '模板') +
      row('对话后端', (m.llm_provider === 'online' ? '在线模型' : m.llm_provider === 'local' ? '本地 Qwen' : '未启用')) +
      row('联网搜索', (info.llm_chat || {}).web_search ? '开 · 最多 ' + ((info.llm_chat || {}).web_search_count || 5) + ' 条' : '关') +
      row('语音播报', info.voice ? '开' : '关') +
      '<h3 style="margin:14px 0 6px;font-size:13px;color:#8b91a6">本次会话</h3>' +
      row('轮次', String(s.turns ?? 0)) +
      row('已点赞', JSON.stringify(s.likes || {})) +
      row('已倒赞', JSON.stringify(s.dislikes || {})) +
      row('已浏览', JSON.stringify(s.viewed_items || {})) +
      row('生效条件', Object.keys(s.slot_state || {}).filter(k => k !== 'budget_raw' && k !== 'category_alt').join('、') || '（无）') +
      row('焦点商品', s.focus_item_id || '（无）');
  }catch(e){ $('statusBody').innerHTML = '<div class="errline">' + esc(e.message) + '</div>'; }
}
function closeStatus(){ $('statusDrawer').classList.remove('show'); $('mask').classList.remove('show'); }

/* ============ 事件绑定 ============ */
function submitAsk(inputId){
  const el = $(inputId); const t = el.value.trim();
  if (!t) return;
  if (busy){ toast('正在处理中，请稍候'); return; }
  el.value = '';
  ask(t);
}
$('searchForm').addEventListener('submit', ev => { ev.preventDefault(); submitAsk('q'); });
$('sendbtn2').addEventListener('click', () => submitAsk('q2'));
$('q2').addEventListener('keydown', e => { if (e.key === 'Enter'){ e.preventDefault(); submitAsk('q2'); } });
$('toolbar').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-act]'); if (b) act(b.dataset.act);
});
$('statusBtn').addEventListener('click', openStatus);
$('gearBtn').addEventListener('click', openStatus);
$('statusClose').addEventListener('click', closeStatus);
$('mask').addEventListener('click', closeStatus);
function switchUser(userId){
  if (busy){ toast('正在处理中，请稍候'); return; }
  setBusy(true);
  post('/api/user', {user_id: userId}).then(d => {
    if (d.ok){ toast('已切换到 ' + d.account.name); location.reload(); }
    else { toast(d.error || '切换失败'); setBusy(false); }
  }).catch(e => { toast(e.message); setBusy(false); });
}
function renderUserMenu(){
  const accs = window.__accounts || [];
  const cur = (window.__account || {}).user_id;
  $('userMenu').innerHTML = accs.map(a => {
    const active = a.user_id === cur;
    return '<button role="menuitem" data-user="' + esc(a.user_id) + '"' + (active ? ' aria-current="true"' : '') + '>' +
      '<span class="uava2" style="background:' + esc(a.color || '#6d5dfc') + '">' +
        esc(String(a.name || '?').slice(0, 1)) + '</span>' +
      '<span><span class="nm">' + esc(a.name) + '</span><br><span class="ps">' + esc(a.persona) + '</span></span>' +
      (active ? '<span class="cur">当前</span>' : '') +
      '</button>';
  }).join('');
}
function toggleUserMenu(open){
  const m = $('userMenu');
  if (!m) return;
  const show = open === undefined ? !m.classList.contains('show') : !!open;
  if (show) renderUserMenu();
  m.classList.toggle('show', show);
  $('userBtn').setAttribute('aria-expanded', String(show));
}
$('userBtn').addEventListener('click', () => toggleUserMenu());
$('userMenu').addEventListener('click', ev => {
  const b = ev.target.closest('[data-user]');
  if (!b) return;
  toggleUserMenu(false);
  switchUser(b.dataset.user);
});
document.addEventListener('click', ev => {
  if (!ev.target.closest('#userMenu') && !ev.target.closest('#userBtn')) toggleUserMenu(false);
});
$('clearChat').addEventListener('click', () => { stream.innerHTML = ''; toast('对话已清空（会话状态不受影响）'); });
$('fchips').addEventListener('click', ev => {
  const b = ev.target.closest('[data-drop]'); if (b) dropFilter(b.dataset.drop);
});
$('sortSel').addEventListener('change', ev => { sortMode = ev.target.value; renderShelf(currentItems); });
$('loadMore').addEventListener('click', loadMore);
if ('IntersectionObserver' in window){
  new IntersectionObserver(entries => {
    if (entries.some(e => e.isIntersecting)) loadMore();
  }, {rootMargin: '320px'}).observe($('loadMore'));
}
$('pageSize').addEventListener('change', async ev => {
  pageSize = Number(ev.target.value) || 8;
  if (lastQuery === null){ await loadHomeShelf(true); return; }
  if (!currentItems.length){ toast('当前结果不是商品列表，无法展开'); return; }
  if (busy) return;
  setBusy(true);
  try{
    const d = await post('/api/more', {text: lastQuery, top_k: pageSize});
    if (d.ok) renderShelf(d.items, $('shelfTitle').textContent, '', null);
    else toast(d.error || '展开失败');
  }catch(e){ toast('展开失败：' + e.message); }
  setBusy(false);
});
$('viewSwitch').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-view]'); if (!b) return;
  viewMode = b.dataset.view;
  $('viewSwitch').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  renderShelf(currentItems);
});
grid.addEventListener('click', ev => {
  const card = ev.target.closest('.pcard'); if (!card) return;
  const id = card.dataset.item;
  const like = ev.target.closest('[data-like]');
  if (like){ ev.stopPropagation(); rate(id, like.dataset.like); return; }
  if (ev.target.closest('[data-cmp]')){ ev.stopPropagation(); toggleCompare(id); return; }
  if (ev.target.closest('[data-open]')) openDetail(id);
});
stream.addEventListener('click', ev => {
  const uf = ev.target.closest('[data-undo-feedback]');
  if (uf){ rate(uf.dataset.undoFeedback, uf.dataset.feedbackPolarity, true); return; }
  const fold = ev.target.closest('[data-fold]');
  if (fold){ const f = fold.closest('.fold'); f.classList.toggle('open');
             fold.textContent = (f.classList.contains('open') ? '▾ 收起依据' : '▸ 展开依据'); return; }
  const slot = ev.target.closest('[data-slot]');
  if (slot){
    if (busy){ toast('正在处理中，请稍候'); return; }
    const t = slot.dataset.slot; $('q').value = ''; ask(t); return;
  }
  const rt = ev.target.closest('[data-retry]');
  if (rt){ act(rt.dataset.retry); return; }
  if (ev.target.closest('[data-type]')){ $('q').focus(); toast('已切到输入框，直接打字即可'); return; }
  const hp = ev.target.closest('[data-help]');
  if (hp){ toast(hp.dataset.help); return; }
});
$('trayItems').addEventListener('click', ev => {
  const b = ev.target.closest('[data-unpick]'); if (b) toggleCompare(b.dataset.unpick);
});
$('trayClear').addEventListener('click', () => { picked.clear(); renderTray(); syncPickUI(); toast('已清空对比栏'); });
$('trayGo').addEventListener('click', openCompare);
document.addEventListener('click', async ev => {
  const tts = ev.target.closest('[data-tts]');
  if (tts && !tts.disabled){
    const text = decodeURIComponent(tts.dataset.tts || '');
    tts.disabled = true; tts.textContent = '合成中…';
    try{
      const d = await post('/api/tts', {text});
      if (d.ok){ new Audio(d.audio_url).play(); tts.textContent = '🔊 重新播放'; }
      else { tts.textContent = '语音不可用'; }
    }catch(e){ tts.textContent = '语音失败'; }
    tts.disabled = false;
    return;
  }
  const c = ev.target.closest('[data-close]');
  if (c){
    ev.stopPropagation();
    document.querySelector(c.dataset.close).classList.remove('show');
    if (c.dataset.close === '#modal') stopLiveGesture();
  }
});
$('modal').addEventListener('click', ev => { if (ev.target === $('modal')) closeAll(); });
$('cmpModal').addEventListener('click', ev => { if (ev.target === $('cmpModal')) closeAll(); });
document.addEventListener('keydown', ev => { if (ev.key === 'Escape'){ closeAll(); closeStatus(); toggleUserMenu(false); } });
$('toBottom').addEventListener('click', bottom);
stream.addEventListener('scroll', () => {
  const far = stream.scrollHeight - stream.scrollTop - stream.clientHeight > 160;
  $('toBottom').classList.toggle('show', far);
});
$('fab').addEventListener('click', () => {
  $('panel').classList.toggle('show');
  $('fab').textContent = $('panel').classList.contains('show') ? '✕' : '💬';
});
function syncTabbar(){
  const narrow = window.innerWidth <= 639;
  $('tabbar').classList.toggle('is-on', narrow);
  if (!narrow) $('panel').classList.remove('show');
}
window.addEventListener('resize', syncTabbar);
syncTabbar();

$('tabbar').addEventListener('click', ev => {
  const b = ev.target.closest('button[data-tab]'); if (!b) return;
  $('tabbar').querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
  const tab = b.dataset.tab;
  if (tab === 'chat'){ $('panel').classList.add('show'); $('fab').textContent = '✕'; }
  else if (tab === 'status'){ $('panel').classList.remove('show'); $('fab').textContent = '💬'; openStatus(); }
  else { $('panel').classList.remove('show'); $('fab').textContent = '💬'; window.scrollTo({top:0,behavior:'smooth'}); }
});

async function loadHomeShelf(force){
  if (!force && userSaid.length) return;   // 用户已经提问，别用首页结果覆盖
  lastQuery = null;
  try{
    const h = await (await fetch('/api/home?limit=' + pageSize)).json();
    if (h.ok && h.items && h.items.length){
      renderShelf(h.items, '猜你喜欢', '基于 v146 个性化精排的默认推荐');
    } else {
      renderShelf([], '开始逛逛');
    }
  }catch(e){
    grid.innerHTML = ''; $('empty').style.display = 'block';
    $('empty').innerHTML = '未能连接服务：' + esc(e.message) +
      '<div class="examples"><button data-reload>重试</button></div>';
    const rb = $('empty').querySelector('[data-reload]');
    if (rb) rb.onclick = () => location.reload();
  }
}

/* ============ 启动（P2：先骨架后内容） ============ */
(function bootSkeleton(){
  grid.innerHTML = Array.from({length: 4}).map(() =>
    '<div class="skel"><div class="sk1"></div><div class="sk2"></div><div class="sk3"></div></div>').join('');
  stream.innerHTML = '<div class="skb"><span class="skdot"></span><span style="color:#8b91a6">正在连接推荐助手…</span></div>';
})();

(async function boot(){
  const info = await (await fetch('/api/info')).json();
  const m = info.modes || {};
  needReal = m.multimodal !== 'real';
  $('chips').innerHTML = ['推荐 ' + zh(MODE_ZH, m.recommender), '多模态 ' + zh(MODE_ZH, m.multimodal),
                          '对话 ' + (m.llm_provider === 'online' ? '在线' : m.llm_provider === 'local' ? '本地' : '关'),
                          '搜索 ' + ((info.llm_chat || {}).web_search ? '开' : '关'),
                          '语音 ' + (info.voice ? '开' : '关')]
    .map(t => '<span class="chip' + (needReal ? ' dim' : '') + '">' + esc(t) + '</span>').join('');
  window.__accounts = info.accounts || [];
  window.__account = info.account || {};
  const acc = info.account || {name: info.user_id, color: '#6d5dfc'};
  $('uname').textContent = acc.name;
  $('uava').textContent = String(acc.name || '?').slice(0, 1);
  if (acc.color) $('uava').style.background = acc.color;
  $('llmbtn').textContent = 'LLM 解释：' + (info.llm ? '开' : '关');
  setBusy(false);
  stream.innerHTML = '';
  await loadHomeShelf();
  addBot('<div style="font-weight:620;margin-bottom:5px">你好，我是推荐助手</div>' +
    '<div style="font-size:13px;color:#8b91a6">在上方说一句需求，或点工具条用<b>语音 / 画面</b>提要求；手势可在商品详情内直接使用。' +
    '推荐结果在左侧货架：<b>点商品看详情</b>、<b>卡片右上角点赞 / 倒赞</b>、<b>点「加入对比」做字段对照</b>。' +
    (needReal ? '<br>当前为<b>离线模式</b>；用 <code>python webui.py --real</code> 可启用麦克风与摄像头。' : '') +
    '</div>');
  $('q').focus();
})();
</script>
</body>
</html>
"""


# ------------------------------------------------------------
# 商品图片：代理外部推荐系统的 /api/image/{id} 并本地缓存
# ------------------------------------------------------------
_MIME_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_EXT_MIME = {v: k for k, v in _MIME_EXT.items()}


def _recsys_image_base() -> str:
    return str(settings.RECOMMENDER_ADAPTER["http"]["base_url"]).rstrip("/")


def fetch_product_image(item_id: str, timeout_s: float | None = None) -> tuple[bytes, str] | None:
    """取商品图：先查本地缓存，再代理推荐系统接口；都拿不到返回 None。

    参考推荐系统的 /api/image/{item_id} 是**公开接口**（无鉴权），
    因此代理它不违反「不读对方内部变量」的边界约定；
    本地缓存让推荐系统未启动时图片依然可显示。
    """
    item_id = str(item_id).strip()
    if not item_id or "/" in item_id or ".." in item_id:
        return None
    cache_dir = settings.PATHS["images"]
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1) 本地缓存
    for ext, mime in _EXT_MIME.items():
        cached = cache_dir / (item_id + ext)
        if cached.exists() and cached.stat().st_size > 0:
            age_days = (time.time() - cached.stat().st_mtime) / 86400
            if age_days <= settings.IMAGE_CONFIG["recache_days"]:
                return cached.read_bytes(), mime

    # 2) 代理推荐系统
    url = _recsys_image_base() + "/api/image/" + urllib.parse.quote(item_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "integrated-ai-project"})
        with urllib.request.urlopen(req, timeout=timeout_s or settings.IMAGE_CONFIG["timeout_s"]) as resp:
            data = resp.read(settings.IMAGE_CONFIG["max_bytes"] + 1)
            ctype = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
    except Exception:
        return None
    if not data or len(data) > settings.IMAGE_CONFIG["max_bytes"]:
        return None
    ext = _MIME_EXT.get(ctype, ".jpg")
    with contextlib.suppress(OSError):
        (cache_dir / (item_id + ext)).write_bytes(data)
    return data, ctype


def category_emoji(category: str) -> str:
    table = settings.IMAGE_CONFIG["category_emoji"]
    return table.get(str(category or ""), settings.IMAGE_CONFIG["default_emoji"])


_DEP_HINTS = (
    ("GetPrototype", "protobuf 版本过高：mediapipe 0.10.14 要求 protobuf>=4.25.3,<5"),
    ("FieldDescriptor", "protobuf 版本过高：mediapipe 0.10.14 要求 protobuf>=4.25.3,<5"),
    ("torchvision::nms", "torchvision 与 torch 不配套：0.21.0 对应 torch 2.6.0"),
    ("AutoProcessor", "transformers 的 AutoProcessor 依赖可用的 torchvision，请先修 torchvision"),
)


def _dep_hint(message: str) -> str | None:
    low = (message or "").lower()
    for needle, hint in _DEP_HINTS:
        if needle.lower() in low:
            return hint
    return None


def _err(
    code: ErrorCode | str,
    message: str,
    *,
    field: str | None = None,
    hint: str | None = None,
    **extra: object,
) -> dict:
    """统一错误响应：保留前端依赖的 error 字符串，同时补齐协议要求的 code/field。"""
    payload: dict = {"ok": False, "error": message, "code": str(code), "field": field}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


def _gesture_preview_url(rows: list[dict], name: str | None, burst_dir: Path, media: Path) -> str | None:
    """选出最佳帧并生成网页可访问的预览地址。"""
    if not rows:
        return None
    best = rows[0]
    if name:
        matched = [r for r in rows if any(g.get("name") == name for g in (r.get("gestures") or []))]
        if matched:
            best = max(
                matched,
                key=lambda r: max(
                    (float(g.get("confidence") or 0.0) for g in (r.get("gestures") or []) if g.get("name") == name),
                    default=0.0,
                ),
            )
    image = str(best.get("image") or "")
    if not image:
        return None
    p = Path(image)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(media.resolve())
        except ValueError:
            rel = Path(burst_dir.name) / p.name
    else:
        rel = Path(burst_dir.name) / p.name
    return "/capture/" + urllib.parse.quote(rel.as_posix())


def _is_feedback_gesture(name: str | None) -> bool:
    """页面只接受点赞/倒赞，其他识别结果一律忽略。"""
    return bool(name and name in settings.MULTIMODAL_ADAPTER["gesture"]["feedback_map"])


def _pick_gesture(rows: list[dict]) -> tuple[str | None, float, dict[str, int], int]:
    """从连拍识别结果中按「票数 → 最高置信度」选最佳手势。"""
    votes: dict[str, int] = {}
    confs: dict[str, float] = {}
    hands = 0
    for row in rows:
        hands += int(row.get("hands_detected") or 0)
        for item in row.get("gestures") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            votes[name] = votes.get(name, 0) + 1
            confs[name] = max(confs.get(name, 0.0), float(item.get("confidence") or 0.0))
    if not votes:
        return None, 0.0, {}, hands
    name = max(votes, key=lambda n: (votes[n], confs[n]))
    return name, confs[name], votes, hands


def _from_app_error(err) -> dict:
    if err is None:
        return _err(ErrorCode.INTERNAL_ERROR, "未知错误")
    return _err(err.code, err.message, field=err.field)


class App:
    """承载 Orchestrator 与全局会话状态（加锁串行化，避免并发调用）。"""

    def __init__(
        self, user_id: str, mode: str | None, with_voice: bool, use_llm: bool, allow_real: bool | None = None
    ) -> None:
        self.lock = threading.Lock()
        self.user_id = user_id
        # allow_real 必须**显式传参**：settings.RUNTIME 在 import 时即已求值，
        # 之后再 os.environ[...]="1" 不会生效（这是实测踩到的坑）。
        self.orch = Orchestrator(mode=mode, allow_real=allow_real, with_voice=with_voice, use_llm=use_llm)
        _asr = getattr(self.orch, "asr", None)
        if _asr is not None:
            _asr.prewarm()
        _chat = getattr(self.orch, "chat", None)
        if _chat is not None:
            _chat.prewarm()
        self.session_id = self.orch.store.default_session_id(user_id)
        self.with_voice = with_voice
        self.media = settings.PATHS["artifacts"] / "webui"
        self.media.mkdir(parents=True, exist_ok=True)
        self.turn = 0

    def account(self) -> dict:
        for item in settings.DEMO_ACCOUNTS:
            if item["user_id"] == self.user_id:
                return item
        return {"user_id": self.user_id, "name": self.user_id, "persona": "", "color": "#6d5dfc"}

    def camera_start(self) -> dict:
        camera = getattr(self.orch, "camera", None)
        if camera is None:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "手势直播需要 --real 模式")
        result = camera.start()
        if not result.ok:
            return _from_app_error(result.error)
        return {"ok": True, "state": result.data}

    def camera_stop(self) -> dict:
        camera = getattr(self.orch, "camera", None)
        if camera is not None:
            camera.stop()
        return {"ok": True, "state": {"running": False}}

    def camera_state(self) -> dict:
        camera = getattr(self.orch, "camera", None)
        if camera is None:
            return {"ok": True, "state": {"running": False}}
        return {"ok": True, "state": camera.state()}

    def switch_user(self, user_id: str) -> dict:
        """切换演示账号：结束旧会话，换新 user_id 的默认会话。"""
        user_id = str(user_id).strip()
        target = next((a for a in settings.DEMO_ACCOUNTS if a["user_id"] == user_id), None)
        if target is None:
            return _err(ErrorCode.INVALID_INPUT, f"未知演示账号：{user_id}", field="user_id")
        _camera = getattr(self.orch, "camera", None)
        if _camera is not None:
            _camera.stop()
        with self.lock:
            self.orch.store.end(self.session_id)
            self.user_id = user_id
            self.session_id = self.orch.store.default_session_id(user_id)
            self.turn = 0
            _chat = getattr(self.orch, "chat", None)
            if _chat is not None:
                _chat.reset()
        return {"ok": True, "account": target, "session_id": self.session_id}

    def handle_spec(self, spec: dict, top_k: int | None = None) -> dict:
        with self.lock:
            self.turn += 1
            # 网页默认按需合成语音：每次推荐都等 TTS 会让 0.2s 的推荐变成 2~3s。
            prev_dry = self.orch.dry_run_voice
            self.orch.dry_run_voice = True
            try:
                res = self.orch.handle(spec, user_id=self.user_id, session_id=self.session_id, top_k=top_k)
            finally:
                self.orch.dry_run_voice = prev_dry
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        # 给每条推荐注入图片地址与类目兜底图标，前端不必自己拼 URL
        for item in resp.get("recommendations", []):
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        pres = resp.get("presentation") or {}
        path = pres.get("audio_path")
        if pres.get("voice_available") and path:
            pres["audio_url"] = "/audio/" + Path(path).name
        return {"ok": True, "response": resp}

    def tts(self, text: str) -> dict:
        """按需合成语音；网页点「语音播报」时才调用，避免阻塞推荐响应。"""
        text = str(text or "").strip()
        if not text:
            return _err(ErrorCode.INVALID_INPUT, "待合成文本为空", field="text")
        tmp_dir = settings.PATHS["artifacts"] / "tts" / uuid.uuid4().hex
        try:
            result = synthesize(text, out_dir=tmp_dir)
            if not result.get("available"):
                return _err(ErrorCode.MODALITY_UNAVAILABLE, str(result.get("error") or "语音合成不可用"))
            src = Path(str(result["path"]))
            name = f"tts_{uuid.uuid4().hex}{src.suffix or '.wav'}"
            target = settings.PATHS["artifacts"] / name
            shutil.copyfile(src, target)
            return {"ok": True, "audio_url": "/audio/" + name, "engine": result.get("engine")}
        except Exception as exc:
            return _err(ErrorCode.INTERNAL_ERROR, f"语音合成失败: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def chat(self, text: str, top_k: int | None = None) -> dict:
        return self.handle_spec({"source": "text", "text": text}, top_k=top_k)

    def voice(self, seconds: float, top_k: int | None = None) -> dict:
        seconds = max(1.0, min(10.0, float(seconds)))  # 防止负数/超长录音拖死服务
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "语音输入需要 --real 模式")
        wav = self.media / f"voice_{self.turn}.wav"
        rec = ad.record_audio(str(wav), seconds=seconds)
        if not rec.ok:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"录音失败: {rec.error}")
        asr_service = getattr(self.orch, "asr", None)
        asr = (
            asr_service.transcribe(str(wav), sample_rate=16000) if asr_service is not None else ad.transcribe(str(wav))
        )
        if not asr.ok:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"识别失败: {asr.error}")
        text = (asr.data or {}).get("text") or ""
        if not text.strip():
            return _err(ErrorCode.EMPTY_TRANSCRIPT, "未识别到有效语音", field="text", transcript="")
        out = self.handle_spec({"source": "voice", "text": text}, top_k=top_k)
        out["transcript"] = text
        return out

    def gesture(self, item_id: str | None = None, top_k: int | None = None) -> dict:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "手势识别需要 --real 模式")
        if item_id:
            with self.lock:
                session = self.orch.store.get(self.session_id)
                if session is not None:
                    session.focus_item_id = str(item_id)
                    if str(item_id) not in session.last_item_ids:
                        session.last_item_ids = [str(item_id), *session.last_item_ids]
        cfg = settings.MULTIMODAL_ADAPTER["gesture"]
        count = max(1, int(cfg.get("capture_frames", 5)))
        interval = max(0, int(cfg.get("capture_interval_ms", 250)))
        stamp = time.strftime("%H%M%S")
        burst_dir = self.media / f"gesture_{self.turn}_{stamp}"
        cap = ad.capture_burst(str(burst_dir), count=count, interval_ms=interval)
        rows: list[dict] = []
        if cap.ok:
            g = ad.recognize_gesture_batch(str(burst_dir / "*.jpg"), limit=count, lenient=True)
        else:
            # 连拍不可用时退回单帧，保证功能可用
            frame = self.media / f"gesture_{self.turn}.jpg"
            cap = ad.capture_frame(str(frame))
            if not cap.ok:
                return _err(ErrorCode.MODALITY_UNAVAILABLE, f"抓帧失败: {cap.error}")
            g = ad.recognize_gesture(str(frame), lenient=True)
            if g.ok:
                rows = [dict(g.data or {})]
        if not g.ok:
            msg = f"手势识别失败: {g.error}"
            hint = _dep_hint(msg)
            return _err(ErrorCode.MODALITY_UNAVAILABLE, msg + (f"（{hint}）" if hint else ""), hint=hint)
        if not rows:
            rows = list((g.data or {}).get("results") or [])
        name, confidence, votes, hands = _pick_gesture(rows)
        preview_url = _gesture_preview_url(rows, name, burst_dir, self.media)
        if name is None:
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                f"未确认出手势（已抓取 {len(rows)} 帧，检测到手 {hands} 次）。"
                "请等倒计时结束后把手举到脸旁边（画面中央），保持手势约 1 秒。",
                preview_url=preview_url,
            )
        min_votes = max(1, int(cfg.get("min_votes", 3)))
        if votes.get(name, 0) < min_votes:
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                f"手势结果不稳定（{name} 仅 {votes.get(name, 0)}/{len(rows)} 帧，"
                f"至少需要 {min_votes} 帧），请保持手势约 1 秒再试。",
                preview_url=preview_url,
            )
        extra = {
            "gesture": name,
            "gesture_confidence": round(confidence, 4),
            "gesture_frames": len(rows),
            "gesture_votes": votes,
            "preview_url": preview_url,
        }
        if not _is_feedback_gesture(name):
            return _err(
                ErrorCode.INVALID_INPUT,
                f"当前只支持 👍 点赞 / 👎 倒赞，检测到「{name}」已忽略。",
                field="gesture",
                gesture=name,
                gesture_confidence=round(confidence, 4),
                gesture_frames=len(rows),
                gesture_votes=votes,
                preview_url=preview_url,
            )
        out = self.handle_spec({"source": "gesture", "gesture": {"name": name, "confidence": confidence}}, top_k=top_k)
        out.update(extra)
        return out

    def vision(self, question: str, top_k: int | None = None) -> dict:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "视觉理解需要 --real 模式")
        frame = self.media / f"vision_{self.turn}.jpg"
        cap = ad.capture_frame(str(frame))
        if not cap.ok:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"抓帧失败: {cap.error}")
        vl = ad.describe_image(str(frame), question or "")
        if not vl.ok:
            msg = f"视觉理解失败: {vl.error}"
            hint = _dep_hint(msg)
            return _err(ErrorCode.MODALITY_UNAVAILABLE, msg + (f"（{hint}）" if hint else ""), hint=hint)
        desc = (vl.data or {}).get("response") or ""
        out = self.handle_spec(
            # 没有提问时 text 必须传 None：传空串会被 parse_event 当成「空 ASR 文本」
            # 直接返回 E_EMPTY_TRANSCRIPT，视觉标签根本走不到（modality=visual 分支）。
            {"source": "vision", "text": question or None, "visual_tags": [{"tag": desc[:24], "confidence": 0.85}]},
            top_k=top_k,
        )
        out["description"] = desc
        return out

    def control(self, action: str) -> dict:
        if action == "undo":
            return self.handle_spec({"source": "text", "text": "撤销"})
        if action == "reset":
            _camera = getattr(self.orch, "camera", None)
            if _camera is not None:
                _camera.stop()
            with self.lock:
                self.orch.store.end(self.session_id)
                self.session_id = f"{self.session_id}-{self.turn}"
            _chat = getattr(self.orch, "chat", None)
            if _chat is not None:
                _chat.reset()
            return {
                "ok": True,
                "response": {
                    "status": "ok",
                    "recommendations": [],
                    "trace": {"latency_ms": {}},
                    "intent_result": {"intent": "reset"},
                },
            }
        return _err(ErrorCode.INVALID_INPUT, "未知操作")

    def product(self, item_id: str) -> dict:
        """商品详情：本地目录事实 + 相似商品（供详情弹窗使用）。"""
        cat = self.orch.catalog
        product = cat.product(item_id)
        if product is None:
            return _err(ErrorCode.ITEM_NOT_FOUND, "商品不在目录中")
        similar = []
        for sid in cat.similar_of(item_id, k=8):
            sp = cat.product(sid) or {}
            similar.append(
                {
                    "item_id": sid,
                    "title": sp.get("title", ""),
                    "category": sp.get("category", ""),
                    "price": sp.get("price"),
                    "emoji": category_emoji(sp.get("category", "")),
                    "img_url": "/api/img/" + urllib.parse.quote(sid),
                }
            )
        return {
            "ok": True,
            "product": {
                "item_id": item_id,
                "title": product.get("title", ""),
                "english_title": product.get("english_title", ""),
                "category": product.get("category", ""),
                "price": product.get("price"),
                "currency": product.get("currency", ""),
                "price_source": product.get("price_source"),
                "transactions": product.get("transactions"),
                "emoji": category_emoji(product.get("category", "")),
                "img_url": "/api/img/" + urllib.parse.quote(item_id),
            },
            "similar": similar,
        }

    def home(self, limit: int = 8) -> dict:
        """首页商品：用**独立会话**跑一次发现型查询，不污染对话历史。

        这样打开页面就有货可看（电商首页观感），而对话面板保持干净。
        """
        spec = {"source": "text", "text": "推荐一些热门商品"}
        limit = max(1, min(40, int(limit)))
        with self.lock:
            # 首页只需要文本货架；合成语音会让 /api/home 从 ~5ms 涨到 ~3.3s，
            # 直接把 LCP 拖到 4s+。这里在锁内临时 dry-run，调用完恢复。
            prev_dry_run = self.orch.dry_run_voice
            prev_llm = self.orch.llm_explainer.enabled
            self.orch.dry_run_voice = True
            # 首页货架必须快：即使运行时打开了「LLM 解释」，也不在这里调用
            # 4B 模型。否则一个 /api/home 会持锁几十秒，重置/翻页全部卡住。
            self.orch.llm_explainer.enabled = False
            try:
                res = self.orch.handle(spec, user_id=self.user_id, session_id=self.session_id + "-home", top_k=limit)
            finally:
                self.orch.dry_run_voice = prev_dry_run
                self.orch.llm_explainer.enabled = prev_llm
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        for item in resp.get("recommendations", []):
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        return {
            "ok": True,
            "items": resp.get("recommendations", []),
            "title": "猜你喜欢",
            "subtitle": "基于 v146 个性化精排的默认推荐",
        }

    def more(self, text: str, top_k: int = 16) -> dict:
        """把当前商品列表扩到 top_k 件；用独立会话，不污染对话历史。"""
        top_k = max(1, min(120, int(top_k)))
        spec = {"source": "text", "text": text or "推荐一些热门商品"}
        sid = f"{self.session_id}-more-{int(time.time() * 1000)}"
        with self.lock:
            prev_dry_run = self.orch.dry_run_voice
            prev_llm = self.orch.llm_explainer.enabled
            self.orch.dry_run_voice = True
            self.orch.llm_explainer.enabled = False
            try:
                res = self.orch.handle(spec, user_id=self.user_id, session_id=sid, top_k=top_k)
            finally:
                self.orch.dry_run_voice = prev_dry_run
                self.orch.llm_explainer.enabled = prev_llm
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        items = list(resp.get("recommendations", []))
        # 推荐系统的候选池通常只到 40 件；无限滚动再往后时用目录热门补齐，
        # 明确标注「商品目录补充」，不冒充个性化精排结果。
        if len(items) < top_k:
            catalog = self.orch.catalog
            seen = {str(item.get("item_id")) for item in items}
            candidates = list(catalog.popular.get("global_top") or [])
            if len(candidates) < top_k:
                candidates.extend(k for k in catalog.catalog if k not in candidates)
            for iid in candidates:
                if len(items) >= top_k:
                    break
                iid = str(iid)
                if iid in seen:
                    continue
                product = catalog.product(iid)
                if not product:
                    continue
                items.append(
                    {
                        "item_id": iid,
                        "title": product.get("title") or product.get("english_title") or iid,
                        "category": product.get("category", "其他"),
                        "price": product.get("price"),
                        "currency": product.get("currency", catalog.currency),
                        "score": max(0.10, 0.30 - 0.001 * len(items)),
                        "rank": len(items) + 1,
                        "sources": ["商品目录补充"],
                        "evidence": {"matched_constraints": []},
                        "explanation": "来自商品目录的更多商品，未参与个性化精排。",
                        "explanation_source": "template",
                        "explanation_grounded": True,
                        "evidence_count": 0,
                    }
                )
                seen.add(iid)
        for item in items:
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        return {"ok": True, "items": items, "top_k": top_k}

    def view(self, item_id: str) -> dict:
        """「继续浏览」弱反馈：用户打开商品详情即上报一次。

        与点赞共用一条链路，但走 source=browse（弱正反馈，只轻微上浮，
        不改变正负反馈状态，也不屏蔽任何商品）。
        """
        if not self.orch.catalog.exists(str(item_id)):
            return _err(ErrorCode.ITEM_NOT_FOUND, "商品不在目录中")
        return self.handle_spec({"source": "browse", "item_id": str(item_id)})

    def feedback(self, item_id: str, polarity: str, top_k: int | None = None) -> dict:
        """商品卡上的点赞/倒赞：先把会话焦点指到该商品，再走正规反馈链路。

        复用 Orchestrator 的反馈闭环（正负互斥、会话隔离、反馈后重排），
        不另起一套逻辑 —— 这样页面上的点赞与语音/手势点赞行为完全一致。
        """
        raw_polarity = str(polarity).lower()
        if raw_polarity in ("none", "clear", "cancel", "off"):
            prev_llm = self.orch.llm_explainer.enabled
            self.orch.llm_explainer.enabled = False
            try:
                result = self.orch.clear_feedback(
                    str(item_id),
                    user_id=self.user_id,
                    session_id=self.session_id,
                    top_k=top_k,
                )
                return {"ok": True, "response": result.data} if result.ok else _from_app_error(result.error)
            finally:
                self.orch.llm_explainer.enabled = prev_llm

        polarity = "positive" if raw_polarity in ("positive", "up", "like", "1") else "negative"
        name = "thumbs_up" if polarity == "positive" else "thumbs_down"
        with self.lock:
            sess = self.orch.store.get_or_create(self.session_id, self.user_id)
            if self.orch.catalog.exists(str(item_id)):
                sess.focus_item_id = str(item_id)
                if str(item_id) not in sess.last_item_ids:
                    sess.last_item_ids = [str(item_id), *sess.last_item_ids]
        prev_llm = self.orch.llm_explainer.enabled
        self.orch.llm_explainer.enabled = False
        try:
            return self.handle_spec({"source": "gesture", "gesture": {"name": name, "confidence": 1.0}}, top_k=top_k)
        finally:
            self.orch.llm_explainer.enabled = prev_llm

    def products(self, item_ids: list[str]) -> dict:
        """批量取商品详情 —— 供对比栏做字段对照。

        设计文档 5.4 建议的接口：避免前端为 N 件商品发 N 次请求。
        数据只来自本地目录，不依赖外部服务。
        """
        cat = self.orch.catalog
        out = []
        for item_id in (item_ids or [])[:8]:
            product = cat.product(str(item_id))
            if not product:
                continue
            out.append(
                {
                    "item_id": str(item_id),
                    "title": product.get("title", ""),
                    "english_title": product.get("english_title", ""),
                    "category": product.get("category", ""),
                    "price": product.get("price"),
                    "currency": product.get("currency", ""),
                    "price_source": product.get("price_source"),
                    "transactions": product.get("transactions"),
                    "emoji": category_emoji(product.get("category", "")),
                    "img_url": "/api/img/" + urllib.parse.quote(str(item_id)),
                }
            )
        if not out:
            return _err(ErrorCode.NO_RESULT, "没有可用的商品")
        return {"ok": True, "products": out}

    def state(self) -> dict:
        sess = self.orch.store.get(self.session_id)
        return sess.snapshot() if sess else {}

    def info(self) -> dict:
        chat_report = (
            self.orch.chat.report() if self.orch.chat is not None and hasattr(self.orch.chat, "report") else {}
        )
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "modes": self.orch.modes(),
            "ok": True,
            "account": self.account(),
            "accounts": settings.DEMO_ACCOUNTS,
            "voice": self.with_voice,
            "llm": bool(self.orch.llm_explainer.enabled),
            "llm_chat": chat_report,
        }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 安全响应头（本地演示也加上，避免把页面当内嵌框架/嗅探类型）
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
                "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            if length > 1_000_000:  # 1MB 上限：本地接口不需要更大请求体
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except Exception:
                return {}

        def _stream_camera(self) -> None:
            camera = getattr(app.orch, "camera", None)
            if camera is None or not camera.running:
                self._json(_err(ErrorCode.MODALITY_UNAVAILABLE, "手势直播未开启"), 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                while True:
                    data = camera.frame()
                    if data:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(0.06)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_GET(self):
            if self.path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                return
            if self.path == "/api/info":
                self._json(app.info())
                return
            if self.path.startswith("/camera/stream"):
                self._stream_camera()
                return
            if self.path == "/api/camera/state":
                self._json(app.camera_state())
                return
            if self.path.startswith("/api/home"):
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((qs.get("limit") or ["8"])[0])
                except ValueError:
                    limit = 8
                self._json(app.home(limit))
                return
            if self.path.startswith("/capture/"):
                rel = urllib.parse.unquote(self.path[len("/capture/") :].split("?")[0])
                base = (settings.PATHS["artifacts"] / "webui").resolve()
                target = (base / rel).resolve()
                if target.is_relative_to(base) and target.is_file():
                    ctype = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
                    self._send(200, target.read_bytes(), ctype)
                else:
                    self._json(_err(ErrorCode.ITEM_NOT_FOUND, "抓拍画面不存在", field="path"), 404)
                return
            if self.path.startswith("/api/img/"):
                item_id = urllib.parse.unquote(self.path[len("/api/img/") :].split("?")[0])
                got = fetch_product_image(item_id)
                if got is None:
                    self._json(_err(ErrorCode.ITEM_NOT_FOUND, "图片不存在", field="item_id"), 404)
                else:
                    data, ctype = got
                    self._send(200, data, ctype)
                return
            if self.path.startswith("/api/product/"):
                item_id = urllib.parse.unquote(self.path[len("/api/product/") :].split("?")[0])
                self._json(app.product(item_id))
                return
            if self.path.startswith("/audio/"):
                name = self.path[len("/audio/") :].split("?")[0]
                target = settings.PATHS["artifacts"] / Path(name).name
                if target.exists():
                    ctype = "audio/wav" if target.suffix == ".wav" else "audio/mpeg"
                    self._send(200, target.read_bytes(), ctype)
                else:
                    self._json(_err(ErrorCode.ITEM_NOT_FOUND, "音频不存在", field="name"), 404)
                return
            self._json(_err(ErrorCode.INVALID_INPUT, f"未知路径：{self.path}", field="path"), 404)

        def do_POST(self):
            body = self._body()
            try:
                if self.path == "/api/chat":
                    raw_k = body.get("top_k")
                    self._json(app.chat(str(body.get("text", "")), int(raw_k) if raw_k else None))
                elif self.path == "/api/more":
                    raw_k = body.get("top_k")
                    self._json(app.more(str(body.get("text", "")), int(raw_k) if raw_k else 16))
                elif self.path == "/api/tts":
                    self._json(app.tts(str(body.get("text", ""))))
                elif self.path == "/api/camera/start":
                    self._json(app.camera_start())
                elif self.path == "/api/camera/stop":
                    self._json(app.camera_stop())
                elif self.path == "/api/voice":
                    raw_k = body.get("top_k")
                    self._json(app.voice(float(body.get("seconds", 4)), int(raw_k) if raw_k else None))
                elif self.path == "/api/gesture":
                    raw_k = body.get("top_k")
                    self._json(app.gesture(str(body.get("item_id") or "") or None, int(raw_k) if raw_k else None))
                elif self.path == "/api/vision":
                    raw_k = body.get("top_k")
                    self._json(app.vision(str(body.get("question", "")), int(raw_k) if raw_k else None))
                elif self.path == "/api/undo":
                    self._json(app.control("undo"))
                elif self.path == "/api/reset":
                    self._json(app.control("reset"))
                elif self.path == "/api/products":
                    ids = body.get("item_ids") or []
                    if isinstance(ids, list):
                        self._json(app.products(list(ids)))
                    else:
                        self._json(_err(ErrorCode.INVALID_INPUT, "item_ids 必须是数组", field="item_ids"), 400)
                elif self.path == "/api/view":
                    self._json(app.view(str(body.get("item_id", ""))))
                elif self.path == "/api/feedback":
                    raw_k = body.get("top_k")
                    self._json(
                        app.feedback(
                            str(body.get("item_id", "")),
                            str(body.get("polarity", "positive")),
                            int(raw_k) if raw_k else None,
                        )
                    )
                elif self.path == "/api/user":
                    self._json(app.switch_user(str(body.get("user_id", ""))))
                elif self.path == "/api/state":
                    self._json({"ok": True, "state": app.state()})
                elif self.path == "/api/llm":
                    with app.lock:
                        enabled = not bool(app.orch.llm_explainer.enabled)
                        app.orch.llm_explainer.enabled = enabled
                    self._json({"ok": True, "enabled": enabled})
                else:
                    self._json(_err(ErrorCode.INVALID_INPUT, f"未知接口：{self.path}", field="path"), 404)
            except Exception as exc:  # 任何异常都回结构化错误，不让页面卡死
                msg = f"{type(exc).__name__}: {exc}"
                hint = _dep_hint(msg)
                payload = _err(ErrorCode.INTERNAL_ERROR, msg + (f"（{hint}）" if hint else ""), hint=hint)
                self._json(payload, 500)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="网页对话界面")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--user", default="15547")
    parser.add_argument("--mode", default=None, choices=["offline", "auto", "online"])
    parser.add_argument("--voice", action="store_true", help="开启语音播报")
    parser.add_argument("--real", action="store_true", help="真实多模态")
    parser.add_argument("--llm", action="store_true", help="真实 LLM 生成解释")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    settings.ensure_dirs()
    mode = args.mode or ("auto" if args.real else None)
    allow_real = True if args.real else None
    if args.real:
        os.environ.setdefault("INTEGRATED_DEMO_USER", "15547")

    app = App(args.user, mode, args.voice, args.llm, allow_real)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    url = f"http://{args.host}:{args.port}"
    modes = app.orch.modes()
    print("=" * 70)
    print("多模态个性化推荐助手 · 网页对话界面")
    print("=" * 70)
    print(f"地址： {url}")
    print(
        f"用户： {args.user}   多模态：{modes['multimodal']}   推荐：{modes['recommender']}   "
        f"LLM：{modes.get('llm_explain')}   对话后端：{modes.get('llm_provider')}   "
        f"语音播报：{'开' if args.voice else '关'}"
    )
    print("按 Ctrl+C 停止服务\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
        _chat = getattr(app.orch, "chat", None)
        if _chat is not None:
            _chat.stop()
        _asr = getattr(app.orch, "asr", None)
        if _asr is not None:
            _asr.stop()
        _camera = getattr(app.orch, "camera", None)
        if _camera is not None:
            _camera.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
