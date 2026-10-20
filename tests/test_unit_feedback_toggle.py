"""反馈取消：再次点击同一 👍/👎 应撤销反馈并恢复推荐。"""

from __future__ import annotations

from src.orchestrator import Orchestrator
from src.session import SessionContext


def test_clear_feedback_restores_disliked_item():
    orch = Orchestrator(mode="offline", with_voice=False)
    user_id = "15547"
    session_id = "toggle-dislike"
    first = orch.handle(
        {"source": "text", "text": "500以内的婚礼装饰品"},
        user_id=user_id,
        session_id=session_id,
    )
    target = first.data["recommendations"][0]["item_id"]

    disliked = orch.handle(
        {"source": "gesture", "gesture": {"name": "thumbs_down", "confidence": 0.99}},
        user_id=user_id,
        session_id=session_id,
    )
    assert target not in [r["item_id"] for r in disliked.data["recommendations"]]
    assert disliked.data["feedback"]["dislikes"][target] == 1

    cleared = orch.clear_feedback(target, user_id=user_id, session_id=session_id, top_k=8)

    assert cleared.ok
    data = cleared.data
    assert data["feedback"]["polarity"] == "none"
    assert data["feedback"]["cleared"] is True
    assert data["feedback"]["likes"] == {}
    assert data["feedback"]["dislikes"] == {}
    assert target in [r["item_id"] for r in data["recommendations"]]


def test_clear_feedback_removes_positive_feedback():
    orch = Orchestrator(mode="offline", with_voice=False)
    user_id = "14156"
    session_id = "toggle-like"
    first = orch.handle(
        {"source": "text", "text": "推荐一些厨房用品"},
        user_id=user_id,
        session_id=session_id,
    )
    target = first.data["recommendations"][0]["item_id"]
    liked = orch.handle(
        {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.99}},
        user_id=user_id,
        session_id=session_id,
    )
    assert liked.data["feedback"]["likes"][target] == 1

    cleared = orch.clear_feedback(target, user_id=user_id, session_id=session_id, top_k=8)

    assert cleared.ok
    assert cleared.data["feedback"]["likes"] == {}
    assert cleared.data["feedback"]["dislikes"] == {}


def test_remove_feedback_also_decrements_category_preference():
    session = SessionContext("s-clear", "u-test")
    session.add_feedback("item-a", "positive", category="装饰品")
    session.add_feedback("item-b", "negative", category="厨房用品")

    session.remove_feedback("item-a", category="装饰品")
    session.remove_feedback("item-b", category="厨房用品")

    assert session.likes == {}
    assert session.dislikes == {}
    assert session.liked_categories == {}
    assert session.disliked_categories == {}
