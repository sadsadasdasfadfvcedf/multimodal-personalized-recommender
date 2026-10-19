"""pytest 公共夹具。

设计要点
--------
1. **会话级只跑一次完整离线套件**（约 22 秒），后续测试共享其产物；
2. 证据类测试通过 `offline_suite` 夹具声明依赖，保证「先跑套件、再读证据」的顺序；
3. 把项目根加入 sys.path，使 `from src.xxx import ...` / `from config import ...` 可用；
4. 只锁定 run_all_offline.py 会重新生成的证据文件 —— 其余（真实模型 / LLM 对比 /
   网页验收）属于按需脚本产物，跑离线套件时不会被刷新，锁进去会产生假失败；
5. 跑套件前先清空工具缓存目录（见 clean_caches 的说明）。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 测试必须确定性、零网络、零 API 额度：即使本机 local.env 配了 DeepSeek Key，
# 也不允许 ChatService 在测试进程里自动启用在线后端。
os.environ["INTEGRATED_LLM_PROVIDER"] = "local"
os.environ["INTEGRATED_ONLINE_LLM"] = "0"
for _secret_env in (
    "INTEGRATED_ONLINE_LLM_API_KEY",
    "DASHSCOPE_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "SILICONFLOW_API_KEY",
    "MOONSHOT_API_KEY",
    "ZHIPUAI_API_KEY",
):
    os.environ.pop(_secret_env, None)

OUTPUTS = PROJECT_ROOT / "outputs"
GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
GOLDEN_FINGERPRINT = GOLDEN_DIR / "evidence_fingerprint.json"

# run_all_offline.py 覆盖的 9 个阶段所产出的证据文件
SUITE_OUTPUTS = [
    "env_check.json",
    "stage01_contract_report.json",
    "stage02_context_cases.jsonl",
    "stage02_context_metrics.json",
    "stage03_demo.json",
    "stage03_traces.jsonl",
    "stage04_explanation_cases.jsonl",
    "stage04_explanations.json",
    "stage05_feedback_ab.json",
    "stage06_acceptance.json",
    "e2e_run_log.jsonl",
    "docs_manifest.json",
    "text_norm_check.json",
]

STAGE_OUTPUTS = {
    "01": "stage01_contract_report.json",
    "02": "stage02_context_metrics.json",
    "03": "stage03_demo.json",
    "04": "stage04_explanations.json",
    "05": "stage05_feedback_ab.json",
    "06": "stage06_acceptance.json",
}

# 工具缓存目录：脚本 06 会把它们当作「不该进提交包」的东西清理掉，
# 并把清理清单写进证据，所以它们的存在与否会直接进入证据内容。
CACHE_DIRS = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")


def clean_caches() -> int:
    """清空项目内全部工具缓存目录，返回删除的目录数。

    为什么必须做
    ------------
    stage06 的证据里有 `cleanup.removed_dirs` / `cleanup.removed_files`，
    记录的是「本次运行删掉了哪些缓存目录」，其内容取决于 **运行前的缓存状态**。
    例如只跑过 pytest、没跑过 ruff 的机器，会少一项 `.ruff_cache`。

    若不先归一化，同一份代码两次运行会产出内容不同的证据文件，
    再叠加上「第一次跑才出现」的差异，回归指纹就会随机假失败。
    """
    removed = 0
    for name in CACHE_DIRS:
        for path in sorted(PROJECT_ROOT.rglob(name), reverse=True):
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
    return removed


@pytest.fixture(scope="session")
def offline_suite():
    """清缓存后跑一次 `scripts/run_all_offline.py`，返回 CompletedProcess。"""
    clean_caches()
    return subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "run_all_offline.py")],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture(scope="session")
def evidence(offline_suite):
    """确保离线套件已跑过，再交还证据目录。"""
    assert offline_suite.returncode == 0, (
        "离线套件未通过，后续证据断言无意义：\n" + (offline_suite.stdout or "")[-4000:]
    )
    return OUTPUTS
