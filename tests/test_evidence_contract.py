"""契约层：证据文件里的验收结论必须保持成立。

比「套件跑通了」更强一层 —— 直接断言写进 outputs/ 的验收数字，
避免出现「脚本没报错但验收结果悄悄退化」。
"""

from __future__ import annotations

import json

import pytest
from conftest import STAGE_OUTPUTS

ALL_ACCEPTANCE_OUTPUTS = [*STAGE_OUTPUTS.values(), "text_norm_check.json"]


def _read(evidence, name):
    return json.loads((evidence / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ALL_ACCEPTANCE_OUTPUTS)
def test_acceptance_checklist_all_true(evidence, name):
    """所有阶段的 acceptance_checklist 每一条都必须为 True。"""
    doc = _read(evidence, name)
    checklist = doc.get("acceptance_checklist")
    if checklist is None:
        pytest.skip(f"{name} 无 acceptance_checklist")
    assert checklist, f"{name} 的 acceptance_checklist 为空"
    failed = [k for k, v in checklist.items() if not v]
    assert not failed, f"{name} 存在未通过的验收项：{failed}"


@pytest.mark.parametrize("stage,name", sorted(STAGE_OUTPUTS.items()))
def test_stage_summary_has_no_failure(evidence, stage, name):
    """凡是有 summary 的阶段，failed 必须为 0。"""
    doc = _read(evidence, name)
    summary = doc.get("summary")
    if not summary:
        pytest.skip(f"阶段 {stage} 无 summary")
    assert summary.get("failed", 0) == 0, f"阶段 {stage} summary.failed={summary.get('failed')}"
    assert summary.get("passed", 0) == summary.get("total", -1), f"阶段 {stage} 未全通过"


def test_stage01_contract_counts(evidence):
    s = _read(evidence, "stage01_contract_report.json")["summary"]
    assert (s["total"], s["passed"], s["failed"]) == (25, 25, 0)
    assert s["verdict"] == "PASS"


def test_stage06_privacy_scan_clean(evidence):
    ps = _read(evidence, "stage06_acceptance.json")["privacy_scan"]
    assert ps["verdict"] == "PASS"
    assert ps["forbidden_files"] == []
    assert ps["sensitive_literals"] == []
    assert ps["absolute_path_literals"] == []


def test_stage06_has_three_case_types(evidence):
    types = _read(evidence, "stage06_acceptance.json")["scenario_types"]
    for key in ("成功", "边界", "失败"):
        assert types.get(key), f"缺少「{key}」类用例"


def test_stage05_has_success_and_no_change_cases(evidence):
    doc = _read(evidence, "stage05_feedback_ab.json")
    counts = {k: v.get("count", 0) for k, v in doc["case_type_summary"].items()}
    assert counts.get("成功", 0) > 0
    assert counts.get("无变化", 0) + counts.get("失败", 0) > 0, "缺少无变化/失败对照案例"


def test_stage04_evidence_rule_is_respected_per_status(evidence):
    """正常行必须有证据，负例行必须没有 —— 「无证据时不编造」正是验收点之一，
    所以不能一刀切地要求所有行 evidence_count > 0。"""
    rows = _read(evidence, "stage04_explanations.json")["explanation_evidence_table"]
    assert rows
    ok_rows = [r for r in rows if r["status"] == "ok"]
    neg_rows = [r for r in rows if r["status"] != "ok"]
    assert ok_rows, "缺少正常解释案例"
    assert neg_rows, "缺少无证据 / 未命中商品的反例"
    assert all(r["evidence_count"] > 0 for r in ok_rows), "正常解释缺少可核验证据"
    assert all((r["evidence_count"] or 0) == 0 for r in neg_rows), "反例不应带证据"


def test_text_norm_all_cases_pass(evidence):
    s = _read(evidence, "text_norm_check.json")["summary"]
    assert s["failed"] == 0
