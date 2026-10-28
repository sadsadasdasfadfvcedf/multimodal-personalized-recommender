"""开放问答路由：非购物类问句交给常驻 LLM，购物需求仍走推荐。

回归锁：
- 「什么是协同过滤？」曾因为没有命中推荐意图而返回“未识别”；
- 寒暄（你是谁）必须继续走固定的 chitchat，不能拉起 4B 模型；
- 「推荐一些 500 以内的礼物」这类购物需求不能被对话抢占。
"""

from __future__ import annotations

from src.errors import ErrorCode, fail, ok
from src.orchestrator import Orchestrator


class FakeChat:
    """记录调用，不加载真实模型。"""

    def __init__(self, reply: str = "协同过滤用用户行为找相似偏好。", error: bool = False) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[str] = []
        self.resets = 0

    def chat(self, text: str):
        self.calls.append(text)
        if self.error:
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "模型未启动")
        return ok(self.reply)

    def reset(self) -> None:
        self.resets += 1


class FakeWebChat(FakeChat):
    def chat(self, text: str):
        self.calls.append(text)
        return ok(
            {
                "text": "这是带来源的回答。[1]",
                "web_search": {
                    "mode": "search",
                    "query": text,
                    "count": 1,
                    "sources": [{"index": 1, "title": "来源", "url": "https://example.com"}],
                },
            }
        )


def _ask(text: str, chat: FakeChat, session_id: str = "chat-probe") -> dict:
    orch = Orchestrator(with_voice=False, chat_service=chat)
    return orch.handle({"source": "text", "text": text}, user_id="15547", session_id=session_id).data


def test_open_question_routes_to_chat():
    chat = FakeChat()
    d = _ask("什么是协同过滤？", chat)
    assert d["intent_result"]["intent"] == "chat"
    assert d["status"] == "ok"
    assert d["answer"] == chat.reply
    assert d["recommendations"] == []
    assert chat.calls == ["什么是协同过滤？"]


def test_general_tasks_are_not_hijacked_by_shopping_scene_words():
    for index, text in enumerate(("帮我写一句生日祝福", "今天天气怎么样")):
        chat = FakeChat()
        orch = Orchestrator(with_voice=False, chat_service=chat)
        session_id = f"general-task-probe-{index}"
        if text.startswith("今天"):
            orch.handle(
                {"source": "text", "text": "推荐一些厨房用品"},
                user_id="15547",
                session_id=session_id,
            )
        d = orch.handle(
            {"source": "text", "text": text},
            user_id="15547",
            session_id=session_id,
        ).data

        assert d["intent_result"]["intent"] == "chat", text
        assert chat.calls == [text], text


def test_chat_web_search_metadata_is_passed_through():
    chat = FakeWebChat()
    d = _ask("DeepSeek 最新版本是什么？", chat)
    assert d["intent_result"]["intent"] == "chat"
    assert d["answer"] == "这是带来源的回答。[1]"
    assert d["web_search"]["count"] == 1
    assert d["web_search"]["sources"][0]["url"] == "https://example.com"


def test_identity_question_stays_chitchat_without_loading_llm():
    chat = FakeChat()
    d = _ask("你是谁", chat)
    assert d["intent_result"]["intent"] == "chitchat"
    assert d["recommendations"] == []
    assert chat.calls == []


def test_product_request_still_recommends():
    chat = FakeChat()
    d = _ask("推荐一些500以内的礼物", chat)
    assert d["intent_result"]["intent"] != "chat"
    assert d["recommendations"]
    assert chat.calls == []


def test_price_question_still_recommends():
    chat = FakeChat()
    d = _ask("300以内的水杯多少钱", chat)
    assert d["intent_result"]["intent"] != "chat"
    assert chat.calls == []


def test_technical_follow_up_routes_to_chat():
    """「推荐」作为名词出现时不能被当成购物指令。"""
    chat = FakeChat()
    d = _ask("它和基于内容的推荐有什么区别？", chat)
    assert d["intent_result"]["intent"] == "chat"
    assert chat.calls


def test_recommendation_system_question_routes_to_chat():
    chat = FakeChat()
    d = _ask("推荐系统为什么需要召回？", chat)
    assert d["intent_result"]["intent"] == "chat"
    assert chat.calls


def test_reason_question_uses_focused_product():
    """点「继续问这件」后，为什么推荐它必须走 explain，而不是通用问答。"""
    for index, text in enumerate(("为什么推荐它", "为什么推荐他", "为什么推荐她", "为什么推荐", "为什么推荐呢")):
        chat = FakeChat()
        orch = Orchestrator(with_voice=False, chat_service=chat)
        session_id = f"focus-explain-probe-{index}"
        first = orch.handle(
            {"source": "text", "text": "推荐一些厨房用品"},
            user_id="15547",
            session_id=session_id,
        ).data
        target = first["recommendations"][0]["item_id"]

        d = orch.handle(
            {"source": "voice", "text": text},
            user_id="15547",
            session_id=session_id,
        ).data

        assert d["intent_result"]["intent"] == "explain", text
        assert d["recommendations"][0]["item_id"] == target, text
        assert "targets" not in d["intent_result"]["slots"], text
        assert chat.calls == [], text


def test_similar_followup_uses_focused_product_without_pronoun():
    for index, text in enumerate(("推荐一些相似的", "推荐一些类似的物品", "还有别的颜色吗")):
        chat = FakeChat()
        orch = Orchestrator(with_voice=False, chat_service=chat)
        session_id = f"focus-similar-probe-{index}"
        first = orch.handle(
            {"source": "text", "text": "推荐一些厨房用品"},
            user_id="15547",
            session_id=session_id,
        ).data
        target = first["recommendations"][0]["item_id"]

        d = orch.handle(
            {"source": "voice", "text": text},
            user_id="15547",
            session_id=session_id,
        ).data

        assert d["status"] != "clarification", text
        assert d["intent_result"]["slots"]["target"]["value"] == target, text
        assert "targets" not in d["intent_result"]["slots"], text
        assert chat.calls == [], text


def test_browsing_another_product_keeps_that_product_as_focus():
    orch = Orchestrator(with_voice=False)
    session_id = "browse-focus-probe"
    first = orch.handle(
        {"source": "text", "text": "推荐一些厨房用品"},
        user_id="15547",
        session_id=session_id,
    ).data
    selected = first["recommendations"][1]["item_id"]

    orch.handle(
        {"source": "browse", "item_id": selected},
        user_id="15547",
        session_id=session_id,
    )
    session = orch.store.get_or_create(session_id, "15547")
    assert session.focus_item_id == selected

    d = orch.handle(
        {"source": "voice", "text": "推荐类似的商品"},
        user_id="15547",
        session_id=session_id,
    ).data
    assert d["intent_result"]["intent"] == "similar"
    assert d["intent_result"]["slots"]["target"]["value"] == selected
    first_similar_ids = [item["item_id"] for item in d["recommendations"]]
    assert selected not in first_similar_ids
    assert orch.store.get_or_create(session_id, "15547").focus_item_id == selected

    another = next(item["item_id"] for item in d["recommendations"] if item["item_id"] != selected)
    orch.handle(
        {"source": "browse", "item_id": another},
        user_id="15547",
        session_id=session_id,
    )
    d2 = orch.handle(
        {"source": "voice", "text": "推荐类似的商品"},
        user_id="15547",
        session_id=session_id,
    ).data
    second_similar_ids = [item["item_id"] for item in d2["recommendations"]]
    assert d2["intent_result"]["slots"]["target"]["value"] == another
    assert another not in second_similar_ids
    assert first_similar_ids != second_similar_ids
    assert orch.store.get_or_create(session_id, "15547").focus_item_id == another


def test_new_product_focus_clears_previous_search_filters():
    orch = Orchestrator(with_voice=False)
    session_id = "product-context-reset-probe"
    first = orch.handle(
        {"source": "text", "text": "只看心形装饰品"},
        user_id="15547",
        session_id=session_id,
    ).data
    assert first["effective_request"]["slots"]["category"]["value"] == "装饰品"
    assert first["effective_request"]["slots"]["theme"]["value"] == "心形"

    mug = "21870"
    assert orch.catalog.category_of(mug) == "厨房用品"
    orch.handle(
        {"source": "browse", "item_id": mug},
        user_id="15547",
        session_id=session_id,
    )
    d = orch.handle(
        {"source": "voice", "text": "推荐相似的商品"},
        user_id="15547",
        session_id=session_id,
    ).data

    assert d["merge_report"]["product_context_reset"] is True
    assert set(d["effective_request"]["slots"]) == {"target"}
    assert d["effective_request"]["slots"]["target"]["value"] == mug
    assert mug not in [item["item_id"] for item in d["recommendations"]]


def test_similar_refresh_returns_a_different_batch():
    orch = Orchestrator(with_voice=False)
    session_id = "similar-refresh-probe"
    orch.handle(
        {"source": "text", "text": "只看心形装饰品"},
        user_id="15547",
        session_id=session_id,
    )
    target = "22335"
    orch.handle(
        {"source": "browse", "item_id": target},
        user_id="15547",
        session_id=session_id,
    )
    first = orch.handle(
        {"source": "voice", "text": "推荐相似的商品"},
        user_id="15547",
        session_id=session_id,
    ).data
    first_ids = [item["item_id"] for item in first["recommendations"]]

    refreshed = orch.handle(
        {"source": "voice", "text": "换一批相似商品"},
        user_id="15547",
        session_id=session_id,
    ).data
    second_ids = [item["item_id"] for item in refreshed["recommendations"]]

    assert refreshed["intent_result"]["intent"] == "similar"
    assert refreshed["intent_result"]["slots"]["target"]["value"] == target
    assert first_ids != second_ids
    assert not set(first_ids) & set(second_ids)


def test_shopping_phrases_are_not_chat():
    for text in ["有什么好推荐", "帮我推荐点东西", "推荐一下有什么好东西"]:
        chat = FakeChat()
        d = _ask(text, chat)
        assert d["intent_result"]["intent"] != "chat", text
        assert chat.calls == []


def test_chat_failure_degrades_but_keeps_intent():
    chat = FakeChat(error=True)
    d = _ask("为什么需要特征工程？", chat)
    assert d["intent_result"]["intent"] == "chat"
    assert d["status"] == "degraded"
    assert d["recommendations"] == []
    assert d["answer"]
    assert "chat_error" in d


def test_vision_question_does_not_go_to_chat():
    """视觉问句必须走 VL 标签链路，不能被纯文本模型截胡。"""
    chat = FakeChat()
    orch = Orchestrator(with_voice=False, chat_service=chat)
    d = orch.handle(
        {
            "source": "vision",
            "text": "画面里有什么？",
            "visual_tags": [{"tag": "白色马克杯", "confidence": 0.85}],
        },
        user_id="15547",
        session_id="vision-chat-probe",
    ).data
    assert d["intent_result"]["intent"] != "chat"
    assert chat.calls == []


def test_chat_output_is_normalized_to_simplified():
    import pytest

    pytest.importorskip("zhconv")
    from src.llm_chat import _to_simplified

    out = _to_simplified("這個推薦系統會分析你的行為。")
    assert "這" not in out and "推薦" not in out
    assert "这个推荐系统" in out


def test_offline_mode_does_not_create_camera_service():
    """默认离线启动不能创建摄像头直播服务。"""
    orch = Orchestrator(mode="offline", with_voice=False)
    assert orch.camera is None


def test_offline_mode_does_not_create_chat_service():
    """默认离线启动不加载任何重型模型。"""
    orch = Orchestrator(mode="offline", with_voice=False)
    assert orch.chat is None


def test_should_chat_is_pure_heuristic():
    assert Orchestrator._should_chat("什么是协同过滤？")
    assert Orchestrator._should_chat("帮我写一段品牌故事")
    assert Orchestrator._should_chat("它和基于内容的推荐有什么区别？")
    assert Orchestrator._should_chat("推荐系统为什么需要召回？")
    assert Orchestrator._should_chat("DeepSeek V4.1 Flash 最新版本是什么？")
    assert not Orchestrator._should_chat("推荐一些500以内的礼物")
    assert not Orchestrator._should_chat("300以内的水杯多少钱")
    assert not Orchestrator._should_chat("有什么好推荐")
    assert not Orchestrator._should_chat("随便看看")
    assert not Orchestrator._should_chat("")
    assert not Orchestrator._should_chat("为什么推荐它", focus_item_id="20829")
    assert not Orchestrator._should_chat("它是什么颜色", focus_item_id="20829")
    assert Orchestrator._should_chat("它和基于内容的推荐有什么区别？", focus_item_id="20829")
