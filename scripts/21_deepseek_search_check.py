"""
scripts/21_deepseek_search_check.py
===================================

DeepSeek V4.1-Flash + Search/Fetch 实测。

用法：
    python scripts/21_deepseek_search_check.py
    python scripts/21_deepseek_search_check.py --query "DeepSeek 最新版本" --count 5
    python scripts/21_deepseek_search_check.py --answer --json

需要 local.env 中配置 DEEPSEEK_API_KEY。未配置时不发起任何网络请求。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from src.llm_chat import ChatService  # noqa: E402
from src.online_llm import OnlineChatClient, resolve_online_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek Search/Fetch 实测")
    parser.add_argument("--query", default="DeepSeek V4.1 Flash 最新版本是什么？", help="搜索问题")
    parser.add_argument("--count", type=int, default=5, help="返回结果条数（1~50）")
    parser.add_argument("--content", action="store_true", help="同时返回网页正文")
    parser.add_argument("--answer", action="store_true", help="把搜索结果注入模型并生成回答")
    parser.add_argument("--json", action="store_true", help="写入 outputs/deepseek_search_check.json")
    args = parser.parse_args()

    cfg = resolve_online_config()
    if cfg is None:
        print("未检测到 DeepSeek 配置，未发起网络请求。请先在 local.env 填写 DEEPSEEK_API_KEY。")
        return 2

    client = OnlineChatClient(cfg)
    if not client.search_available:
        print(f"当前 provider={cfg.provider} 不支持 DeepSeek Search/Fetch。")
        return 3

    started = time.perf_counter()
    result = client.search_web(args.query, count=args.count, need_content=args.content)
    search_ms = round((time.perf_counter() - started) * 1000, 2)
    report: dict = {
        "ok": result.ok,
        "provider": cfg.provider,
        "model": cfg.model,
        "query": args.query,
        "search_latency_ms": search_ms,
        "count": 0,
        "sources": [],
        "error": result.error.to_dict() if result.error else None,
    }

    if result.ok:
        data = result.data or {}
        rows = list(data.get("results") or [])
        report["count"] = len(rows)
        report["sources"] = [
            {
                "title": row.get("title"),
                "url": row.get("url"),
                "site_name": row.get("site_name"),
                "date": row.get("date"),
                "authority": row.get("authority"),
            }
            for row in rows
        ]
        print(f"provider={cfg.provider} model={cfg.model} sources={len(rows)} search_ms={search_ms}")
        for index, row in enumerate(rows, 1):
            print(f"[{index}] {row.get('title')} · {row.get('site_name')} · {row.get('url')}")

        if args.answer:
            context = ChatService._format_search_results(rows)
            answer_result = client.chat(args.query, context=context)
            report["answer_ok"] = answer_result.ok
            report["answer"] = answer_result.data if answer_result.ok else None
            report["answer_error"] = answer_result.error.to_dict() if answer_result.error else None
            if answer_result.ok:
                print("\n" + str(answer_result.data))
    else:
        print("搜索失败：" + (result.error.message if result.error else "未知错误"))

    if args.json:
        settings.ensure_dirs()
        target = settings.PATHS["outputs"] / "deepseek_search_check.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"证据已写入 {target}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
