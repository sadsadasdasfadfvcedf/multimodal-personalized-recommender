"""scripts/_bootstrap.py —— 让所有脚本都能以 `python scripts/xx.py` 方式运行。"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def ensure_utf8_console() -> None:
    """让 Windows 控制台按 UTF-8 输出，避免 GBK 控制台直接崩掉。

    背景：中文 Windows 控制台默认代码页 936（GBK）。脚本里打印 `▶`、`✅`
    这类字符时，**父进程**会抛 UnicodeEncodeError 直接退出 —— 给子进程设
    PYTHONIOENCODING 不管用，因为崩的是父进程（run_all_offline 曾复现）。

    这里同时做两件事：
    1. 把控制台代码页切到 65001，避免「不崩但全是乱码」；
    2. 把 stdout/stderr 重新配置为 UTF-8 + errors=replace，彻底不抛。
    """
    if os.name == "nt":
        with contextlib.suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]


ensure_utf8_console()


def banner(title: str) -> None:
    line = "=" * 68
    print(f"\n{line}\n{title}\n{line}")
