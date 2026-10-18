"""
src/asr_service.py
==================

常驻 ASR 客户端（SenseVoiceSmall）。

- 懒启动：第一次语音输入才拉起 `scripts/asr_worker.py`
- 复用：之后所有语音走同一进程（模型只加载一次）
- 隔离：cwd/PYTHONPATH 指向 multimodal_ai，避免两个项目的 config/src 冲突
- 失败可恢复：worker 退出后下次请求自动重启
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from config import settings

from .errors import ErrorCode, Result, fail, ok


class AsrService:
    """常驻语音识别客户端。线程安全（同一时刻只处理一条音频）。"""

    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or settings.ASR_CONFIG
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self._seq = 0

    def _pump(self, proc: subprocess.Popen[str], q: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.strip())
        q.put(None)

    def _ensure(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        root = settings.MULTIMODAL_ROOT
        if root is None:
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
        try:
            proc = subprocess.Popen(
                [sys.executable, "-B", str(worker), json.dumps({"root": str(root)}, ensure_ascii=False)],
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

    def _send(self, payload: dict[str, Any]) -> Result:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "ASR worker 未启动")
        self._seq += 1
        payload["id"] = self._seq
        try:
            proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        except Exception as exc:
            self.close()
            return fail(ErrorCode.INTERNAL_ERROR, f"ASR 请求写入失败: {exc}")

        deadline = time.time() + float(self.cfg["timeout_s"])
        while time.time() < deadline:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    self.close()
                    return fail(ErrorCode.MODALITY_UNAVAILABLE, "ASR worker 已退出")
                continue
            if line is None:
                self.close()
                return fail(ErrorCode.MODALITY_UNAVAILABLE, "ASR worker 已退出")
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") != self._seq:
                continue
            if msg.get("ok"):
                return ok({"text": str(msg.get("text") or "")})
            return fail(ErrorCode.INTERNAL_ERROR, str(msg.get("error") or "语音识别失败"))
        self.close()
        return fail(ErrorCode.TIMEOUT, "语音识别超时")

    def prewarm(self) -> None:
        """后台预加载模型，避免第一次语音输入再等 ~25 秒。"""
        if self._proc is not None:
            return
        threading.Thread(target=self._prewarm_locked, daemon=True).start()

    def _prewarm_locked(self) -> None:
        with self._lock:
            self._ensure()

    def transcribe(self, audio_path: str, sample_rate: int = 16000) -> Result:
        with self._lock:
            if not self._ensure():
                return fail(ErrorCode.MODALITY_UNAVAILABLE, "语音模型不可用（启动失败）")
            return self._send({"audio": str(Path(audio_path).resolve()), "sample_rate": int(sample_rate)})

    def stop(self) -> None:
        with self._lock:
            self.close()
