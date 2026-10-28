"""颜色筛选回归：用户说「黑色」，结果里就不能出现白色商品。

根因链
------
1. 召回没有颜色来源：纯颜色筛选只能从个性化/热门候选里碰运气，
   目录里的黑色商品不在候选池时，硬约束过滤必然为空；
2. 空结果降级却先放宽颜色（保留类型），于是「黑色」返回一堆白色相框；
3. 提示只写「条件较窄」，用户看不出颜色被忽略。

修复后约束：
    · 颜色召回把标题命中颜色词的候选拉进候选池；
    · 硬约束为空时先保留颜色、放宽类目/类型；
    · 降级说明必须写清放宽了什么。
"""

from __future__ import annotations

import pytest

from src.contracts import make_event
from src.intent import understand
from src.orchestrator import Orchestrator
from src.recall import recall
from src.session import SessionContext

USER_ID = "14156"


@pytest.fixture(scope="module")
def orch():
    return Orchestrator(mode="offline", with_voice=False)


def _request(orch, text: str, session_id: str):
    event = make_event(user_id=USER_ID, source="text", text=text)
    result = understand(
        event,
        catalog_categories=orch.catalog.categories(),
        session_id=session_id,
    )
    assert result.ok, result.error
    return result.data["request"]


def _is_black(orch, item_id: str) -> bool:
    title = f"{orch.catalog.title(item_id)} {orch.catalog.product(item_id).get('english_title', '')}".lower()
    return any(word in title for word in ("黑", "black"))


def test_color_recall_supplies_black_candidates(orch):
    """召回层：颜色来源必须能把目录里的黑色商品带进候选池。"""
    request = _request(orch, "黑色", "s-color-recall")
    result = recall(
        request,
        orch.recommender,
        orch.catalog,
        SessionContext("s-color-recall", USER_ID),
    )
    assert result.ok and result.data["candidates"]
    black_ids = [c.item_id for c in result.data["candidates"] if _is_black(orch, c.item_id)]
    assert len(black_ids) >= 8, f"颜色召回只带回 {len(black_ids)} 件黑色商品"
    sources = {row["source"]: row for row in result.data["trace"]["sources"]}
    assert sources.get("color", {}).get("status") == "ok"


def test_pure_color_query_returns_only_black(orch):
    """主链路：只说「黑色」时，前 8 件必须全部是黑色商品。"""
    event = make_event(user_id=USER_ID, source="text", text="黑色")
    result = orch.handle(event.to_dict(), user_id=USER_ID, session_id="s-black-only")
    assert result.ok, result.error
    recs = result.data["recommendations"]
    assert len(recs) == 8
    offenders = [rec["title"] for rec in recs if not _is_black(orch, rec["item_id"])]
    assert not offenders, f"「黑色」返回了非黑色商品: {offenders}"


def test_color_survives_inherited_category(orch):
    """复用截图场景：先看办公用品，再说黑色。

    办公用品只有 3 件且没有黑色商品，严格匹配必然为空。降级时必须
    保留颜色、放宽类目，而不是保留类目、放宽颜色（后者会返回白/粉色
    办公用品，与用户输入相反）。
    """
    session_id = "s-office-then-black"
    first = make_event(user_id=USER_ID, source="text", text="只看办公用品")
    assert orch.handle(first.to_dict(), user_id=USER_ID, session_id=session_id).ok
    second = make_event(user_id=USER_ID, source="text", text="黑色")
    result = orch.handle(second.to_dict(), user_id=USER_ID, session_id=session_id)
    assert result.ok, result.error
    data = result.data
    recs = data["recommendations"]
    assert recs
    offenders = [rec["title"] for rec in recs if not _is_black(orch, rec["item_id"])]
    assert not offenders, f"降级后混入非黑色商品: {offenders}"

    trace = (data.get("trace") or {}).get("ranking") or {}
    assert trace.get("degraded") is True
    assert "保留颜色" in (trace.get("degrade_reason") or "")
    assert "保留颜色" in (trace.get("supplement_note") or "")
