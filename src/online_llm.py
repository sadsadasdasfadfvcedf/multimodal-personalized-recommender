"""
src/online_llm.py
=================

OpenAI 兼容的在线大模型客户端。

设计目标
--------
1. **不绑定单一厂商**：DashScope、DeepSeek、OpenAI、SiliconFlow、
   Moonshot、智谱以及任意 OpenAI 兼容网关都可使用。
2. **不落盘密钥**：Key 只从环境变量读取，错误信息会先脱敏。
3. **可靠降级**：在线请求失败时由 ``ChatService`` 自动回退本地 Qwen，
   不影响离线演示；设置 ``INTEGRATED_ONLINE_LLM_STRICT=1`` 可关闭回退。
4. **可测试**：HTTP 调用集中在 ``_request``，单元测试无需真实联网。
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from .errors import ErrorCode, Result, fail, ok

DEFAULT_SYSTEM_PROMPT = """你是「多模态个性化推荐助手」的对话引擎，运行在一个课程项目中。

回答原则:
1. 用简洁、自然的中文直接回答问题，默认控制在 150 字以内。
2. 不要编造来源、文献、链接、统计数据或商品信息；不确定就如实说明。
3. 与商品推荐无关的问题就事论事回答，不要强行推荐商品。
4. 用户要求写文案、解释概念、翻译或计算时，按需求给出结果。
"""

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "dashscope": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
    },
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "siliconflow": {
        "api_key_env": "SILICONFLOW_API_KEY",
        "base_url": "https://api.siliconflow.cn/v1",
        "model": "Qwen/Qwen2.5-7B-Instruct",
    },
    "moonshot": {
        "api_key_env": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-8k",
    },
    "zhipu": {
        "api_key_env": "ZHIPUAI_API_KEY",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
    },
}

PROVIDER_ORDER = ["dashscope", "deepseek", "openai", "siliconflow", "moonshot", "zhipu"]
GENERIC_KEY_ENV = "INTEGRATED_ONLINE_LLM_API_KEY"


def _env(name: str) -> str:
    return str(os.getenv(name) or "").strip()


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


def _strip_endpoint(base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    suffix = "/chat/completions"
    if url.endswith(suffix):
        url = url[: -len(suffix)]
    return url.rstrip("/")


def _redact(text: str, api_key: str) -> str:
    value = str(text or "")
    if api_key:
        value = value.replace(api_key, "***")
    return value[:500]


@dataclass(frozen=True)
class OnlineLLMConfig:
    """在线模型配置。``api_key`` 只存在于进程内存中。"""

    provider: str
    base_url: str
    api_key: str
    model: str
    timeout_s: float = 45.0
    max_history_messages: int = 12
    concurrency: int = 3
    strict: bool = False
    system_prompt: str = DEFAULT_SYSTEM_PROMPT

    def public_info(self) -> dict[str, Any]:
        """可安全写入日志/接口的配置摘要（不含 Key）。"""
        host = urlparse(self.base_url).netloc or self.base_url
        return {
            "configured": True,
            "provider": self.provider,
            "model": self.model,
            "endpoint_host": host,
            "strict": self.strict,
        }


def resolve_online_config(cfg: dict[str, Any] | None = None) -> OnlineLLMConfig | None:
    """从环境变量解析在线配置；没有任何可用 Key 时返回 ``None``。

    优先级：
    1. ``INTEGRATED_ONLINE_LLM_API_KEY`` + 通用 base/model
    2. ``INTEGRATED_ONLINE_LLM_PROVIDER`` 指定厂商的专用 Key
    3. 自动扫描常见厂商 Key
    """
    cfg = cfg or {}
    raw_online_cfg = cfg.get("online")
    online_cfg: dict[str, Any] = raw_online_cfg if isinstance(raw_online_cfg, dict) else {}
    mode = str(cfg.get("provider") or _env("INTEGRATED_LLM_PROVIDER") or "auto").lower()
    if mode == "local" or online_cfg.get("enabled") is False:
        return None

    explicit_provider = _env("INTEGRATED_ONLINE_LLM_PROVIDER").lower()
    generic_key = _env(GENERIC_KEY_ENV)
    generic_base = _env("INTEGRATED_ONLINE_LLM_BASE_URL")
    generic_model = _env("INTEGRATED_ONLINE_LLM_MODEL")

    provider = ""
    api_key = ""
    base_url = ""
    model = ""

    if explicit_provider in PROVIDER_PRESETS:
        preset = PROVIDER_PRESETS[explicit_provider]
        provider = explicit_provider
        api_key = generic_key or _env(preset["api_key_env"])
        base_url = generic_base or preset["base_url"]
        model = generic_model or preset["model"]
    elif generic_key:
        provider = explicit_provider or "custom"
        api_key = generic_key
        base_url = generic_base
        model = generic_model
    else:
        for candidate in PROVIDER_ORDER:
            preset = PROVIDER_PRESETS[candidate]
            key = _env(preset["api_key_env"])
            if key:
                provider = candidate
                api_key = key
                base_url = generic_base or preset["base_url"]
                model = generic_model or preset["model"]
                break

    if not api_key or not base_url or not model:
        return None

    return OnlineLLMConfig(
        provider=provider,
        base_url=_strip_endpoint(base_url),
        api_key=api_key,
        model=model,
        timeout_s=float(str(online_cfg.get("timeout_s") or os.getenv("INTEGRATED_ONLINE_LLM_TIMEOUT_S", "45"))),
        max_history_messages=max(2, int(online_cfg.get("max_history_messages") or 12)),
        concurrency=max(1, int(online_cfg.get("concurrency") or 3)),
        strict=_flag("INTEGRATED_ONLINE_LLM_STRICT", False),
        system_prompt=str(online_cfg.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
    )


class OnlineChatClient:
    """OpenAI 兼容 Chat Completions 客户端。

    只负责在线调用与对话历史；是否回退本地由 ``ChatService`` 决定。
    """

    def __init__(self, config: OnlineLLMConfig) -> None:
        self.cfg = config
        self._history: list[dict[str, str]] = []
        self.last_error: str | None = None

    def _endpoint(self) -> str:
        return _strip_endpoint(self.cfg.base_url) + "/chat/completions"

    def _request(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        timeout_s: float,
    ) -> Result:
        try:
            import requests
        except Exception as exc:  # pragma: no cover - 依赖缺失时才会走到
            return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线 LLM 依赖 requests 不可用: {exc}")

        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "max_tokens": max(1, int(max_tokens)),
            "temperature": float(temperature),
            "stream": False,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.api_key}",
        }

        last_link_error = ""
        for attempt in range(2):
            try:
                response = requests.post(
                    self._endpoint(),
                    json=payload,
                    headers=headers,
                    timeout=max(1.0, float(timeout_s)),
                )
            except requests.exceptions.Timeout:
                self.last_error = "在线 LLM 请求超时"
                return fail(ErrorCode.TIMEOUT, self.last_error)
            except requests.exceptions.RequestException as exc:
                last_link_error = _redact(f"{type(exc).__name__}: {exc}", self.cfg.api_key)
                self.last_error = last_link_error
                if attempt == 0:
                    time.sleep(0.4)
                    continue
                return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线 LLM 连接失败: {last_link_error}")

            status = int(response.status_code)
            if status < 400:
                return self._parse_response(response)

            body = _redact(getattr(response, "text", ""), self.cfg.api_key)
            self.last_error = f"HTTP {status}: {body}"
            if status in {429, 500, 502, 503, 504} and attempt == 0:
                time.sleep(0.6)
                continue
            code = ErrorCode.TIMEOUT if status == 429 else ErrorCode.MODALITY_UNAVAILABLE
            return fail(code, f"在线 LLM 返回 HTTP {status}: {body}")

        return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线 LLM 调用失败: {last_link_error or self.last_error}")

    def _parse_response(self, response: Any) -> Result:
        try:
            data = response.json()
        except Exception:
            body = _redact(getattr(response, "text", ""), self.cfg.api_key)
            self.last_error = f"响应不是 JSON: {body}"
            return fail(ErrorCode.INTERNAL_ERROR, self.last_error)

        choices = data.get("choices") if isinstance(data, dict) else None
        if not choices:
            self.last_error = "在线 LLM 响应缺少 choices"
            return fail(ErrorCode.INTERNAL_ERROR, self.last_error)

        first = choices[0] if isinstance(choices[0], dict) else {}
        raw_message = first.get("message")
        message: dict[str, Any] = raw_message if isinstance(raw_message, dict) else {}
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                    parts.append(str(item.get("text") or ""))
            content = "".join(parts)
        text = str(content or "").strip()
        if not text:
            self.last_error = "在线 LLM 返回空文本"
            return fail(ErrorCode.INTERNAL_ERROR, self.last_error)
        self.last_error = None
        return ok(text, model=self.cfg.model, provider=self.cfg.provider)

    def _messages(self, user_text: str) -> list[dict[str, str]]:
        history = self._history[-self.cfg.max_history_messages :]
        return [{"role": "system", "content": self.cfg.system_prompt}, *history, {"role": "user", "content": user_text}]

    def chat(self, text: str) -> Result:
        """多轮对话；失败时不污染历史。"""
        user_text = str(text or "").strip()
        if not user_text:
            return fail(ErrorCode.INVALID_INPUT, "对话文本不能为空")
        result = self._request(
            self._messages(user_text),
            max_tokens=320,
            temperature=0.7,
            timeout_s=self.cfg.timeout_s,
        )
        if not result.ok:
            return result
        answer = str(result.data or "").strip()
        self._history.extend(
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": answer},
            ]
        )
        if len(self._history) > self.cfg.max_history_messages:
            self._history = self._history[-self.cfg.max_history_messages :]
        return result

    def complete_many(
        self,
        prompts: list[str],
        *,
        max_new_tokens: int = 120,
        temperature: float = 0.2,
    ) -> Result:
        """并发生成多条无状态提示词；输出顺序与输入严格一致。"""
        items = [str(prompt or "") for prompt in prompts]
        if not items:
            return ok({"outputs": [], "model": self.cfg.model, "provider": self.cfg.provider, "failed": 0})

        results: list[Result | None] = [None] * len(items)

        def run(index: int, prompt: str) -> tuple[int, Result]:
            messages = [
                {"role": "system", "content": self.cfg.system_prompt},
                {"role": "user", "content": prompt},
            ]
            return index, self._request(
                messages,
                max_tokens=max_new_tokens,
                temperature=temperature,
                timeout_s=self.cfg.timeout_s,
            )

        workers = max(1, min(self.cfg.concurrency, len(items)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, index, prompt) for index, prompt in enumerate(items)]
            for future in as_completed(futures):
                try:
                    index, result = future.result()
                    results[index] = result
                except Exception as exc:
                    # 找到对应索引比较麻烦；统一记录，随后按空结果回退模板。
                    self.last_error = _redact(f"{type(exc).__name__}: {exc}", self.cfg.api_key)

        outputs: list[dict[str, Any]] = []
        failures: list[str] = []
        for item_result in results:
            if item_result is not None and item_result.ok:
                outputs.append({"text": str(item_result.data or "").strip()})
                continue
            reason = (
                item_result.error.message
                if item_result is not None and item_result.error
                else (self.last_error or "未知错误")
            )
            outputs.append({"text": "", "error": reason})
            failures.append(reason)

        if len(failures) == len(outputs):
            return fail(ErrorCode.MODALITY_UNAVAILABLE, f"在线 LLM 批量调用失败: {failures[0]}")
        return ok(
            {
                "outputs": outputs,
                "model": self.cfg.model,
                "provider": self.cfg.provider,
                "failed": len(failures),
            }
        )

    def reset(self) -> None:
        self._history.clear()

    def report(self) -> dict[str, Any]:
        return {
            **self.cfg.public_info(),
            "last_error": self.last_error,
        }
