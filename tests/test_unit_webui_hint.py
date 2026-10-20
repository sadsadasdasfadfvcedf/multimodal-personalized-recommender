"""webui.py —— 依赖错误的可操作提示。

浏览器里只会看到一段 error 文本；如果它是
"'MessageFactory' object has no attribute 'GetPrototype'"，没人知道要降 protobuf。
这里把「错误签名 -> 修复建议」钉住。
"""

from __future__ import annotations

import importlib.util
import time
from pathlib import Path

from conftest import PROJECT_ROOT

from src.errors import ok


def _webui():
    spec = importlib.util.spec_from_file_location("webui_under_test", PROJECT_ROOT / "webui.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_hint_for_protobuf_error():
    hint = _webui()._dep_hint("'MessageFactory' object has no attribute 'GetPrototype'")
    assert hint and "protobuf" in hint


def test_hint_for_torchvision_nms_error():
    hint = _webui()._dep_hint("RuntimeError: operator torchvision::nms does not exist")
    assert hint and "torchvision" in hint


def test_hint_for_auto_processor_error():
    hint = _webui()._dep_hint("ModuleNotFoundError: Could not import module 'AutoProcessor'")
    assert hint and "torchvision" in hint


def test_hint_is_none_for_unrelated_error():
    assert _webui()._dep_hint("Connection refused") is None


def test_err_helper_includes_protocol_fields():
    """docs/protocol.md §8：任何失败都要带 code + message + field。"""
    w = _webui()
    payload = w._err(w.ErrorCode.INVALID_INPUT, "bad input", field="text", hint="hint", extra="x")
    assert payload["ok"] is False
    assert payload["error"] == "bad input"
    assert payload["code"] == "E_INVALID_INPUT"
    assert payload["field"] == "text"
    assert payload["hint"] == "hint"
    assert payload["extra"] == "x"


def test_only_thumbs_are_feedback_gestures():
    w = _webui()
    assert w._is_feedback_gesture("thumbs_up")
    assert w._is_feedback_gesture("thumbs_down")
    assert not w._is_feedback_gesture("victory")
    assert not w._is_feedback_gesture("open_palm")
    assert not w._is_feedback_gesture("index_up")
    assert not w._is_feedback_gesture("index_down")
    assert not w._is_feedback_gesture("unknown_pose")


def test_unsupported_gesture_does_not_write_feedback():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.capture_burst = lambda *args, **kwargs: ok({"dir": "burst"})
    app.orch.multimodal.recognize_gesture_batch = lambda *args, **kwargs: ok(
        {
            "results": [
                {
                    "image": "frame.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "victory", "confidence": 0.99}],
                },
                {
                    "image": "frame2.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "victory", "confidence": 0.97}],
                },
                {
                    "image": "frame3.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "victory", "confidence": 0.96}],
                },
            ]
        }
    )
    called: list[dict] = []
    app.handle_spec = lambda spec, **kwargs: called.append(spec) or {"ok": True}

    result = app.gesture(None, 8)

    assert result["ok"] is False
    assert result["code"] == "E_INVALID_INPUT"
    assert result["gesture"] == "victory"
    assert called == []


def test_thumbs_up_still_writes_feedback():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.capture_burst = lambda *args, **kwargs: ok({"dir": "burst"})
    app.orch.multimodal.recognize_gesture_batch = lambda *args, **kwargs: ok(
        {
            "results": [
                {
                    "image": "frame.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.99}],
                },
                {
                    "image": "frame2.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.97}],
                },
                {
                    "image": "frame3.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.96}],
                },
            ]
        }
    )
    called: list[dict] = []
    app.handle_spec = lambda spec, **kwargs: called.append(spec) or {"ok": True, "status": "ok"}

    result = app.gesture(None, 8)

    assert result["ok"] is True
    assert result["gesture"] == "thumbs_up"
    assert called and called[0]["source"] == "gesture"


def test_two_of_five_votes_do_not_write_feedback():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.capture_burst = lambda *args, **kwargs: ok({"dir": "burst"})
    app.orch.multimodal.recognize_gesture_batch = lambda *args, **kwargs: ok(
        {
            "results": [
                {
                    "image": "frame.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.99}],
                },
                {
                    "image": "frame2.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.97}],
                },
            ]
        }
    )
    called: list[dict] = []
    app.handle_spec = lambda spec, **kwargs: called.append(spec) or {"ok": True}

    result = app.gesture(None, 8)

    assert result["ok"] is False
    assert result["code"] == "E_LOW_CONFIDENCE_MODALITY"
    assert "至少需要 3 帧" in result["error"]
    assert called == []


def test_gesture_item_id_sets_session_focus_before_feedback():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.capture_burst = lambda *args, **kwargs: ok({"dir": "burst"})
    app.orch.multimodal.recognize_gesture_batch = lambda *args, **kwargs: ok(
        {
            "results": [
                {
                    "image": f"frame{i}.jpg",
                    "hands_detected": 1,
                    "gestures": [{"name": "thumbs_up", "confidence": 0.96}],
                }
                for i in range(3)
            ]
        }
    )
    session = app.orch.store.get_or_create(app.session_id, app.user_id)
    called: list[dict] = []
    app.handle_spec = lambda spec, **kwargs: called.append(spec) or {"ok": True, "status": "ok"}

    result = app.gesture("20829", 8)

    assert result["ok"] is True
    assert session.focus_item_id == "20829"
    assert session.last_item_ids[0] == "20829"
    assert called and called[0]["source"] == "gesture"


def test_feedback_clear_returns_ok_wrapper():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.clear_feedback = lambda *args, **kwargs: ok({"status": "ok", "feedback": {"polarity": "none"}})

    result = app.feedback("20829", "none", 8)

    assert result["ok"] is True
    assert result["response"]["feedback"]["polarity"] == "none"


def test_feedback_clear_skips_tts_synthesis():
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    captured: dict[str, bool] = {}

    def fake_clear(*args, **kwargs):
        captured["dry_run"] = app.orch.dry_run_voice
        return ok({"status": "ok", "feedback": {"polarity": "none"}})

    app.orch.clear_feedback = fake_clear
    original = app.orch.dry_run_voice

    result = app.feedback("20829", "none", 8)

    assert result["ok"] is True
    assert captured["dry_run"] is True
    assert app.orch.dry_run_voice is original


def test_feedback_write_skips_tts_synthesis():
    """点赞/倒赞只更新货架，不能在请求线程里同步合成语音。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    captured: dict[str, bool] = {}

    def fake_handle(spec, **kwargs):
        captured["dry_run"] = app.orch.dry_run_voice
        return {"ok": True, "status": "ok"}

    app.handle_spec = fake_handle
    original = app.orch.dry_run_voice

    result = app.feedback("20829", "up", 8)

    assert result["ok"] is True
    assert captured["dry_run"] is True
    assert app.orch.dry_run_voice is original


def test_vision_uses_object_prompt_and_passes_visual_tag():
    """举物品识图：默认提示词聚焦主要物品，VL 描述进入视觉标签槽位。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    captured: dict[str, object] = {}

    app.orch.multimodal.capture_frame = lambda *args, **kwargs: ok({"image": "frame.jpg"})

    def fake_describe(image, question):
        captured["question"] = question
        return ok({"response": "一个白色陶瓷杯"})

    app.orch.multimodal.describe_image = fake_describe

    def fake_handle(spec, **kwargs):
        captured["spec"] = spec
        return {
            "ok": True,
            "response": {
                "status": "ok",
                "recommendations": [],
                "intent_result": {"slots": {"category": {"value": "厨房用品"}}},
            },
        }

    app.handle_spec = fake_handle

    result = app.vision("", 8)

    assert result["ok"] is True
    assert "最主要的一个物品" in str(captured["question"])
    assert captured["spec"]["source"] == "vision"
    assert captured["spec"]["text"] is None
    assert captured["spec"]["visual_tags"][0]["tag"] == "一个白色陶瓷杯"
    assert result["description"] == "一个白色陶瓷杯"


def test_vision_reuses_camera_worker_frame():
    """摄像头直播已占用设备时，识图必须复用 worker 帧，不能再开一路 VideoCapture。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    jpeg = b"\xff\xd8camera-frame\xff\xd9"
    captured: dict[str, bytes] = {}

    class FakeCamera:
        def start(self):
            return ok({"running": True})

        def frame(self):
            return jpeg

    app.orch.camera = FakeCamera()

    def must_not_open_camera(*args, **kwargs):
        raise AssertionError("camera worker 已在运行，不应直接打开第二路摄像头")

    app.orch.multimodal.capture_frame = must_not_open_camera

    def fake_describe(image, question):
        captured["image"] = Path(image).read_bytes()
        return ok({"response": "一副黑色眼镜"})

    app.orch.multimodal.describe_image = fake_describe
    app.handle_spec = lambda *args, **kwargs: {
        "ok": True,
        "response": {
            "status": "ok",
            "recommendations": [],
            "intent_result": {"slots": {"category": {"value": "饰品"}}},
        },
    }

    result = app.vision("", 8)

    assert result["ok"] is True
    assert captured["image"] == jpeg


def test_vision_unmapped_object_returns_actionable_error():
    """识别不到商品品类时要提示重试，不能拿无关商品凑推荐。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.multimodal.active_mode = "real"
    app.orch.multimodal.capture_frame = lambda *args, **kwargs: ok({"image": "frame.jpg"})
    app.orch.multimodal.describe_image = lambda *args, **kwargs: ok({"response": "一个不明物体"})
    app.handle_spec = lambda *args, **kwargs: {"ok": True, "response": {"status": "ok", "intent_result": {"slots": {}}}}

    result = app.vision("", 8)

    assert result["ok"] is False
    assert result["code"] == str(w.ErrorCode.LOW_CONFIDENCE_MODALITY)
    assert "未能确定商品品类" in result["error"]


def test_chat_receives_explicit_focus_item():
    """网页追问必须显式把当前商品传给后端，不能只依赖前端局部变量。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    captured: dict[str, str] = {}

    def fake_handle(spec, **kwargs):
        session = app.orch.store.get_or_create(app.session_id, app.user_id)
        captured["focus"] = session.focus_item_id or ""
        captured["text"] = spec.get("text") or ""
        return {"ok": True, "response": {"status": "ok", "recommendations": []}}

    app.handle_spec = fake_handle

    result = app.chat("为什么推荐它", 8, "20829")

    assert result["ok"] is True
    assert captured == {"focus": "20829", "text": "为什么推荐它"}


def test_home_and_more_do_not_call_llm_explain():
    """首页货架/翻页必须快；运行时开着 LLM 解释也不能在这里拉起 4B 模型。"""
    w = _webui()
    app = w.App("15547", "offline", False, False, None)
    app.orch.llm_explainer.enabled = True
    t0 = time.time()
    home = app.home(8)
    assert home["ok"] and len(home["items"]) == 8
    more = app.more("推荐一些热门商品", 16)
    assert more["ok"] and len(more["items"]) == 16
    assert app.orch.llm_explainer.enabled is True
    assert time.time() - t0 < 5


def test_pick_gesture_uses_votes_then_confidence():
    """连拍结果按票数优先、同票数看最高置信度。"""
    rows = [
        {"hands_detected": 1, "gestures": [{"name": "thumbs_up", "confidence": 0.7}]},
        {"hands_detected": 1, "gestures": [{"name": "thumbs_up", "confidence": 0.9}]},
        {"hands_detected": 1, "gestures": [{"name": "victory", "confidence": 0.99}]},
    ]
    name, conf, votes, hands = _webui()._pick_gesture(rows)
    assert name == "thumbs_up"
    assert conf == 0.9
    assert votes == {"thumbs_up": 2, "victory": 1}
    assert hands == 3


def test_pick_gesture_returns_none_without_gesture():
    name, conf, votes, hands = _webui()._pick_gesture([{"hands_detected": 1, "gestures": []}])
    assert name is None and conf == 0.0 and votes == {} and hands == 1


def test_from_app_error_maps_code_and_field():
    w = _webui()
    err = w.ErrorCode.ITEM_NOT_FOUND
    payload = w._from_app_error(type("E", (), {"code": err, "message": "not found", "field": "item_id"})())
    assert payload["code"] == "E_ITEM_NOT_FOUND"
    assert payload["field"] == "item_id"
