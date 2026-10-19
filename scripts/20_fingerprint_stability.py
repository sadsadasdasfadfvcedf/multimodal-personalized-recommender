"""
scripts/20_fingerprint_stability.py
===================================

验证证据指纹本身可重复：连续跑两遍离线套件，比较归一化后的 13 个证据文件。

只有两次完全一致，才允许更新 ``tests/golden/evidence_fingerprint.json``；
否则说明证据里仍有未归一化的随机源，更新黄金基准只会把噪声固化下来。

用法：
    python scripts/20_fingerprint_stability.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_DIR = PROJECT_ROOT / "tests"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from _fingerprint import fingerprint_dir  # noqa: E402
from conftest import OUTPUTS, SUITE_OUTPUTS, clean_caches  # noqa: E402


def run_once() -> dict[str, str]:
    clean_caches()
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_all_offline.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError("离线套件失败：\n" + (proc.stdout or "")[-4000:])
    return fingerprint_dir(OUTPUTS, SUITE_OUTPUTS)


def main() -> int:
    print("第 1 轮：运行离线套件并计算指纹 ...")
    first = run_once()
    print("第 2 轮：运行离线套件并计算指纹 ...")
    second = run_once()

    changed = sorted(name for name in set(first) | set(second) if first.get(name) != second.get(name))
    if changed:
        print("指纹不稳定，以下文件两轮不一致：" + "、".join(changed))
        return 1
    print(f"稳定：{len(first)} 个证据文件两轮指纹完全一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
