"""在线视觉 API 客户端：响应解析、超时与密钥脱敏。"""

from __future__ import annotations

import requests

from src.errors import ErrorCode
from src.vision_api import DashScopeVisionClient, VisionAPIConfig


def _config() -> VisionAPIConfig:
    return VisionAPIConfig(
        provider="dashscope",
        base_url="https://example.test/v1",
        api_key="secret-vision-key",
        model="qwen3-vl-plus",
        timeout_s=5,
        max_tokens=32,
        max_image_bytes=1024 * 1024,
    )


class _Response:
    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


def test_online_vision_success(monkeypatch, tmp_path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    captured: dict = {}

    def fake_post(url, headers, json, timeout):
        captured.update({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return _Response(
            200,
            {
                "model": "qwen3-vl-plus",
                "choices": [{"message": {"content": "粉色花卉图案杯子"}}],
                "usage": {"total_tokens": 100},
            },
        )

    monkeypatch.setattr(requests, "post", fake_post)
    client = DashScopeVisionClient(_config())

    result = client.describe_image(image, "只回答颜色+品类")

    assert result.ok
    assert result.data["response"] == "粉色花卉图案杯子"
    assert result.data["model"] == "qwen3-vl-plus"
    assert result.data["provider"] == "dashscope"
    assert captured["url"].endswith("/chat/completions")
    assert captured["headers"]["Authorization"] == "Bearer secret-vision-key"
    assert "secret-vision-key" not in str(client.public_info())


def test_online_vision_http_error_redacts_key(monkeypatch, tmp_path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: _Response(401, {"error": "bad key secret-vision-key"}),
    )
    client = DashScopeVisionClient(_config())

    result = client.describe_image(image)

    assert not result.ok
    assert result.error.code == ErrorCode.MODALITY_UNAVAILABLE
    assert "secret-vision-key" not in result.error.message
    assert "***" in result.error.message


def test_online_vision_timeout(monkeypatch, tmp_path):
    image = tmp_path / "frame.jpg"
    image.write_bytes(b"\xff\xd8fake-jpeg\xff\xd9")

    def timeout(*args, **kwargs):
        raise requests.exceptions.Timeout()

    monkeypatch.setattr(requests, "post", timeout)
    client = DashScopeVisionClient(_config())

    result = client.describe_image(image)

    assert not result.ok
    assert result.error.code == ErrorCode.TIMEOUT
