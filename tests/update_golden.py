"""重新生成黄金指纹基准（只在行为**按预期**改变后运行）。

用法：
    python tests/update_golden.py

它会先清空 __pycache__、跑一遍离线套件，再把归一化后的证据指纹写入
tests/golden/evidence_fingerprint.json。

⚠️ 只有当行为**确实按预期改变**时才更新；纯重构不该需要动它。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from _fingerprint import fingerprint_dir  # noqa: E402
from conftest import (  # noqa: E402
    GOLDEN_FINGERPRINT,
    OUTPUTS,
    SUITE_OUTPUTS,
    clean_caches,
)


def main() -> int:
    removed = clean_caches()
    print(f"已清理 {removed} 个工具缓存目录")
    print("跑一遍离线套件以刷新证据 ...")
    proc = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_all_offline.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    sys.stdout.write((proc.stdout or "")[-800:])
    if proc.returncode != 0:
        print("套件未通过，拒绝更新黄金基准。")
        return 1

    fp = fingerprint_dir(OUTPUTS, SUITE_OUTPUTS)
    GOLDEN_FINGERPRINT.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_FINGERPRINT.write_text(json.dumps(fp, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    print(f"黄金指纹已更新：{GOLDEN_FINGERPRINT}（{len(fp)} 个文件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
