"""
src/llm_chat.py
===============

常驻 LLM 对话服务（客户端）。

- 懒启动：第一次聊天时才拉起 `scripts/llm_chat_worker.py`
- 复用：之后所有消息走同一个进程（模型只加载一次）
- 隔离：cwd/PYTHONPATH 指向 multimodal_ai，避免两个项目的 config/src 冲突
- 失败可恢复：worker 退出后下次请求自动重启
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from config import settings

from .errors import ErrorCode, Result, fail, ok
from .online_llm import OnlineChatClient, resolve_online_config


def _to_simplified(text: str) -> str:
    """聊天输出偶发繁体，统一转简体；未安装 zhconv 时原样返回。

    不复用 `text_norm.normalize_fluency`：那套逻辑会删除未映射的英文词，
    适合商品解释，不适合会提到 LSTM / Top-K 等术语的开放问答。
    """
    try:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import zhconv  # type: ignore

        return str(zhconv.convert(text, "zh-cn"))
    except Exception:
        return text


class ChatService:
    """常驻对话客户端。线程安全（同一时刻只处理一条消息）。"""

    def __init__(self, cfg: dict | None = None, online_client: OnlineChatClient | None = None) -> None:
        self.cfg = cfg or settings.CHAT_CONFIG
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0
        self._started_at = 0.0
        # 在线后端是可选的：有 Key 时优先，失败后仍可回退本地 worker。
        self._online: OnlineChatClient | None = online_client
        if self._online is None and self.cfg.get("online") is not None:
            online_cfg = resolve_online_config(self.cfg)
            if online_cfg is not None:
                self._online = OnlineChatClient(online_cfg)
        self._last_provider = "online" if self._online is not None else "local"
        self._last_error: str | None = None

    @classmethod
    def online_configured(cls, cfg: dict | None = None) -> bool:
        """只检查配置，不发起网络请求。"""
        return resolve_online_config(cfg or settings.CHAT_CONFIG) is not None

    @property
    def provider_mode(self) -> str:
        return str(self.cfg.get("provider") or "auto").strip().lower()

    @property
    def online_available(self) -> bool:
        return self._online is not None and self.provider_mode != "local"

    def report(self) -> dict:
        online = self._online.report() if self._online is not None else {"configured": False}
        return {
            "mode": self.provider_mode,
            "online_configured": self._online is not None,
            "last_provider": self._last_provider,
            "last_error": self._last_error,
            "online": online,
            "web_search": bool(self._online is not None and self._online.search_available),
            "web_search_count": self._online.cfg.search_count if self._online is not None else 0,
            "local_worker": bool(self._proc is not None and self._proc.poll() is None),
        }

    # ---------------- 启动 / 关闭 ----------------
    def _model_path(self) -> str | None:
        root = settings.MULTIMODAL_ROOT
        if root is None:
            return None
        explicit = os.environ.get("MULTIMODAL_LLM_PATH")
        if explicit and Path(explicit).is_dir():
            return explicit
        for rel in self.cfg.get("model_candidates", []):
            cand = Path(root) / rel
            if cand.is_dir():
                return str(cand)
        return None

    def _pump(self, proc: subprocess.Popen[str], q: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.strip())
        q.put(None)

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        root = settings.MULTIMODAL_ROOT
        model = self._model_path()
        if root is None or model is None:
            return False
        worker = settings.PROJECT_ROOT / self.cfg["worker"]
        if not worker.exists():
            return False
        env = dict(os.environ)
        env["MM_ROOT"] = str(root)
        env["PYTHONPATH"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        if not os.getenv("INTEGRATED_MM_ONLINE"):
            env.setdefault("HF_HUB_OFFLINE", "1")
            env.setdefault("TRANSFORMERS_OFFLINE", "1")
        params = {"root": str(root), "model_path": model}
        try:
            proc = subprocess.Popen(
                [sys.executable, "-B", str(worker), json.dumps(params, ensure_ascii=False)],
                cwd=str(root),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except Exception:
            return False
        self._proc = proc
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, args=(proc, self._queue), daemon=True).start()
        self._started_at = time.time()
        deadline = time.time() + float(self.cfg["startup_timeout_s"])
        while time.time() < deadline:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    self.close()
                    return False
                continue
            if line is None:
                self.close()
                return False
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("event") == "ready":
                return True
            if msg.get("event") == "error":
                self.close()
                return False
        self.close()
        return False

    def close(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(5)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    # ---------------- 对话 ----------------
    def _send(self, payload: dict[str, Any]) -> Result:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "对话 worker 未启动")
        self._seq += 1
        payload["id"] = self._seq
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as exc:
            self.close()
            return fail(ErrorCode.INTERNAL_ERROR, f"对话请求写入失败: {exc}")

        deadline = time.time() + float(self.cfg["timeout_s"])
        while time.time() < deadline:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    self.close()
                    return fail(ErrorCode.MODALITY_UNAVAILABLE, "对话 worker 已退出")
                continue
            if line is None:
                self.close()
                return fail(ErrorCode.MODALITY_UNAVAILABLE, "对话 worker 已退出")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") != self._seq:
                continue
            if msg.get("ok"):
                if "outputs" in msg:
                    return ok({"outputs": msg.get("outputs") or [], "model": msg.get("model")})
                return ok(_to_simplified(str(msg.get("text") or "")))
            return fail(ErrorCode.INTERNAL_ERROR, str(msg.get("error") or "对话生成失败"))
        self.close()
        return fail(ErrorCode.TIMEOUT, "对话生成超时")

    def prewarm(self) -> None:
        """后台预加载模型，让首次聊天/LLM 解释不再等 ~25 秒。"""
        # 在线模式不需要加载本地权重；只有真正需要回退时才按需加载。
        if self.online_available:
            return
        if self._proc is not None:
            return
        threading.Thread(target=self._prewarm_locked, daemon=True).start()

    def _prewarm_locked(self) -> None:
        with self._lock:
            self._ensure()

    def complete_many(
        self,
        prompts: list[str],
        *,
        max_new_tokens: int = 120,
        temperature: float = 0.2,
    ) -> Result:
        """用同一个已加载模型批量生成（用于推荐解释，不写入聊天历史）。"""
        with self._lock:
            if self.online_available:
                assert self._online is not None
                online_result = self._online.complete_many(
                    prompts,
                    max_new_tokens=max_new_tokens,
                    temperature=temperature,
                )
                if online_result.ok:
                    self._last_provider = "online"
                    self._last_error = None
                    return online_result
                self._last_error = online_result.error.message if online_result.error else "在线 LLM 批量调用失败"
                if self._online.cfg.strict:
                    return online_result
            if not self._ensure():
                detail = f"；在线后端失败：{self._last_error}" if self._last_error else ""
                return fail(
                    ErrorCode.MODALITY_UNAVAILABLE,
                    "对话模型不可用（未找到权重或启动失败）" + detail,
                )
            result = self._send(
                {
                    "op": "prompts",
                    "prompts": list(prompts),
                    "max_new_tokens": int(max_new_tokens),
                    "temperature": float(temperature),
                }
            )
            self._last_provider = "local"
            return result

    def _should_search(self, text: str) -> bool:
        """只对时效性/检索型问题触发搜索，购物推荐不消耗搜索额度。"""
        lowered = str(text or "").lower()
        if not lowered:
            return False
        blocked = self.cfg.get("web_search_block_markers", [])
        if any(str(marker).lower() in lowered for marker in blocked):
            return False
        markers = self.cfg.get("web_search_markers", [])
        return any(str(marker).lower() in lowered for marker in markers)

    @staticmethod
    def _format_search_results(results: list[dict]) -> str:
        blocks: list[str] = []
        for index, row in enumerate(results, 1):
            title = str(row.get("title") or "")
            url = str(row.get("url") or "")
            site = str(row.get("site_name") or "")
            date = str(row.get("date") or "")
            summary = str(row.get("summary") or row.get("snippet") or "")
            content = str(row.get("content") or "")
            body = content[:3000] if content else summary[:1500]
            blocks.append(f"[{index}] {title}\n站点：{site}\n日期：{date}\nURL：{url}\n{body}")
        return "\n\n".join(blocks)

    def _search_context(self, text: str) -> tuple[str | None, dict | None]:
        """返回 (注入模型的上下文, 给前端的来源摘要)。"""
        if not self.online_available or self._online is None or not self._online.search_available:
            return None, None
        if not self._should_search(text):
            return None, None

        started = time.perf_counter()
        lowered = text.lower()
        url_match = re.search(r"https?://[^\s]+", text)
        if url_match and any(marker in lowered for marker in ("抓取", "抓一下", "fetch", "总结", "看看", "正文")):
            fetched = self._online.fetch_url(url_match.group(0))
            elapsed = round((time.perf_counter() - started) * 1000, 2)
            if fetched.ok:
                data = fetched.data or {}
                title = str(data.get("title") or "")
                url = str(data.get("url") or url_match.group(0))
                content = str(data.get("content") or "")
                context = f"[1] {title}\nURL：{url}\n{content[:6000]}"
                return context, {
                    "mode": "fetch",
                    "query": text,
                    "count": 1,
                    "latency_ms": elapsed,
                    "sources": [{"index": 1, "title": title, "url": url, "site_name": "", "date": ""}],
                }
            reason = fetched.error.message if fetched.error else "Fetch 失败"
            return None, {
                "mode": "fetch",
                "query": text,
                "count": 0,
                "latency_ms": elapsed,
                "sources": [],
                "error": reason,
            }

        need_content = any(marker in lowered for marker in ("详细", "全文", "原文", "正文", "文档", "内容"))
        result = self._online.search_web(text, need_content=need_content, ori_question=text)
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        if not result.ok:
            reason = result.error.message if result.error else "Search 失败"
            return None, {
                "mode": "search",
                "query": text,
                "count": 0,
                "latency_ms": elapsed,
                "sources": [],
                "error": reason,
            }

        data = result.data or {}
        results = list(data.get("results") or [])
        sources = [
            {
                "index": index,
                "title": str(row.get("title") or ""),
                "url": str(row.get("url") or ""),
                "site_name": str(row.get("site_name") or ""),
                "date": str(row.get("date") or ""),
                "authority": row.get("authority"),
            }
            for index, row in enumerate(results, 1)
        ]
        meta = {
            "mode": "search",
            "query": text,
            "count": len(results),
            "latency_ms": elapsed,
            "sources": sources,
        }
        if not results:
            return None, meta
        return self._format_search_results(results), meta

    def chat(self, text: str) -> Result:
        with self._lock:
            search_context, search_meta = self._search_context(text)
            if self.online_available:
                assert self._online is not None
                online_result = self._online.chat(text, context=search_context)
                if online_result.ok:
                    self._last_provider = "online"
                    self._last_error = None
                    answer = _to_simplified(str(online_result.data or ""))
                    if search_meta is not None:
                        return ok({"text": answer, "web_search": search_meta})
                    return ok(answer)
                self._last_error = online_result.error.message if online_result.error else "在线 LLM 调用失败"
                if self._online.cfg.strict:
                    return online_result
            if not self._ensure():
                detail = f"；在线后端失败：{self._last_error}" if self._last_error else ""
                return fail(
                    ErrorCode.MODALITY_UNAVAILABLE,
                    "对话模型不可用（未找到权重或启动失败）" + detail,
                )
            local_text = text
            if search_context:
                local_text = "【实时网页资料】\n" + search_context + "\n\n【用户问题】\n" + text
            result = self._send({"text": local_text})
            if result.ok:
                self._last_provider = "local"
                if search_meta is not None:
                    return ok(
                        {
                            "text": _to_simplified(str(result.data or "")),
                            "web_search": search_meta,
                        }
                    )
            return result

    def reset(self) -> None:
        """清空模型侧的多轮历史（切换用户 / 重开会话时调用）。"""
        with self._lock:
            if self._online is not None:
                self._online.reset()
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return
            self._seq += 1
            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps({"id": self._seq, "op": "reset"}) + "\n")
                proc.stdin.flush()
            except Exception:
                self.close()

    def stop(self) -> None:
        with self._lock:
            self.close()
