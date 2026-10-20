"""webui.py —— 依赖错误的可操作提示。

浏览器里只会看到一段 error 文本；如果它是
"'MessageFactory' object has no attribute 'GetPrototype'"，没人知道要降 protobuf。
这里把「错误签名 -> 修复建议」钉住。
"""

from __future__ import annotations

import importlib.util
import time

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
