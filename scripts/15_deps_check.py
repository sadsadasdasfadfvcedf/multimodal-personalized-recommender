"""
scripts/15_deps_check.py
========================

真实多模态依赖深检（演示前可选，不联网）。

检查内容：
    1. mediapipe 的 protobuf 约束是否满足（0.10.14 要求 <5）
    2. torchvision 的 torch 约束是否满足（0.21.0 要求 ==2.6.0）
    3. 真正 import mediapipe / torchvision，并跑一次 NMS 冒烟
    4. transformers.AutoProcessor 能否导入（视觉链路的常见失败点）

输出：outputs/deps_check.json
"""

from __future__ import annotations

from _bootstrap import banner

from config import settings
from src.deps import check_stack
from src.utils import now_iso, write_json


def main() -> int:
    banner("真实多模态依赖深检")
    report = {"checked_at": now_iso(), **check_stack(deep=True)}
    path = write_json(settings.PATHS["outputs"] / "deps_check.json", report)

    for name, row in report["checks"].items():
        mark = "PASS" if row.get("ok") is not False else "FAIL"
        detail = row.get("error") or row.get("hint") or row.get("requires") or ""
        print(f"  [{mark}] {name}: {detail}")

    print()
    if report["problems"]:
        print("发现问题：")
        for item in report["problems"]:
            print("  - " + item)
        print("\n建议：" + str(report["hint"]))
    else:
        print("依赖组合兼容，真实多模态模式可用。")
    print(f"\n[15_deps_check] 证据已写入 {path.name}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
