"""
scripts/19_online_llm_check.py
==============================

在线 LLM 联通性与延迟实测。

用法：
    set DASHSCOPE_API_KEY=sk-...
    python scripts/19_online_llm_check.py

    # 任意 OpenAI 兼容网关
    set INTEGRATED_ONLINE_LLM_API_KEY=sk-...
    set INTEGRATED_ONLINE_LLM_BASE_URL=https://your-gateway/v1
    set INTEGRATED_ONLINE_LLM_MODEL=your-model
    python scripts/19_online_llm_check.py

未配置 Key 时脚本以退出码 2 结束，不发起任何网络请求。
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
from src.online_llm import OnlineChatClient, resolve_online_config  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="在线 LLM 联通性与延迟实测")
    parser.add_argument("--prompt", default="用一句话解释什么是协同过滤。", help="测试问题")
    parser.add_argument("--json", action="store_true", help="把结果写入 outputs/online_llm_check.json")
    args = parser.parse_args()

    cfg = resolve_online_config()
    if cfg is None:
        print("未检测到在线 LLM 配置，未发起网络请求。")
        print("请设置 DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY 之一，")
        print("或设置 INTEGRATED_ONLINE_LLM_API_KEY + BASE_URL + MODEL。")
        return 2

    client = OnlineChatClient(cfg)
    started = time.perf_counter()
    result = client.chat(args.prompt)
    elapsed = round((time.perf_counter() - started) * 1000, 2)

    report = {
        "ok": result.ok,
        "provider": cfg.provider,
        "model": cfg.model,
        "endpoint_host": cfg.public_info()["endpoint_host"],
        "latency_ms": elapsed,
        "answer": result.data if result.ok else None,
        "error": result.error.to_dict() if result.error else None,
    }

    print(f"provider={report['provider']} model={report['model']} latency_ms={elapsed}")
    if result.ok:
        print(str(result.data))
    else:
        print("调用失败：" + (result.error.message if result.error else "未知错误"))

    if args.json:
        settings.ensure_dirs()
        target = settings.PATHS["outputs"] / "online_llm_check.json"
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"证据已写入 {target}")

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
