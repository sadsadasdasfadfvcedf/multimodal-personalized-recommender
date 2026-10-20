"""在线视觉识图检查（可选，使用 DashScope Qwen VL）。

默认从本机 :8020 代理获取一张商品图，连续调用两次在线识图，
验证模型配置、响应解析和延迟。未配置 Key 时跳过，不影响离线验收。
"""

from __future__ import annotations

import argparse
import os
import tempfile
import urllib.request
from pathlib import Path

from _bootstrap import banner

from config import settings
from src.utils import now_iso, write_json
from src.vision_api import DashScopeVisionClient


def main() -> int:
    parser = argparse.ArgumentParser(description="在线视觉识图检查")
    parser.add_argument("--url", default="http://127.0.0.1:8020")
    parser.add_argument("--item-id", default="22698")
    parser.add_argument("--image", default=None)
    args = parser.parse_args()

    banner("在线视觉识图检查（Qwen VL / DashScope）")
    client = DashScopeVisionClient.from_env()
    report: dict = {
        "checked_at": now_iso(),
        "configured": client is not None,
        "mode": "online" if client is not None else "skipped",
        "checks": [],
        "verdict": "SKIP",
    }
    if client is None:
        print("跳过：未配置 DASHSCOPE_API_KEY")
        path = write_json(settings.PATHS["outputs"] / "online_vision_check.json", report)
        print(f"[22_online_vision_check] 证据已写入 {path.name}")
        return 0

    if args.image:
        image_path = Path(args.image)
        cleanup = None
    else:
        fd, tmp_name = tempfile.mkstemp(prefix="vl-check-", suffix=".jpg")
        os.close(fd)
        image_path = Path(tmp_name)
        cleanup = image_path
        image_url = f"{args.url.rstrip('/')}/api/img/{args.item_id}"
        with urllib.request.urlopen(image_url, timeout=15) as response:
            image_path.write_bytes(response.read())

    try:
        for index in (1, 2):
            result = client.describe_image(image_path)
            item = {
                "round": index,
                "ok": result.ok,
                "error": str(result.error) if not result.ok else None,
                "description": (result.data or {}).get("response") if result.ok else None,
                "model": (result.data or {}).get("model") if result.ok else None,
                "latency_ms": (result.data or {}).get("latency_ms") if result.ok else None,
            }
            report["checks"].append(item)
            print(f"[{'PASS' if result.ok else 'FAIL'}] 第 {index} 次：{item}")
    finally:
        if cleanup is not None:
            cleanup.unlink(missing_ok=True)

    passed = all(item["ok"] for item in report["checks"])
    report["verdict"] = "PASS" if passed else "FAIL"
    report["model"] = client.cfg.model
    report["provider"] = client.cfg.provider
    path = write_json(settings.PATHS["outputs"] / "online_vision_check.json", report)
    print(f"[22_online_vision_check] 证据已写入 {path.name}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
