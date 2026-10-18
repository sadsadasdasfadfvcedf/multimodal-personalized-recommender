"""src/adapters/multimodal_adapter.py —— 空文本的语义区分。

回归锁：视觉/手势按钮不输入问题时，前端曾传 text=""，parse_event 把它当成
「空 ASR 文本」直接返回 E_EMPTY_TRANSCRIPT，视觉标签永远走不到 modality=visual。
但语音识别为空必须继续报错（FAULT-04 依赖这个行为），所以不能一刀切。
"""

from __future__ import annotations

from src.adapters.multimodal_adapter import MultimodalAdapter
from src.errors import ErrorCode


def _parse(spec):
    return MultimodalAdapter().parse_event(spec, user_id="u-test")


def test_empty_text_with_visual_tags_is_treated_as_no_text():
    r = _parse({"source": "vision", "text": "", "visual_tags": [{"tag": "白色马克杯", "confidence": 0.85}]})
    assert r.ok
    assert r.data.text is None
    assert r.data.modality == "visual"


def test_whitespace_text_with_visual_tags_is_treated_as_no_text():
    r = _parse({"source": "vision", "text": "   ", "visual_tags": [{"tag": "白色马克杯", "confidence": 0.85}]})
    assert r.ok
    assert r.data.modality == "visual"


def test_empty_text_with_gesture_is_treated_as_no_text():
    r = _parse({"source": "gesture", "text": "", "gesture": {"name": "thumbs_up", "confidence": 0.92}})
    assert r.ok
    assert r.data.text is None
    assert r.data.modality == "feedback"


def test_empty_voice_text_still_errors():
    """语音链路识别为空是真错误，不能被「空文本忽略」逻辑吞掉。"""
    r = _parse({"source": "voice", "text": "   "})
    assert not r.ok
    assert r.error is not None
    assert r.error.code == ErrorCode.EMPTY_TRANSCRIPT


def test_nonempty_text_with_visual_tags_keeps_text():
    r = _parse({"source": "vision", "text": "这是什么", "visual_tags": [{"tag": "白色马克杯", "confidence": 0.85}]})
    assert r.ok
    assert r.data.text == "这是什么"
    assert r.data.modality == "asr"
