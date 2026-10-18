"""
src/camera_service.py
=====================

常驻摄像头 + 手势直播（客户端）。

- `start()` 拉起 `scripts/camera_worker.py`，摄像头保持打开；
- worker 持续写 `latest.jpg` 与 `state.json`；
- `state()` 供网页轮询当前手势/事件；
- `frame()` 供 `/camera/stream` 输出 MJPEG；
- `stop()` 释放摄像头。
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

from config import settings

from .errors import ErrorCode, Result, fail, ok


class CameraService:
    def __init__(self, cfg: dict | None = None) -> None:
        self.cfg = cfg or settings.CAMERA_CONFIG
        self.out_dir = settings.PATHS["artifacts"] / "camera"
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._proc: subprocess.Popen[str] | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._lock = threading.Lock()
        self.last_error = ""

    def _pump(self, proc: subprocess.Popen[str], q: queue.Queue[str | None]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            q.put(line.strip())
        q.put(None)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _ensure(self) -> bool:
        if self.running:
            return True
        root = settings.MULTIMODAL_ROOT
        if root is None:
            self.last_error = "未找到 multimodal_ai 根目录"
            return False
        worker = settings.PROJECT_ROOT / self.cfg["worker"]
        if not worker.exists():
            self.last_error = f"缺少 worker: {worker}"
            return False
        env = dict(os.environ)
        env["MM_ROOT"] = str(root)
        env["PYTHONPATH"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        if not os.getenv("INTEGRATED_MM_ONLINE"):
            env.setdefault("HF_HUB_OFFLINE", "1")
            env.setdefault("TRANSFORMERS_OFFLINE", "1")
        params = {
            "root": str(root),
            "out_dir": str(self.out_dir),
            "camera_index": int(self.cfg.get("camera_index", 0)),
            "width": int(self.cfg.get("width", 640)),
            "height": int(self.cfg.get("height", 480)),
            "fps": float(self.cfg.get("fps", 12)),
        }
        # 清掉上一轮残留，避免 start() 立刻返回旧的 event_id / running=false
        with contextlib.suppress(Exception):
            (self.out_dir / "state.json").unlink()
        with contextlib.suppress(Exception):
            (self.out_dir / "latest.jpg").unlink()
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
        except Exception as exc:
            self.last_error = f"摄像头 worker 启动失败: {exc}"
            return False
        self._proc = proc
        self._queue = queue.Queue()
        threading.Thread(target=self._pump, args=(proc, self._queue), daemon=True).start()
        deadline = time.time() + float(self.cfg.get("startup_timeout_s", 30.0))
        while time.time() < deadline:
            try:
                line = self._queue.get(timeout=1.0)
            except queue.Empty:
                if proc.poll() is not None:
                    self._close_proc()
                    return False
                continue
            if line is None:
                self._close_proc()
                return False
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("event") == "ready":
                return True
            if msg.get("event") == "error":
                self.last_error = str(msg.get("error") or "摄像头启动失败")
                self._close_proc()
                return False
        self.last_error = "摄像头启动超时"
        self._close_proc()
        return False

    def _close_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.terminate()
        try:
            proc.wait(3)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    def start(self) -> Result:
        with self._lock:
            if self._ensure():
                return ok(self.state())
            return fail(ErrorCode.MODALITY_UNAVAILABLE, self.last_error or "摄像头不可用")

    def stop(self) -> None:
        with self._lock:
            proc = self._proc
            if proc is not None and proc.stdin is not None:
                self._proc = None
                with contextlib.suppress(Exception):
                    proc.stdin.write(json.dumps({"op": "stop"}) + "\n")
                    proc.stdin.flush()
                try:
                    proc.wait(3)
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()
                return
            self._close_proc()

    def state(self) -> dict:
        path = self.out_dir / "state.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["running"] = self.running and bool(data.get("running"))
            return data
        except Exception:
            return {"running": self.running, "current_gesture": None, "event_id": 0}

    def frame(self) -> bytes | None:
        path = self.out_dir / "latest.jpg"
        try:
            return path.read_bytes()
        except Exception:
            return None
