"""src/intent.py —— 意图分类与槽位抽取。

test_contains_any_already_lowercases 是批次一一个「差点误判」的回归锁：
classify() 里曾出现过 `lowered = text.lower()` 定义了却不用，看上去像
「英文关键词按原文本匹配、大写输入匹配不上」的真 bug。实际 contains_any
内部已做小写化，所以那是纯冗余。这里把该结论钉死，避免以后有人「修」错方向。
"""

from __future__ import annotations

import pytest

from src import intent
from src.adapters.multimodal_adapter import MultimodalAdapter
from src.catalog import Catalog
from src.contracts import Slot, make_event


@pytest.fixture(scope="module")
def categories():
    return Catalog.from_snapshot().categories()


def _event(text=None, gesture=None, source="voice", modality="asr"):
    return make_event(user_id="u-test", source=source, modality=modality, text=text, gesture=gesture)


def _intent_of(event, categories):
    slots = intent.extract_slots(event, catalog_categories=categories)
    return intent.classify(event, slots, categories)[0], slots


@pytest.mark.parametrize(
    "text,expected",
    [
        ("推荐一些礼物", "discover"),
        ("500以内的结婚礼物", "filter"),
        ("为什么推荐第一个", "explain"),
        ("随便看看有什么", "discover"),
    ],
)
def test_classify_basic_intents(text, expected, categories):
    got, _ = _intent_of(_event(text), categories)
    assert got == expected


def test_gesture_thumbs_up_becomes_feedback(categories):
    """手势要经 parse_event 规范化（补 is_feedback/polarity）后才算反馈。"""
    result = MultimodalAdapter().parse_event(
        {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.95}},
        user_id="u-test",
    )
    assert result.ok
    got, slots = _intent_of(result.data, categories)
    assert got == "feedback"
    assert slots.get("polarity") is not None


@pytest.mark.parametrize(
    "text,slot_name,expected",
    [
        ("500以内的红色礼物", "budget_max", 500.0),
        ("500元以上的礼物", "budget_min", 500.0),
    ],
)
def test_extract_budget_slots(text, slot_name, expected, categories):
    _, slots = _intent_of(_event(text), categories)
    assert slot_name in slots, f"{text} 未抽出 {slot_name}：{sorted(slots)}"
    assert slots[slot_name].value == pytest.approx(expected)


def test_extract_color_and_category(categories):
    _, slots = _intent_of(_event("500以内的红色礼物"), categories)
    assert slots["colors"].value == ["红色"]
    assert slots["category"].value


def test_empty_text_yields_no_slots(categories):
    _, slots = _intent_of(_event("嗯"), categories)
    assert slots == {}


def test_budget_min_above_max_is_a_conflict():
    slots = {
        "budget_min": Slot(name="budget_min", value=1000.0),
        "budget_max": Slot(name="budget_max", value=500.0),
    }
    conflicts = intent.detect_conflicts(slots)
    assert [c["id"] for c in conflicts] == ["budget_range"]


def test_valid_budget_range_has_no_conflict():
    slots = {
        "budget_min": Slot(name="budget_min", value=100.0),
        "budget_max": Slot(name="budget_max", value=500.0),
    }
    assert intent.detect_conflicts(slots) == []


def test_understand_returns_request_intent_and_clarification(categories):
    result = intent.understand(_event("500以内的结婚礼物"), catalog_categories=categories)
    assert result.ok
    assert set(result.data) == {"request", "intent_result", "clarification"}
    assert result.data["intent_result"]["intent"] == "filter"


def test_english_keywords_match_regardless_of_case(categories):
    """英文关键词大小写不敏感 —— 依赖 contains_any 内部小写化。"""
    lower, _ = _intent_of(_event("recommend something"), categories)
    upper, _ = _intent_of(_event("RECOMMEND SOMETHING"), categories)
    assert lower == upper == "discover"
