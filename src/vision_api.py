"""在线视觉理解客户端（OpenAI 兼容接口）。

默认对接阿里云百炼 DashScope 的 Qwen VL。Key 只从服务端环境变量读取，
不会进入前端、日志或证据文件。
"""

from __future__ import annotations

import base64
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from config import settings
from src.errors import ErrorCode, Result, fail, ok

DEFAULT_VISION_QUESTION = (
    "只识别画面中央最主要的一个物品。只回答「颜色+品类」。"
    "品类优先从这些词中选择：家居装饰、装饰品、厨房用品、生活用品、收纳用品、派对用品、"
    "手工用品、饰品、购物袋、玩具、园艺用品、办公用品、宠物用品、文具用品。"
    "不要描述人物、动作或背景。"
)


@dataclass(frozen=True)
class VisionAPIConfig:
    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_s: float
    max_tokens: int
    max_image_bytes: int

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def public_info(self) -> dict[str, Any]:
        return {
            "configured": True,
            "provider": self.provider,
            "model": self.model,
            "endpoint_host": urlparse(self.base_url).netloc or self.base_url,
            "timeout_s": self.timeout_s,
        }


def resolve_vision_api_config(cfg: dict[str, Any] | None = None) -> VisionAPIConfig | None:
    cfg = cfg or settings.VISION_API_CONFIG
    provider = str(cfg.get("provider") or "dashscope").strip().lower()
    if provider in {"", "off", "none", "local", "0", "false"}:
        return None
    key_env = str(cfg.get("api_key_env") or "DASHSCOPE_API_KEY")
    api_key = str(os.getenv(key_env) or "").strip()
    base_url = str(cfg.get("base_url") or "").strip()
    model = str(cfg.get("model") or "").strip()
    if not api_key or not base_url or not model:
        return None
    return VisionAPIConfig(
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model=model,
        timeout_s=max(1.0, float(cfg.get("timeout_s", 15))),
        max_tokens=max(1, int(cfg.get("max_tokens", 32))),
        max_image_bytes=max(1024, int(cfg.get("max_image_bytes", 4 * 1024 * 1024))),
    )


def _redact(text: str, api_key: str) -> str:
    value = str(text or "")
    if api_key:
        value = value.replace(api_key, "***")
    return value[:1200]


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
        return "\n".join(parts).strip()
    return ""


class DashScopeVisionClient:
    """调用支持图片输入的 OpenAI 兼容视觉模型。"""

    def __init__(self, config: VisionAPIConfig) -> None:
        self.cfg = config
        self.last_error: str | None = None

    @classmethod
    def from_env(cls) -> DashScopeVisionClient | None:
        config = resolve_vision_api_config()
        return cls(config) if config is not None else None

    def public_info(self) -> dict[str, Any]:
        return self.cfg.public_info()

    def describe_image(self, image_path: str | Path, question: str | None = None) -> Result:
        try:
            import requests
        except Exception as exc:  # pragma: no cover - requirements 中已提供
            return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线视觉依赖 requests 不可用: {exc}")

        path = Path(image_path)
        if not path.is_file():
            return fail(ErrorCode.INVALID_INPUT, f"图片不存在: {path}", field="image")
        data = path.read_bytes()
        if not data:
            return fail(ErrorCode.INVALID_INPUT, "图片为空", field="image")
        if len(data) > self.cfg.max_image_bytes:
            return fail(ErrorCode.INVALID_INPUT, "图片超过在线接口大小限制", field="image")

        suffix = path.suffix.lower()
        mime = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        image_b64 = base64.b64encode(data).decode("ascii")
        prompt = str(question or "").strip() or DEFAULT_VISION_QUESTION
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime};base64,{image_b64}"},
                        },
                    ],
                }
            ],
            "max_tokens": self.cfg.max_tokens,
            "temperature": 0,
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }

        started = time.perf_counter()
        try:
            response = requests.post(
                self.cfg.endpoint,
                headers=headers,
                json=payload,
                timeout=self.cfg.timeout_s,
            )
        except requests.exceptions.Timeout:
            self.last_error = "在线视觉请求超时"
            return fail(ErrorCode.TIMEOUT, self.last_error)
        except requests.exceptions.RequestException as exc:
            self.last_error = _redact(f"{type(exc).__name__}: {exc}", self.cfg.api_key)
            return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线视觉连接失败: {self.last_error}")

        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        if response.status_code >= 400:
            body = _redact(response.text, self.cfg.api_key)
            self.last_error = f"HTTP {response.status_code}: {body}"
            code = ErrorCode.TIMEOUT if response.status_code in {408, 429, 504} else ErrorCode.MODALITY_UNAVAILABLE
            return fail(code, f"在线视觉返回 HTTP {response.status_code}: {body}")
        try:
            payload_data = response.json()
        except Exception:
            body = _redact(response.text, self.cfg.api_key)
            self.last_error = f"在线视觉响应不是 JSON: {body}"
            return fail(ErrorCode.INTERNAL_ERROR, self.last_error)

        choices = payload_data.get("choices") or []
        message = (choices[0] or {}).get("message") or {} if choices else {}
        text = _content_text(message.get("content"))
        if not text:
            self.last_error = "在线视觉返回空结果"
            return fail(ErrorCode.MODALITY_UNAVAILABLE, self.last_error)
        self.last_error = None
        return ok(
            {
                "response": text,
                "provider": self.cfg.provider,
                "model": payload_data.get("model") or self.cfg.model,
                "latency_ms": latency_ms,
                "usage": payload_data.get("usage") or {},
            }
        )
