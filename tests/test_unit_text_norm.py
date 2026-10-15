"""src/text_norm.py —— 中文规范化与流畅度检测。

其中 test_english_replacements_has_no_duplicate_key 是批次二 F601 修复的回归锁：
该字典曾在第 75 行与第 83 行重复定义 "gift"，重复键会让后一处静默覆盖前一处。
"""

from __future__ import annotations

from src import text_norm


def test_english_replacements_has_no_duplicate_key():
    import re
    from pathlib import Path

    import src.text_norm as mod

    src = Path(mod.__file__).read_text(encoding="utf-8")
    block = src.split("EN_REPLACEMENTS", 1)[1].split("}", 1)[0]
    keys = re.findall(r'"([A-Za-z_ ]+)"\s*:', block)
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    assert duplicates == [], f"EN_REPLACEMENTS 存在重复键（后定义会静默覆盖）：{duplicates}"


def test_gift_maps_to_liwu():
    assert text_norm.EN_REPLACEMENTS["gift"] == "礼物"


def test_find_latin_picks_two_letter_words():
    assert text_norm.find_latin("hello 你好 world") == ["hello", "world"]
    assert text_norm.find_latin("纯中文没有英文") == []


def test_find_traditional_detects_traditional():
    assert text_norm.find_traditional("這是繁體字") != []
    assert text_norm.find_traditional("这是简体字") == []


def test_normalize_fluency_is_idempotent_on_clean_text():
    clean = "这是一句规范的中文推荐理由。"
    out, actions = text_norm.normalize_fluency(clean)
    assert out == clean
    assert actions == []


def test_normalize_fluency_flags_unmapped_english():
    """has_too_much_english 作用在 normalize_fluency 的产物上（action=english_too_much），
    而不是 verify_fluency 的产物（action=latin_words）—— 两者属于不同门控阶段。"""
    _, actions = text_norm.normalize_fluency("hello world")
    assert text_norm.has_too_much_english(actions)


def test_normalize_fluency_replaces_mapped_english_word():
    _, actions = text_norm.normalize_fluency("ball 球")
    assert [a["action"] for a in actions] == ["english_replaced", "collapse_cjk_spaces"]
    assert not text_norm.has_too_much_english(actions)


def test_verify_fluency_flags_latin_words():
    actions = text_norm.verify_fluency("recommend gift for wedding budget")
    assert any(a["type"] == "latin_words" for a in actions)


def test_verify_fluency_accepts_clean_chinese():
    assert text_norm.verify_fluency("这款装饰品适合婚礼场景，价格实惠。") == []


def test_describe_shape():
    d = text_norm.describe()
    assert set(d) == {"traditional_markers", "converter", "english_map_size"}
    assert d["english_map_size"] == len(text_norm.EN_REPLACEMENTS)
    assert d["traditional_markers"] > 0
