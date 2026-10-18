"""寒暄 / 身份问答：直接回答，不进入推荐链路。

回归锁：「你是谁」曾被当成 discover，返回 8 件商品。
但「你好，推荐一些礼物」这类正常需求不能被误判成寒暄。
"""

from __future__ import annotations

import pytest

from src.orchestrator import Orchestrator


def _ask(text: str) -> dict:
    orch = Orchestrator(with_voice=False)
    return orch.handle({"source": "text", "text": text}, user_id="15547", session_id="smalltalk-probe").data


def test_identity_question_is_answered_without_products():
    d = _ask("你是谁")
    assert d["status"] == "ok"
    assert d["intent_result"]["intent"] == "chitchat"
    assert d["recommendations"] == []
    assert "推荐助手" in d["answer"]


def test_capability_question_lists_features():
    d = _ask("你能做什么")
    assert d["intent_result"]["intent"] == "chitchat"
    assert "筛选" in d["answer"] and "对比" in d["answer"]


@pytest.mark.parametrize("text", ["你好", "您好", "谢谢", "help"])
def test_short_pleasantries_are_chitchat(text):
    assert _ask(text)["intent_result"]["intent"] == "chitchat"


def test_greeting_with_request_still_recommends():
    """「你好」+ 正常需求不能被寒暄短路吞掉。"""
    d = _ask("你好，推荐一些礼物")
    assert d["intent_result"]["intent"] != "chitchat"
    assert d["recommendations"]
