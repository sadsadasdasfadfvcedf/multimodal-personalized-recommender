"""已购商品排除：任何来源都不允许把已购商品重新推荐给用户。

回归锁：离线快照的个性化分数里包含已购商品（如用户 14156 的 22699 分数 0.97），
recall 合并时曾豁免 personalized 来源的排除检查，导致「已购商品不再推荐」
（data/kb/06 与 recall.py 的承诺）被打破。
"""

from __future__ import annotations

import pytest

from src.contracts import make_event
from src.intent import understand
from src.orchestrator import Orchestrator
from src.ranking import rank
from src.recall import Candidate, recall
from src.session import SessionContext

DEMO_USERS = ("15547", "14156", "17231")


@pytest.fixture(scope="module")
def orch():
    return Orchestrator(mode="offline", with_voice=False)


def test_purchased_never_in_recommendations(orch):
    for uid in DEMO_USERS:
        purchased = orch.catalog.purchased(uid)
        assert purchased, f"用户 {uid} 没有已购记录，测试前提失效"
        event = make_event(user_id=uid, source="text", text="随便推荐一些")
        result = orch.handle(event.to_dict(), user_id=uid, session_id=f"purchased-probe-{uid}")
        assert result.ok, f"用户 {uid} 主链路失败: {result.error}"
        recs = result.data["recommendations"]
        assert recs, f"用户 {uid} 无推荐结果"
        leaked = [i for i in recs if i["item_id"] in purchased]
        assert not leaked, f"用户 {uid} 的已购商品被重新推荐: {leaked}"


def test_recall_merge_drops_purchased_for_every_source(orch):
    """召回合并层：personalized 来源同样要剔除已购（曾豁免该来源）。"""
    event = make_event(user_id="14156", source="text", text="随便推荐一些")
    request = understand(
        event,
        catalog_categories=orch.catalog.categories(),
        session_id="s-excl",
    ).data["request"]
    result = recall(request, orch.recommender, orch.catalog, SessionContext("s-excl", "14156"))
    assert result.ok and result.data["candidates"]
    purchased = orch.catalog.purchased("14156")
    leaked = [c for c in result.data["candidates"] if c.item_id in purchased]
    assert not leaked, f"召回候选里混入已购商品: {[c.item_id for c in leaked]}"


def test_recall_without_session_still_excludes_purchased(orch):
    """没有 session 也不能把已购商品带进候选池。"""
    event = make_event(user_id="14156", source="text", text="随便推荐一些")
    request = understand(
        event,
        catalog_categories=orch.catalog.categories(),
        session_id="s-excl-no-session",
    ).data["request"]
    result = recall(request, orch.recommender, orch.catalog)
    assert result.ok and result.data["candidates"]
    purchased = orch.catalog.purchased("14156")
    leaked = [c for c in result.data["candidates"] if c.item_id in purchased]
    assert not leaked, f"无 session 的召回候选里混入已购商品: {[c.item_id for c in leaked]}"


def test_rank_hard_filter_excludes_purchased_directly(orch):
    """排序入口的双重防线：直接调用 rank() 时硬约束过滤同样排除已购。"""
    event = make_event(user_id="14156", source="text", text="随便推荐一些")
    request = understand(
        event,
        catalog_categories=orch.catalog.categories(),
        session_id="s-rank",
    ).data["request"]
    purchased = orch.catalog.purchased("14156")
    purchased_id = next(iter(purchased))
    normal_id = next(item_id for item_id in orch.catalog.catalog if item_id not in purchased)
    rank_result = rank([Candidate(purchased_id), Candidate(normal_id)], request, orch.catalog)
    assert rank_result.ok
    ranked = rank_result.data["ranked"]
    leaked = [c for c in ranked if c.item_id in purchased]
    assert not leaked, f"排序结果里混入已购商品: {[c.item_id for c in leaked]}"
    # 淘汰明细里的排除原因必须与事实一致
    dropped = rank_result.data["trace"]["dropped_sample"]
    assert any(row["item_id"] == purchased_id for row in dropped), "淘汰明细里没有已购商品"
