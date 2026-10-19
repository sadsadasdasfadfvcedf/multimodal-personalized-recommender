"""在线 LLM 配置解析、请求协议与本地回退的单元测试。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import requests

from src.errors import ErrorCode, fail, ok
from src.llm_chat import ChatService
from src.llm_explain import LlmExplainer
from src.online_llm import (
    GENERIC_KEY_ENV,
    PROVIDER_PRESETS,
    OnlineChatClient,
    OnlineLLMConfig,
    resolve_online_config,
)


def _clear_online_env(monkeypatch) -> None:
    names = [
        GENERIC_KEY_ENV,
        "INTEGRATED_LLM_PROVIDER",
        "INTEGRATED_ONLINE_LLM_PROVIDER",
        "INTEGRATED_ONLINE_LLM_BASE_URL",
        "INTEGRATED_ONLINE_LLM_MODEL",
        "INTEGRATED_ONLINE_LLM_STRICT",
        *[preset["api_key_env"] for preset in PROVIDER_PRESETS.values()],
    ]
    for name in names:
        monkeypatch.delenv(name, raising=False)


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self._text = text
        self.status_code = status_code
        self.text = text

    def json(self) -> dict[str, Any]:
        return {"choices": [{"message": {"content": self._text}}]}


def _config(**overrides: Any) -> OnlineLLMConfig:
    values: dict[str, Any] = {
        "provider": "custom",
        "base_url": "https://example.com/v1",
        "api_key": "sk-secret",
        "model": "test-model",
        "timeout_s": 1,
        "concurrency": 2,
    }
    values.update(overrides)
    return OnlineLLMConfig(**values)


def test_resolve_auto_detects_dashscope(monkeypatch):
    _clear_online_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope")

    cfg = resolve_online_config({"provider": "auto", "online": {"enabled": True}})

    assert cfg is not None
    assert cfg.provider == "dashscope"
    assert cfg.model == "qwen-plus"
    assert cfg.base_url == "https://dashscope.aliyuncs.com/compatible-mode/v1"


def test_resolve_local_mode_ignores_key(monkeypatch):
    _clear_online_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope")

    assert resolve_online_config({"provider": "local", "online": {"enabled": True}}) is None


def test_resolve_generic_openai_compatible_endpoint(monkeypatch):
    _clear_online_env(monkeypatch)
    monkeypatch.setenv(GENERIC_KEY_ENV, "sk-generic")
    monkeypatch.setenv("INTEGRATED_ONLINE_LLM_BASE_URL", "https://gateway.example/v1/chat/completions")
    monkeypatch.setenv("INTEGRATED_ONLINE_LLM_MODEL", "my-model")

    cfg = resolve_online_config({"provider": "auto", "online": {"enabled": True}})

    assert cfg is not None
    assert cfg.provider == "custom"
    assert cfg.base_url == "https://gateway.example/v1"
    assert cfg.model == "my-model"


def test_deepseek_provider_mode_wins_over_other_keys(monkeypatch):
    _clear_online_env(monkeypatch)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "sk-dashscope")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-deepseek")

    cfg = resolve_online_config({"provider": "deepseek", "online": {"enabled": True}})

    assert cfg is not None
    assert cfg.provider == "deepseek"
    assert cfg.model == "deepseek-chat"
    assert cfg.base_url == "https://api.deepseek.com/v1"


def test_online_chat_keeps_multi_turn_history_and_reset(monkeypatch):
    calls: list[dict[str, Any]] = []

    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        calls.append({"url": url, **kwargs})
        return FakeResponse("回答" + str(len(calls)))

    monkeypatch.setattr(requests, "post", fake_post)
    client = OnlineChatClient(_config())

    assert client.chat("第一问").data == "回答1"
    assert client.chat("第二问").data == "回答2"

    second_messages = calls[1]["json"]["messages"]
    assert second_messages[1] == {"role": "user", "content": "第一问"}
    assert second_messages[2] == {"role": "assistant", "content": "回答1"}
    assert second_messages[-1] == {"role": "user", "content": "第二问"}
    assert "sk-secret" not in str(client.report())

    client.reset()
    assert client.chat("重置后").ok
    assert calls[2]["json"]["messages"][-1] == {"role": "user", "content": "重置后"}
    assert len(calls[2]["json"]["messages"]) == 2


def test_online_complete_many_preserves_input_order(monkeypatch):
    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        prompt = kwargs["json"]["messages"][-1]["content"]
        return FakeResponse("答复:" + prompt)

    monkeypatch.setattr(requests, "post", fake_post)
    client = OnlineChatClient(_config(concurrency=2))

    result = client.complete_many(["甲", "乙", "丙"])

    assert result.ok
    assert [item["text"] for item in result.data["outputs"]] == ["答复:甲", "答复:乙", "答复:丙"]


def test_online_client_over_real_local_http():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            prompt = body["messages"][-1]["content"]
            payload = json.dumps(
                {"choices": [{"message": {"content": "回复:" + prompt}}]},
                ensure_ascii=False,
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = _config(base_url=f"http://127.0.0.1:{server.server_port}/v1")
        result = OnlineChatClient(cfg).chat("联调")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result.ok
    assert result.data == "回复:联调"


def test_online_error_redacts_api_key(monkeypatch):
    def fake_post(url: str, **kwargs: Any) -> FakeResponse:
        return FakeResponse("bad key sk-secret", status_code=401)

    monkeypatch.setattr(requests, "post", fake_post)
    result = OnlineChatClient(_config()).chat("你好")

    assert not result.ok
    assert result.error is not None
    assert "sk-secret" not in result.error.message
    assert "***" in result.error.message


def test_chat_service_falls_back_to_local_when_online_fails(monkeypatch):
    online = OnlineChatClient(_config())
    monkeypatch.setattr(online, "chat", lambda text: fail(ErrorCode.MODALITY_UNAVAILABLE, "online down"))
    service = ChatService(
        cfg={
            "provider": "auto",
            "online": {"enabled": True},
            "timeout_s": 1,
            "startup_timeout_s": 1,
            "worker": "unused",
            "model_candidates": [],
        },
        online_client=online,
    )
    monkeypatch.setattr(service, "_ensure", lambda: True)
    monkeypatch.setattr(service, "_send", lambda payload: ok("本地回答:" + str(payload.get("text"))))

    result = service.chat("你好")

    assert result.ok
    assert result.data == "本地回答:你好"
    assert service.report()["last_provider"] == "local"
    assert "online down" in str(service.report()["last_error"])


def test_llm_explainer_available_with_online_chat_only():
    class FakeChat:
        online_available = True

    explainer = LlmExplainer(adapter=None, catalog=None, enabled=True, chat=FakeChat())

    assert explainer.available()
