"""会话结束汇总：统计必须在清空状态之前完成。"""

from __future__ import annotations

from src.session import SessionContext


def test_end_summary_counts_views_before_clear():
    session = SessionContext("s-end", "u-test")
    session.add_view("item-a", category="装饰品")
    session.add_view("item-a", category="装饰品")
    session.add_view("item-b", category="厨房用品")

    summary = session.end()

    assert summary["viewed_items"] == 2
    assert summary["viewed_categories"] == 2
    snapshot = session.snapshot()
    assert snapshot["viewed_items"] == {}
    assert snapshot["viewed_categories"] == {}
