"""scripts/_bootstrap.py —— 控制台编码兜底。

回归锁：中文 Windows 控制台默认代码页 936（GBK），打印 `▶` / `✅` 会让
**父进程**抛 UnicodeEncodeError（run_all_offline 曾因此 exit 1，一个阶段都没跑）。
"""

from __future__ import annotations

import os
import subprocess
import sys

from conftest import PROJECT_ROOT


def test_dotenv_loads_missing_values_without_overriding(tmp_path):
    from config import settings

    env_file = tmp_path / "local.env"
    env_file.write_text(
        "# comment\nDEEPSEEK_API_KEY=sk-from-dotenv\nINTEGRATED_LLM_PROVIDER=deepseek\n",
        encoding="utf-8",
    )
    target: dict[str, str] = {"INTEGRATED_LLM_PROVIDER": "already-set"}

    settings._load_dotenv(env_file, target)

    assert target["DEEPSEEK_API_KEY"] == "sk-from-dotenv"
    assert target["INTEGRATED_LLM_PROVIDER"] == "already-set"


def test_gbk_console_does_not_crash_on_symbols(tmp_path):
    scripts_dir = PROJECT_ROOT / "scripts"
    script = tmp_path / "probe.py"
    script.write_text(
        "import sys\n"
        f"sys.path.insert(0, r'{scripts_dir}')\n"
        "from _bootstrap import banner\n"
        "banner('中文标题')\n"
        "print('▶ 阶段01 · 契约 ✅ ❌')\n",
        encoding="utf-8",
    )
    env = {**os.environ, "PYTHONIOENCODING": "gbk"}
    proc = subprocess.run([sys.executable, "-B", str(script)], capture_output=True, env=env, timeout=60)
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")[-600:]
