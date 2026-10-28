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


def test_visual_tag_can_supply_object_category(categories):
    """举物品识图：没有文字时，视觉标签也要能提供品类与颜色。"""
    event = make_event(
        user_id="u-test",
        source="vision",
        modality="visual",
        visual_tags=[{"tag": "一个白色陶瓷杯", "confidence": 0.9}],
    )
    slots = intent.extract_slots(event, catalog_categories=categories)
    assert slots["category"].value == "厨房用品"
    assert slots["colors"].value == ["白色"]
    assert intent.classify(event, slots, categories)[0] == "discover"


def test_visual_tag_maps_glasses_to_accessories(categories):
    """「举起眼镜」这类常见物品也必须能映射到目录品类，不能只识别不推荐。"""
    event = make_event(
        user_id="u-test",
        source="vision",
        modality="visual",
        visual_tags=[{"tag": "一副黑色眼镜", "confidence": 0.9}],
    )
    slots = intent.extract_slots(event, catalog_categories=categories)
    assert slots["category"].value == "饰品"
    assert slots["colors"].value == ["黑色"]


def test_visual_tag_maps_monitor_to_office_supplies(categories):
    event = make_event(
        user_id="u-test",
        source="vision",
        modality="visual",
        visual_tags=[{"tag": "黑色电脑显示器", "confidence": 0.88}],
    )
    slots = intent.extract_slots(event, catalog_categories=categories)
    assert slots["category"].value == "办公用品"
    assert slots["colors"].value == ["黑色"]


def test_visual_tag_maps_skateboard_to_toys(categories):
    event = make_event(
        user_id="u-test",
        source="vision",
        modality="visual",
        visual_tags=[{"tag": "一块滑板", "confidence": 0.86}],
    )
    slots = intent.extract_slots(event, catalog_categories=categories)
    assert slots["category"].value == "玩具"


@pytest.mark.parametrize(
    "tag,expected_category,expected_type",
    [
        ("蓝色金属标牌", "家居装饰", "标志牌"),
        ("黑色雨伞", "生活用品", "雨伞"),
    ],
)
def test_visual_tag_maps_common_catalog_objects(tag, expected_category, expected_type, categories):
    event = make_event(
        user_id="u-test",
        source="vision",
        modality="visual",
        visual_tags=[{"tag": tag, "confidence": 0.9}],
    )
    slots = intent.extract_slots(event, catalog_categories=categories)
    assert slots["category"].value == expected_category
    assert slots["item_type"].value == expected_type


def test_teapot_extracts_specific_product_type(categories):
    event = _event("嗯给我推荐一些茶壶")
    got, slots = _intent_of(event, categories)

    assert got == "discover"
    assert slots["category"].value == "厨房用品"
    assert slots["item_type"].value == "茶壶"
    assert "茶壶" in slots["item_type_terms"].value
    constraints = intent.build_constraints(slots, event.text or "", got)
    title_type = next(c for c in constraints if c.field == "title_type")
    assert title_type.kind == "hard"
    assert "茶壶" in title_type.value


def test_theme_and_material_are_extracted_from_title_lexicon(categories):
    event = _event("只看复古金属装饰品")
    got, slots = _intent_of(event, categories)

    assert got == "filter"
    assert slots["category"].value == "装饰品"
    assert slots["theme"].value == "复古"
    assert slots["material"].value == "金属"

    constraints = intent.build_constraints(slots, event.text or "", got)
    theme = next(c for c in constraints if c.field == "title_theme")
    material = next(c for c in constraints if c.field == "title_material")
    assert theme.kind == "hard"
    assert material.kind == "hard"
    assert {"复古", "vintage", "retro"} & set(theme.value)
    assert {"金属", "metal"} & set(material.value)


@pytest.mark.parametrize(
    "text,expected_index",
    [
        ("第一个", 1),
        ("第二个", 2),
        ("第十个", 10),
        ("第十五个", 15),
        ("第二十个", 20),
        ("第二十一个", 21),
    ],
)
def test_resolve_ordinal_target_chinese_numerals(text, expected_index):
    session_items = [f"item-{i}" for i in range(1, 31)]
    assert intent._resolve_ordinal_target(text, session_items) == session_items[expected_index - 1]


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


def test_category_scene_conflict_is_detected(categories):
    """回归锁：这条冲突检测曾把 Slot 对象直接与字符串比较（`scene in ("厨房",)`），
    条件恒为 False，导致 category_scene 冲突实际上从未触发过 —— 属于「功能静默失效」。
    正确写法是比对 scene.value。"""
    _, slots = _intent_of(_event("厨房用的装饰品"), categories)
    conflict_ids = [c["id"] for c in intent.detect_conflicts(slots)]
    assert "category_scene" in conflict_ids, f"未触发冲突，实际={conflict_ids}"


def test_category_scene_conflict_does_not_over_fire(categories):
    """修复后不得产生误报。"""
    for text in ("客厅的装饰品", "厨房的锅"):
        _, slots = _intent_of(_event(text), categories)
        conflict_ids = [c["id"] for c in intent.detect_conflicts(slots)]
        assert "category_scene" not in conflict_ids, f"{text} 误报冲突：{conflict_ids}"


# ------------------------------------------------------------
# 中文数字预算（三百以内 / 五百元以下 / 一百五左右）
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("三百以内的水杯", 300.0),
        ("五百元以下的厨房用品", 500.0),
        ("一百二十以内", 120.0),
        ("两千五左右", 2500.0 * 1.15),
    ],
)
def test_extract_chinese_numeral_budget(text, expected, categories):
    """「三百以内」这种写法 ASR 与手输都会出现，此前整句解析不出预算。"""
    _, slots = _intent_of(_event(text), categories)
    assert slots["budget_max"].value == pytest.approx(expected, rel=1e-3)


def test_chinese_numeral_around_gives_a_range(categories):
    _, slots = _intent_of(_event("一百五左右的杯子"), categories)
    assert slots["budget_max"].value == pytest.approx(172.5, rel=1e-3)
    assert slots["budget_min"].value == pytest.approx(127.5, rel=1e-3)


def test_chinese_numeral_does_not_steal_distant_direction_word(categories):
    """回归锁：「推荐一些两百块以内的装饰」曾产出 price<=1 ——
    「一」（来自「一些」）借到了 ±6 字窗口里远处的「以内」。
    正确答案是 200，且只应有一个预算上限。"""
    _, slots = _intent_of(_event("推荐一些两百块以内的装饰"), categories)
    assert slots["budget_max"].value == pytest.approx(200.0)


@pytest.mark.parametrize("text", ["十分好看的水杯", "一起看看有什么", "三百"])
def test_word_containing_numeral_char_is_not_a_budget(text, categories):
    """没有紧邻的方向词/计量单位时，数词不得被当成预算。"""
    _, slots = _intent_of(_event(text), categories)
    assert "budget_max" not in slots
    assert "budget_min" not in slots


# ------------------------------------------------------------
# 适配器：输入侧归一化在进入意图解析之前完成
# ------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,normalized,expected_budget",
    [
        ("预算是内300的水杯", "预算以内300的水杯", 300.0),
        ("300以內的水杯", "300以内的水杯", 300.0),
        ("３００以内的水杯", "300以内的水杯", 300.0),
        ("3 0 0 以内", "300 以内", 300.0),
    ],
)
def test_adapter_normalizes_input_before_intent(raw, normalized, expected_budget, categories):
    adapter = MultimodalAdapter()
    result = adapter.parse_event({"source": "voice", "text": raw}, user_id="u-test")
    assert result.ok
    assert result.data.text == normalized
    # 原文必须保留，否则「报告数字可追溯」无从谈起
    assert result.data.raw["spec"]["text"] == raw
    assert result.data.raw.get("text_norm"), "归一化处置明细应写入 raw 以便追溯"

    slots = intent.extract_slots(result.data, catalog_categories=categories)
    assert slots["budget_max"].value == pytest.approx(expected_budget)


def test_adapter_leaves_clean_input_without_norm_record():
    adapter = MultimodalAdapter()
    result = adapter.parse_event({"source": "voice", "text": "300以内的水杯"}, user_id="u-test")
    assert result.ok
    assert result.data.text == "300以内的水杯"
    assert "text_norm" not in result.data.raw
