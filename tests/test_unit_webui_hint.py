"""webui.py —— 依赖错误的可操作提示。

浏览器里只会看到一段 error 文本；如果它是
"'MessageFactory' object has no attribute 'GetPrototype'"，没人知道要降 protobuf。
这里把「错误签名 -> 修复建议」钉住。
"""

from __future__ import annotations

import importlib.util

from conftest import PROJECT_ROOT


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


def test_from_app_error_maps_code_and_field():
    w = _webui()
    err = w.ErrorCode.ITEM_NOT_FOUND
    payload = w._from_app_error(type("E", (), {"code": err, "message": "not found", "field": "item_id"})())
    assert payload["code"] == "E_ITEM_NOT_FOUND"
    assert payload["field"] == "item_id"
