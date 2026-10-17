"""scripts/14_asr_quality_check.py —— ASR 质量评测的指标实现。

这些是纯函数（中文数字归一化 / 编辑距离 / CER），不依赖 GPU 或真实模型。
把它们钉住，避免以后「调整指标口径」时把结论悄悄改了。
"""

from __future__ import annotations

import importlib.util
import sys

import pytest
from conftest import PROJECT_ROOT


@pytest.fixture(scope="module")
def ev():
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    path = PROJECT_ROOT / "scripts" / "14_asr_quality_check.py"
    spec = importlib.util.spec_from_file_location("asr_eval_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("三百以内", "300以内"),
        ("一百五左右", "150左右"),
        ("五百元以下，谢谢。", "500元以下谢谢"),
        ("300以内", "300以内"),
    ],
)
def test_canon_normalizes_numerals_and_punctuation(ev, raw, expected):
    assert ev._canon(raw) == expected


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("kitten", "sitting", 3),
        ("", "abc", 3),
        ("abc", "", 3),
        ("abc", "abc", 0),
    ],
)
def test_levenshtein(ev, a, b, expected):
    assert ev._levenshtein(a, b) == expected


def test_cer_ignores_numeral_style_and_punctuation(ev):
    """中文数字与阿拉伯数字应视为等价；标点差异不计入错误。"""
    assert ev._cer("三百以内的水杯", "300以内的水杯") == 0.0
    assert ev._cer("这个不太行，换一个", "这个不太行换一个") == 0.0


def test_cer_counts_real_substitution(ev):
    # 2 字参考、1 字不同 -> 0.5
    assert ev._cer("红色", "蓝色") == 0.5


def test_cn_to_int_handles_colloquial_forms(ev):
    assert ev._cn_to_int("三百") == 300
    assert ev._cn_to_int("一百五") == 150
    assert ev._cn_to_int("十五") == 15
