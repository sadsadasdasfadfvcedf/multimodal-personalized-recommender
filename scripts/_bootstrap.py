"""scripts/_bootstrap.py —— 让所有脚本都能以 `python scripts/xx.py` 方式运行。"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def banner(title: str) -> None:
    line = "=" * 68
    print(f"\n{line}\n{title}\n{line}")
