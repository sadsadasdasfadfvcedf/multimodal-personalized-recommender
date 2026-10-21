"""语音 + 识图融合与具体商品类型召回。"""

from __future__ import annotations

import importlib.util

from conftest import PROJECT_ROOT

from src.errors import ok
from src.orchestrator import Orchestrator


def _webui():
    spec = importlib.util.spec_from_file_location("webui_voice_under_test", PROJECT_ROOT / "webui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_teapot_query_recalls_exact_product_type():
    orch = Orchestrator(mode="offline", with_voice=False)
    result = orch.handle(
        {"source": "voice", "text": "嗯给我推荐一些茶壶"},
        user_id="14156",
        session_id="voice-teapot",
    )

    assert result.ok
    data = result.data
    assert data["intent_result"]["slots"]["item_type"]["value"] == "茶壶"
    assert data["recommendations"]
    assert len(data["recommendations"]) == 8
    assert "茶壶" in data["recommendations"][0]["title"]
    assert data["trace"]["ranking"]["strict_count"] > 0
    assert data["trace"]["ranking"]["supplemented_count"] > 0
    assert "茶壶推荐" in data["presentation"]["text"]


def test_voice_visual_phrase_merges_asr_with_camera_frame():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.record_audio = lambda *args, **kwargs: ok({"recorded": True})

    class FakeAsr:
        def transcribe(self, *args, **kwargs):
            return ok({"text": "推荐我手上相关的物品"})

    app.orch.asr = FakeAsr()
    called = {}

    def fake_vision(question, top_k=None):
        called["question"] = question
        called["top_k"] = top_k
        return {
            "ok": True,
            "description": "白色陶瓷茶杯",
            "vision_latency_ms": 800,
            "response": {
                "status": "ok",
                "recommendations": [],
                "intent_result": {"intent": "discover", "slots": {}},
                "event": {"source": "vision", "modality": "visual"},
                "trace": {"modes": {"vision": "dashscope"}},
            },
        }

    app.vision = fake_vision
    result = app.voice(4, top_k=8)

    assert result["ok"] is True
    assert result["transcript"] == "推荐我手上相关的物品"
    assert result["input_modes"] == ["voice", "vision"]
    assert result["description"] == "白色陶瓷茶杯"
    assert called == {"question": "推荐我手上相关的物品", "top_k": 8}
    assert result["response"]["event"]["source"] == "voice"
    assert result["response"]["event"]["modality"] == "multimodal"
    assert result["response"]["trace"]["modes"]["asr"] == "real"


def test_voice_regular_shopping_phrase_stays_text_only():
    w = _webui()
    app = w.App("14156", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.record_audio = lambda *args, **kwargs: ok({"recorded": True})

    class FakeAsr:
        def transcribe(self, *args, **kwargs):
            return ok({"text": "嗯给我推荐一些茶壶"})

    app.orch.asr = FakeAsr()
    app.vision = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应抓取摄像头"))
    called = {}

    def fake_handle(spec, top_k=None):
        called["spec"] = spec
        called["top_k"] = top_k
        return {"ok": True, "response": {"status": "ok", "recommendations": []}}

    app.handle_spec = fake_handle
    result = app.voice(4, top_k=8)

    assert result["input_modes"] == ["voice"]
    assert called["spec"] == {"source": "voice", "text": "嗯给我推荐一些茶壶"}


def test_more_padding_preserves_category_and_product_type():
    w = _webui()
    app = w.App("14156", "offline", False, False, None)

    by_category = app.more("只看厨房用品", 24)
    assert by_category["ok"] is True
    assert by_category["items"]
    assert {item["category"] for item in by_category["items"]} == {"厨房用品"}

    by_type = app.more("推荐一些茶壶", 24)
    assert by_type["ok"] is True
    assert by_type["items"]
    assert "茶壶" in by_type["items"][0]["title"]
    assert any(item["evidence"].get("match_scope") == "broadened" for item in by_type["items"])


def test_similar_preview_is_automatic_and_does_not_pollute_main_session():
    w = _webui()
    app = w.App("14156", "offline", False, False, None)
    main = app.orch.store.get_or_create(app.session_id, app.user_id)
    assert main.focus_item_id is None
    seed = app.home(8)["items"][0]["item_id"]

    result = app.similar_preview(seed, top_k=4)

    assert result["ok"] is True
    assert result["seed_id"] == seed
    assert result["items"]
    assert len(result["items"]) <= 4
    assert all(item["img_url"].startswith("/api/img/") for item in result["items"])
    current = app.orch.store.get_or_create(app.session_id, app.user_id)
    assert current.focus_item_id == main.focus_item_id


def test_visual_backpack_fills_results_with_related_products():
    orch = Orchestrator(mode="offline", with_voice=False)
    data = orch.handle(
        {
            "source": "vision",
            "text": "我手上推荐一下我手上的物品",
            "visual_tags": [{"tag": "黑色背包", "confidence": 0.9}],
        },
        user_id="15547",
        session_id="visual-backpack-fill",
    ).data

    assert data["intent_result"]["slots"]["item_type"]["value"] == "背包"
    assert len(data["recommendations"]) == 8
    assert "背包" in data["recommendations"][0]["title"]
    assert data["trace"]["ranking"]["supplemented_count"] > 0
    assert "补充" in data["trace"]["ranking"]["supplement_note"]


def test_generic_budget_request_resets_previous_visual_filters():
    orch = Orchestrator(mode="offline", with_voice=False)
    session_id = "generic-budget-reset"
    orch.handle(
        {
            "source": "vision",
            "text": "我手上推荐一下我手上的物品",
            "visual_tags": [{"tag": "黑色背包", "confidence": 0.9}],
        },
        user_id="15547",
        session_id=session_id,
    )
    data = orch.handle(
        {"source": "voice", "text": "推荐一下五百以内的物品"},
        user_id="15547",
        session_id=session_id,
    ).data

    assert data["merge_report"]["context_reset"] is True
    assert set(data["effective_request"]["slots"]) == {"budget_max", "budget_raw"}
    assert len(data["recommendations"]) == 8
    assert len({item["category"] for item in data["recommendations"]}) > 1
