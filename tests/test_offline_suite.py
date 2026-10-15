"""集成层：完整离线套件必须整体通过。

这是最外层的安全网 —— 只要它绿，说明 9 个阶段的端到端链路没坏。
"""

from __future__ import annotations

import re


def test_suite_exit_code_zero(offline_suite):
    assert offline_suite.returncode == 0, "run_all_offline.py 退出码非 0\n" + (offline_suite.stdout or "")[-4000:]


def test_all_nine_stages_pass(offline_suite):
    out = offline_suite.stdout or ""
    assert "合计 9/9 通过" in out, "未看到 9/9 通过汇总：\n" + out[-4000:]


def test_no_stage_reported_fail(offline_suite):
    out = offline_suite.stdout or ""
    assert "❌" not in out, "汇总表出现失败标记：\n" + out[-4000:]
    assert " FAIL " not in out.upper().replace("✅", "PASS")
    # 汇总表里每行的结果列只允许 PASS
    rows = re.findall(r"^\s*\d{2}\s+\S+\s+(✅|❌)\s", out, re.M)
    assert rows, "未解析到阶段汇总行"
    assert all(r == "✅" for r in rows), f"存在失败阶段：{rows}"


def test_stdout_reports_evidence_refreshed(offline_suite):
    out = offline_suite.stdout or ""
    assert "outputs/" in out
