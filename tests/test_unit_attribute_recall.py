"""属性召回回归：材质 / 主题 / 场景 / 备用类目。

背景
----
颜色召回修好后，其余「标题可核验」的属性仍然没有召回来源：
    · 说「圣诞装饰」，候选池来自个性化/热门，圣诞节商品进不了 TopK；
    · 说「玻璃花瓶」，材质硬约束只命中 1 件，补足时混入毛毡花瓶；
    · 说「生日礼物」，只召回主类目（派对用品），备用类目（装饰品）缺失。

同时锁定英文匹配的词边界：`wood` 不能命中 `WOODLAND`，否则「木质相框」
会混入「林地夏洛特包」。
"""

from __future__ import annotations

import pytest

from config import settings
from src.contracts import make_event
from src.intent import understand
from src.orchestrator import Orchestrator
from src.recall import recall
from src.session import SessionContext
from src.utils import title_has_term

USER_ID = "14156"


@pytest.fixture(scope="module")
def orch():
    return Orchestrator(mode="offline", with_voice=False)


def _run(orch, text: str, session_id: str):
    event = make_event(user_id=USER_ID, source="text", text=text)
    result = orch.handle(event.to_dict(), user_id=USER_ID, session_id=session_id)
    assert result.ok, result.error
    return result.data


def _titles(orch, data):
    return [item["title"] for item in data["recommendations"]]


def _matches(orch, data, field: str) -> list[bool]:
    slots = (data.get("effective_request") or {}).get("slots") or {}
    words = {
        "material": settings.SLOT_LEXICON["materials"],
        "theme": settings.SLOT_LEXICON["themes"],
        "scene": settings.SLOT_LEXICON["scenes"],
    }[field]
    name = (slots.get(field) or {}).get("value")
    if field == "scene":
        terms = [word for word, scene in words.items() if scene == name] + [name]
    else:
        terms = list(words.get(name, [name]))
    out = []
    for item in data["recommendations"]:
        title = f"{item['title']} {orch.catalog.product(item['item_id']).get('english_title', '')}".lower()
        out.append(any(title_has_term(title, term) for term in terms))
    return out


def test_scene_recall_prioritizes_verified_scene_items(orch):
    data = _run(orch, "圣诞装饰", "s-scene-recall")
    assert all(_matches(orch, data, "scene")), _titles(orch, data)


def test_material_recall_keeps_material_in_supplements(orch):
    data = _run(orch, "玻璃花瓶", "s-material-recall")
    assert all(_matches(orch, data, "material")), _titles(orch, data)


def test_theme_recall_keeps_theme_in_supplements(orch):
    data = _run(orch, "复古相框", "s-theme-recall")
    assert all(_matches(orch, data, "theme")), _titles(orch, data)


def test_category_alt_is_recalled(orch):
    """「生日礼物」的备用类目（装饰品）也要进候选池。"""
    event = make_event(user_id=USER_ID, source="text", text="生日礼物 500以内")
    request = understand(
        event,
        catalog_categories=orch.catalog.categories(),
        session_id="s-alt",
    ).data["request"]
    alt = request.slot_value("category_alt") or []
    assert alt, "测试前提：该查询应解析出备用类目"
    result = recall(request, orch.recommender, orch.catalog, SessionContext("s-alt", USER_ID))
    assert result.ok
    alt_ids = {cand.item_id for cand in result.data["candidates"] if orch.catalog.category_of(cand.item_id) in alt}
    assert alt_ids, f"备用类目 {alt} 没有任何候选"


def test_title_has_term_uses_english_word_boundary():
    assert title_has_term("woodland charlotte bag", "wood") is False
    assert title_has_term("solid wood frame", "wood") is True
    assert title_has_term("wooden picture frame", "wood") is False  # wooden 是独立词条
    assert title_has_term("wooden picture frame", "wooden") is True
    assert title_has_term("glass stars", "star") is True
    assert title_has_term("glass starfish", "star") is False
    assert title_has_term("happy season", "sea") is False
    assert title_has_term("白色木质相框", "相框") is True
