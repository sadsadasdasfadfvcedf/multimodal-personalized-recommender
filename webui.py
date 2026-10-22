"""
webui.py —— 多模态个性化推荐助手 · 网页对话界面
=================================================

在浏览器里和系统对话（不再是终端，也不再是写死的脚本）。

零新增依赖：只用 Python 标准库 http.server 起服务，因此保持项目「离线可跑」的原则。
（对比：推荐系统前端用的是 FastAPI，需要额外装包；本页面不需要。）

页面能力：
    文本对话      输入框直接问
    🎤 语音      点按钮录 4 秒真实麦克风 → ASR → 对话（需 --real）
    ✋ 手势      商品详情内自动开启手势直播，👍→点赞、👎→倒赞（需 --real）
    📷 识图推荐  识别画面中的主要物品并更新商品推荐（需 --real）
    ↩ 撤销 / ⟳ 重来 / 会话状态 / LLM 开关

用法：
    python webui.py                      # http://127.0.0.1:8020
    python webui.py --real               # 启用麦克风/摄像头/真实模型
    python webui.py --real --llm         # 再用真实 LLM 生成解释
    python webui.py --port 8020 --user 14156
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
WEB_ROOT = PROJECT_ROOT / "web"

from config import settings  # noqa: E402
from src.errors import ErrorCode  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.presentation import synthesize  # noqa: E402
from src.utils import contains_any, ensure_utf8_stdio  # noqa: E402
from src.vision_api import DEFAULT_VISION_QUESTION, DashScopeVisionClient  # noqa: E402

# ------------------------------------------------------------
# 商品图片：代理外部推荐系统的 /api/image/{id} 并本地缓存
# ------------------------------------------------------------
_MIME_EXT = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_EXT_MIME = {v: k for k, v in _MIME_EXT.items()}
_ALLOWED_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
_PLACEHOLDER_IMAGE = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirectHandler)


def _valid_item_id(item_id: str) -> bool:
    return bool(_ITEM_ID_RE.fullmatch(str(item_id or "")))


def _sniff_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_cache_path(cache_dir: Path, item_id: str, ext: str) -> Path:
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()
    target = (cache_dir / f"{digest}{ext}").resolve()
    if not target.is_relative_to(cache_dir.resolve()):
        raise ValueError("图片缓存路径越界")
    return target


def _recsys_image_base() -> str:
    return str(settings.RECOMMENDER_ADAPTER["http"]["base_url"]).rstrip("/")


def fetch_product_image(item_id: str, timeout_s: float | None = None) -> tuple[bytes, str] | None:
    """取商品图：先查本地缓存，再代理推荐系统接口；都拿不到返回 None。

    参考推荐系统的 /api/image/{item_id} 是**公开接口**（无鉴权），
    因此代理它不违反「不读对方内部变量」的边界约定；
    本地缓存让推荐系统未启动时图片依然可显示。
    """
    item_id = str(item_id).strip()
    if not _valid_item_id(item_id):
        return None
    cache_dir = settings.PATHS["images"]
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1) 本地缓存
    for ext, _mime in _EXT_MIME.items():
        cached = _image_cache_path(cache_dir, item_id, ext)
        if cached.exists() and cached.stat().st_size > 0:
            age_days = (time.time() - cached.stat().st_mtime) / 86400
            if age_days <= settings.IMAGE_CONFIG["recache_days"]:
                data = cached.read_bytes()
                detected = _sniff_image_mime(data)
                if detected in _ALLOWED_IMAGE_TYPES:
                    return data, detected

    # 2) 代理推荐系统
    url = _recsys_image_base() + "/api/image/" + urllib.parse.quote(item_id)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "integrated-ai-project"})
        with _NO_REDIRECT_OPENER.open(req, timeout=timeout_s or settings.IMAGE_CONFIG["timeout_s"]) as resp:
            if resp.status != 200:
                return None
            data = resp.read(settings.IMAGE_CONFIG["max_bytes"] + 1)
    except Exception:
        return None
    if not data or len(data) > settings.IMAGE_CONFIG["max_bytes"]:
        return None
    ctype = _sniff_image_mime(data)
    if ctype not in _ALLOWED_IMAGE_TYPES:
        return None
    ext = _MIME_EXT[ctype]
    with contextlib.suppress(OSError):
        _image_cache_path(cache_dir, item_id, ext).write_bytes(data)
    return data, ctype


def category_emoji(category: str) -> str:
    table = settings.IMAGE_CONFIG["category_emoji"]
    return table.get(str(category or ""), settings.IMAGE_CONFIG["default_emoji"])


_DEP_HINTS = (
    ("GetPrototype", "protobuf 版本过高：mediapipe 0.10.14 要求 protobuf>=4.25.3,<5"),
    ("FieldDescriptor", "protobuf 版本过高：mediapipe 0.10.14 要求 protobuf>=4.25.3,<5"),
    ("torchvision::nms", "torchvision 与 torch 不配套：0.21.0 对应 torch 2.6.0"),
    ("AutoProcessor", "transformers 的 AutoProcessor 依赖可用的 torchvision，请先修 torchvision"),
)

_OBJECT_VISION_QUESTION = DEFAULT_VISION_QUESTION


def _dep_hint(message: str) -> str | None:
    low = (message or "").lower()
    for needle, hint in _DEP_HINTS:
        if needle.lower() in low:
            return hint
    return None


def _err(
    code: ErrorCode | str,
    message: str,
    *,
    field: str | None = None,
    hint: str | None = None,
    **extra: object,
) -> dict:
    """统一错误响应：保留前端依赖的 error 字符串，同时补齐协议要求的 code/field。"""
    payload: dict = {"ok": False, "error": message, "code": str(code), "field": field}
    if hint:
        payload["hint"] = hint
    payload.update(extra)
    return payload


def _gesture_preview_url(rows: list[dict], name: str | None, burst_dir: Path, media: Path) -> str | None:
    """选出最佳帧并生成网页可访问的预览地址。"""
    if not rows:
        return None
    best = rows[0]
    if name:
        matched = [r for r in rows if any(g.get("name") == name for g in (r.get("gestures") or []))]
        if matched:
            best = max(
                matched,
                key=lambda r: max(
                    (float(g.get("confidence") or 0.0) for g in (r.get("gestures") or []) if g.get("name") == name),
                    default=0.0,
                ),
            )
    image = str(best.get("image") or "")
    if not image:
        return None
    p = Path(image)
    if p.is_absolute():
        try:
            rel = p.resolve().relative_to(media.resolve())
        except ValueError:
            rel = Path(burst_dir.name) / p.name
    else:
        rel = Path(burst_dir.name) / p.name
    return "/capture/" + urllib.parse.quote(rel.as_posix())


def _is_feedback_gesture(name: str | None) -> bool:
    """页面只接受点赞/倒赞，其他识别结果一律忽略。"""
    return bool(name and name in settings.MULTIMODAL_ADAPTER["gesture"]["feedback_map"])


def _pick_gesture(rows: list[dict]) -> tuple[str | None, float, dict[str, int], int]:
    """从连拍识别结果中按「票数 → 最高置信度」选最佳手势。"""
    votes: dict[str, int] = {}
    confs: dict[str, float] = {}
    hands = 0
    for row in rows:
        hands += int(row.get("hands_detected") or 0)
        for item in row.get("gestures") or []:
            name = str(item.get("name") or "")
            if not name:
                continue
            votes[name] = votes.get(name, 0) + 1
            confs[name] = max(confs.get(name, 0.0), float(item.get("confidence") or 0.0))
    if not votes:
        return None, 0.0, {}, hands
    name = max(votes, key=lambda n: (votes[n], confs[n]))
    return name, confs[name], votes, hands


def _from_app_error(err) -> dict:
    if err is None:
        return _err(ErrorCode.INTERNAL_ERROR, "未知错误")
    return _err(err.code, err.message, field=err.field)


class App:
    """承载 Orchestrator 与全局会话状态（加锁串行化，避免并发调用）。"""

    def __init__(
        self, user_id: str, mode: str | None, with_voice: bool, use_llm: bool, allow_real: bool | None = None
    ) -> None:
        # RLock：临时覆盖运行时开关时允许安全地嵌套 handle_spec。
        self.lock = threading.RLock()
        self.rate_lock = threading.Lock()
        self._rate_calls: dict[str, list[float]] = {}
        self.user_id = user_id
        # allow_real 必须**显式传参**：settings.RUNTIME 在 import 时即已求值，
        # 之后再 os.environ[...]="1" 不会生效（这是实测踩到的坑）。
        self.orch = Orchestrator(mode=mode, allow_real=allow_real, with_voice=with_voice, use_llm=use_llm)
        self.vision_api = DashScopeVisionClient.from_env()
        self._vision_cache: dict[str, tuple[float, dict]] = {}
        self._vision_calls: list[float] = []
        _asr = getattr(self.orch, "asr", None)
        if _asr is not None:
            _asr.prewarm()
        _chat = getattr(self.orch, "chat", None)
        if _chat is not None:
            _chat.prewarm()
        self.session_id = self.orch.store.default_session_id(user_id)
        self.with_voice = with_voice
        self.media = settings.PATHS["artifacts"] / "webui"
        self.media.mkdir(parents=True, exist_ok=True)
        self.turn = 0

    @contextlib.contextmanager
    def _runtime_flags(
        self,
        *,
        llm_enabled: bool | None = None,
        dry_run_voice: bool | None = None,
    ):
        """在锁内临时覆盖共享开关，退出时无条件恢复。"""
        with self.lock:
            prev_llm = self.orch.llm_explainer.enabled
            prev_dry = self.orch.dry_run_voice
            if llm_enabled is not None:
                self.orch.llm_explainer.enabled = bool(llm_enabled)
            if dry_run_voice is not None:
                self.orch.dry_run_voice = bool(dry_run_voice)
            try:
                yield
            finally:
                self.orch.llm_explainer.enabled = prev_llm
                self.orch.dry_run_voice = prev_dry

    def _consume_rate(self, key: str, limit: int, window_s: float) -> tuple[bool, int]:
        now = time.monotonic()
        with self.rate_lock:
            calls = [stamp for stamp in self._rate_calls.get(key, []) if now - stamp < window_s]
            if len(calls) >= limit:
                retry_after = max(1, int(window_s - (now - calls[0])))
                self._rate_calls[key] = calls
                return False, retry_after
            calls.append(now)
            self._rate_calls[key] = calls
        return True, 0

    def rate_limit(self, path: str, client_ip: str) -> tuple[bool, int]:
        security = settings.WEB_SECURITY_CONFIG
        per_path = security["rate_limits"].get(path)
        if not per_path:
            return True, 0
        limit, window_s = per_path
        allowed, retry = self._consume_rate(f"{path}:{client_ip}", int(limit), float(window_s))
        if not allowed:
            return False, retry
        if path in security["paid_paths"]:
            limit, window_s = security["global_paid_limit"]
            return self._consume_rate("global-paid", int(limit), float(window_s))
        return True, 0

    def account(self) -> dict:
        for item in settings.DEMO_ACCOUNTS:
            if item["user_id"] == self.user_id:
                return item
        return {"user_id": self.user_id, "name": self.user_id, "persona": "", "color": "#6d5dfc"}

    def _handle_spec_fast(self, spec: dict, top_k: int | None = None) -> dict:
        """多模态快捷入口不等待 LLM 批量改写解释，先快速返回商品结果。"""
        with self._runtime_flags(llm_enabled=False):
            return self.handle_spec(spec, top_k=top_k)

    def _vision_cache_get(self, key: str) -> dict | None:
        now = time.monotonic()
        with self.lock:
            expired = [cache_key for cache_key, (until, _) in self._vision_cache.items() if until <= now]
            for cache_key in expired:
                self._vision_cache.pop(cache_key, None)
            cached = self._vision_cache.get(key)
            return dict(cached[1]) if cached is not None else None

    def _vision_cache_put(self, key: str, data: dict) -> None:
        ttl = max(0.0, float(settings.VISION_API_CONFIG["cache_ttl_s"]))
        limit = max(1, int(settings.VISION_API_CONFIG["cache_max_entries"]))
        if ttl <= 0:
            return
        with self.lock:
            self._vision_cache[key] = (time.monotonic() + ttl, dict(data))
            while len(self._vision_cache) > limit:
                oldest = next(iter(self._vision_cache))
                self._vision_cache.pop(oldest, None)

    def _vision_rate_allow(self) -> tuple[bool, int]:
        now = time.monotonic()
        window = max(1.0, float(settings.VISION_API_CONFIG["rate_window_s"]))
        limit = max(1, int(settings.VISION_API_CONFIG["rate_limit_per_minute"]))
        with self.lock:
            self._vision_calls = [stamp for stamp in self._vision_calls if now - stamp < window]
            if len(self._vision_calls) >= limit:
                retry_after = max(1, int(window - (now - self._vision_calls[0])))
                return False, retry_after
            self._vision_calls.append(now)
        return True, 0

    @staticmethod
    def _attach_vision_trace(
        out: dict,
        vision_meta: dict,
        *,
        cache_key: str,
        fallback: bool,
    ) -> None:
        response = out.get("response")
        if not isinstance(response, dict):
            return
        trace = response.setdefault("trace", {})
        if not isinstance(trace, dict):
            trace = {}
            response["trace"] = trace
        modes = trace.setdefault("modes", {})
        if not isinstance(modes, dict):
            modes = {}
            trace["modes"] = modes
        latency_ms = trace.setdefault("latency_ms", {})
        if not isinstance(latency_ms, dict):
            latency_ms = {}
            trace["latency_ms"] = latency_ms
        modes["vision"] = vision_meta.get("vision_provider") or "local"
        modes["vision_model"] = vision_meta.get("vision_model") or ""
        latency_ms["vision_ms"] = vision_meta.get("vision_latency_ms")
        trace["vision"] = {
            "provider": vision_meta.get("vision_provider") or "local",
            "model": vision_meta.get("vision_model") or "",
            "latency_ms": vision_meta.get("vision_latency_ms"),
            "cache_hit": bool(vision_meta.get("vision_cache_hit")),
            "cache_key": cache_key[:12],
            "fallback": fallback,
            "online_error": vision_meta.get("vision_online_error"),
        }
        if fallback:
            degradations = trace.setdefault("degradations", {})
            if not isinstance(degradations, dict):
                degradations = {}
                trace["degradations"] = degradations
            degradations["vision"] = {
                "adapter": "vision",
                "level": "L1",
                "from": "online",
                "to": "local",
                "reason": f"在线识图失败，已回退本地 Qwen2-VL: {vision_meta.get('vision_online_error') or ''}",
            }

    def camera_start(self) -> dict:
        with self.lock:
            camera = getattr(self.orch, "camera", None)
            if camera is None:
                return _err(ErrorCode.MODALITY_UNAVAILABLE, "手势直播需要 --real 模式")
            result = camera.start()
        if not result.ok:
            return _from_app_error(result.error)
        return {"ok": True, "state": result.data}

    def camera_stop(self) -> dict:
        with self.lock:
            camera = getattr(self.orch, "camera", None)
            if camera is not None:
                camera.stop()
        return {"ok": True, "state": {"running": False}}

    def camera_state(self) -> dict:
        with self.lock:
            camera = getattr(self.orch, "camera", None)
            if camera is None:
                return {"ok": True, "state": {"running": False}}
            state = camera.state()
        return {"ok": True, "state": state}

    def switch_user(self, user_id: str) -> dict:
        """切换演示账号：结束旧会话，换新 user_id 的默认会话。"""
        user_id = str(user_id).strip()
        target = next((a for a in settings.DEMO_ACCOUNTS if a["user_id"] == user_id), None)
        if target is None:
            return _err(ErrorCode.INVALID_INPUT, f"未知演示账号：{user_id}", field="user_id")
        _camera = getattr(self.orch, "camera", None)
        if _camera is not None:
            _camera.stop()
        with self.lock:
            self.orch.store.end(self.session_id)
            self.user_id = user_id
            self.session_id = self.orch.store.default_session_id(user_id)
            self.turn = 0
            _chat = getattr(self.orch, "chat", None)
            if _chat is not None:
                _chat.reset()
        return {"ok": True, "account": target, "session_id": self.session_id}

    def handle_spec(self, spec: dict, top_k: int | None = None) -> dict:
        with self.lock:
            self.turn += 1
            # 网页默认按需合成语音：每次推荐都等 TTS 会让 0.2s 的推荐变成 2~3s。
            with self._runtime_flags(dry_run_voice=True):
                res = self.orch.handle(spec, user_id=self.user_id, session_id=self.session_id, top_k=top_k)
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        # 给每条推荐注入图片地址与类目兜底图标，前端不必自己拼 URL
        for item in resp.get("recommendations", []):
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        pres = resp.get("presentation") or {}
        path = pres.get("audio_path")
        if pres.get("voice_available") and path:
            pres["audio_url"] = "/audio/" + Path(path).name
        return {"ok": True, "response": resp}

    def tts(self, text: str) -> dict:
        """按需合成语音；网页点「语音播报」时才调用，避免阻塞推荐响应。"""
        text = str(text or "").strip()
        if not text:
            return _err(ErrorCode.INVALID_INPUT, "待合成文本为空", field="text")
        max_chars = int(settings.WEB_SECURITY_CONFIG["max_tts_chars"])
        if len(text) > max_chars:
            return _err(
                ErrorCode.INVALID_INPUT,
                f"待合成文本过长，最多 {max_chars} 个字符",
                field="text",
            )
        tmp_dir = settings.PATHS["artifacts"] / "tts" / uuid.uuid4().hex
        try:
            result = synthesize(text, out_dir=tmp_dir)
            if not result.get("available"):
                return _err(ErrorCode.MODALITY_UNAVAILABLE, str(result.get("error") or "语音合成不可用"))
            src = Path(str(result["path"]))
            name = f"tts_{uuid.uuid4().hex}{src.suffix or '.wav'}"
            target = settings.PATHS["artifacts"] / name
            shutil.copyfile(src, target)
            return {"ok": True, "audio_url": "/audio/" + name, "engine": result.get("engine")}
        except Exception as exc:
            return _err(ErrorCode.INTERNAL_ERROR, f"语音合成失败: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def chat(self, text: str, top_k: int | None = None, focus_item_id: str | None = None) -> dict:
        text = str(text or "")
        max_chars = int(settings.WEB_SECURITY_CONFIG["max_chat_chars"])
        if len(text) > max_chars:
            return _err(
                ErrorCode.INVALID_INPUT,
                f"输入文本过长，最多 {max_chars} 个字符",
                field="text",
            )
        focus = str(focus_item_id or "").strip()
        if focus and self.orch.catalog.exists(focus):
            with self.lock:
                session = self.orch.store.get_or_create(self.session_id, self.user_id)
                session.focus_item_id = focus
                if focus not in session.last_item_ids:
                    session.last_item_ids = [focus, *session.last_item_ids]
        return self.handle_spec({"source": "text", "text": text}, top_k=top_k)

    def voice(self, seconds: float, top_k: int | None = None) -> dict:
        seconds = max(1.0, min(10.0, float(seconds)))  # 防止负数/超长录音拖死服务
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "语音输入需要 --real 模式")
        wav = self.media / f"voice_{uuid.uuid4().hex}.wav"
        rec = ad.record_audio(str(wav), seconds=seconds)
        if not rec.ok:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"录音失败: {rec.error}")
        asr_service = getattr(self.orch, "asr", None)
        asr = (
            asr_service.transcribe(str(wav), sample_rate=16000) if asr_service is not None else ad.transcribe(str(wav))
        )
        if not asr.ok:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"识别失败: {asr.error}")
        text = (asr.data or {}).get("text") or ""
        if not text.strip():
            return _err(ErrorCode.EMPTY_TRANSCRIPT, "未识别到有效语音", field="text", transcript="")
        if contains_any(text, settings.MULTIMODAL_ADAPTER.get("voice_visual_markers", [])):
            out = self.vision(text, top_k=top_k)
            out["transcript"] = text
            out["input_modes"] = ["voice", "vision"]
            response = out.get("response")
            if isinstance(response, dict):
                event = response.get("event")
                if isinstance(event, dict):
                    event["source"] = "voice"
                    event["modality"] = "multimodal"
                trace = response.get("trace")
                if isinstance(trace, dict):
                    modes = trace.get("modes")
                    if isinstance(modes, dict):
                        modes["asr"] = "real"
            return out
        out = self.handle_spec({"source": "voice", "text": text}, top_k=top_k)
        out["transcript"] = text
        out["input_modes"] = ["voice"]
        return out

    def _capture_vision_frame(self, target: Path) -> tuple[bool, str]:
        """摄像头 worker 已运行时复用其帧；未运行时直接抓一帧，避免 10 秒启动等待。"""
        camera = getattr(self.orch, "camera", None)
        if camera is not None and getattr(camera, "running", False):
            deadline = time.time() + 3.0
            while time.time() < deadline:
                data = camera.frame()
                if data:
                    try:
                        target.write_bytes(data)
                        return True, ""
                    except OSError as exc:
                        return False, f"写入抓帧文件失败: {exc}"
                time.sleep(0.1)
            return False, "摄像头直播运行中，但暂时没有可用画面"

        # 商品详情未打开时 worker 通常是停止的；直接抓帧比启动完整 worker 快得多。
        cap = self.orch.multimodal.capture_frame(str(target), warmup_frames=8)
        if cap.ok:
            return True, ""

        # 直接抓帧失败时再启动 worker，保留设备冷启动兜底。
        if camera is not None:
            started = camera.start()
            if started.ok:
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    data = camera.frame()
                    if data:
                        try:
                            target.write_bytes(data)
                            return True, ""
                        except OSError as exc:
                            return False, f"写入抓帧文件失败: {exc}"
                    time.sleep(0.1)
        return False, str(cap.error or "摄像头抓帧失败")

    def gesture(
        self,
        item_id: str | None = None,
        top_k: int | None = None,
        apply_feedback: bool = True,
    ) -> dict:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "手势识别需要 --real 模式")
        if item_id:
            with self.lock:
                session = self.orch.store.get(self.session_id)
                if session is not None:
                    session.focus_item_id = str(item_id)
                    if str(item_id) not in session.last_item_ids:
                        session.last_item_ids = [str(item_id), *session.last_item_ids]
        cfg = settings.MULTIMODAL_ADAPTER["gesture"]
        count = max(1, int(cfg.get("capture_frames", 5)))
        interval = max(0, int(cfg.get("capture_interval_ms", 250)))
        request_token = uuid.uuid4().hex
        burst_dir = self.media / f"gesture_{request_token}"
        cap = ad.capture_burst(str(burst_dir), count=count, interval_ms=interval)
        rows: list[dict] = []
        if cap.ok:
            g = ad.recognize_gesture_batch(str(burst_dir / "*.jpg"), limit=count, lenient=True)
        else:
            # 连拍不可用时退回单帧，保证功能可用
            frame = self.media / f"gesture_{request_token}.jpg"
            cap = ad.capture_frame(str(frame))
            if not cap.ok:
                return _err(ErrorCode.MODALITY_UNAVAILABLE, f"抓帧失败: {cap.error}")
            g = ad.recognize_gesture(str(frame), lenient=True)
            if g.ok:
                rows = [dict(g.data or {})]
        if not g.ok:
            msg = f"手势识别失败: {g.error}"
            hint = _dep_hint(msg)
            return _err(ErrorCode.MODALITY_UNAVAILABLE, msg + (f"（{hint}）" if hint else ""), hint=hint)
        if not rows:
            rows = list((g.data or {}).get("results") or [])
        name, confidence, votes, hands = _pick_gesture(rows)
        preview_url = _gesture_preview_url(rows, name, burst_dir, self.media)
        if name is None:
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                f"未确认出手势（已抓取 {len(rows)} 帧，检测到手 {hands} 次）。"
                "请等倒计时结束后把手举到脸旁边（画面中央），保持手势约 1 秒。",
                preview_url=preview_url,
            )
        min_votes = max(1, int(cfg.get("min_votes", 3)))
        if votes.get(name, 0) < min_votes:
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                f"手势结果不稳定（{name} 仅 {votes.get(name, 0)}/{len(rows)} 帧，"
                f"至少需要 {min_votes} 帧），请保持手势约 1 秒再试。",
                preview_url=preview_url,
            )
        extra = {
            "gesture": name,
            "gesture_confidence": round(confidence, 4),
            "gesture_frames": len(rows),
            "gesture_votes": votes,
            "preview_url": preview_url,
        }
        if not _is_feedback_gesture(name):
            return _err(
                ErrorCode.INVALID_INPUT,
                f"当前只支持 👍 点赞 / 👎 倒赞，检测到「{name}」已忽略。",
                field="gesture",
                gesture=name,
                gesture_confidence=round(confidence, 4),
                gesture_frames=len(rows),
                gesture_votes=votes,
                preview_url=preview_url,
            )
        if not apply_feedback:
            return {
                "ok": True,
                "gesture": name,
                "gesture_confidence": round(confidence, 4),
                "gesture_frames": len(rows),
                "gesture_votes": votes,
                "preview_url": preview_url,
                "feedback_applied": False,
                "requires_confirmation": name == "thumbs_down",
            }
        out = self._handle_spec_fast(
            {"source": "gesture", "gesture": {"name": name, "confidence": confidence}},
            top_k=top_k,
        )
        out.update(extra)
        return out

    def vision(self, question: str, top_k: int | None = None) -> dict:
        ad = self.orch.multimodal
        if ad.active_mode != "real":
            return _err(ErrorCode.MODALITY_UNAVAILABLE, "视觉理解需要 --real 模式")
        frame = self.media / f"vision_{uuid.uuid4().hex}.jpg"
        captured, capture_error = self._capture_vision_frame(frame)
        if not captured:
            return _err(ErrorCode.MODALITY_UNAVAILABLE, f"抓帧失败: {capture_error}")
        question_text = (question or "").strip() or None
        vision_question = _OBJECT_VISION_QUESTION
        try:
            image_bytes = frame.read_bytes()
        except OSError as exc:
            return _err(ErrorCode.INTERNAL_ERROR, f"读取抓帧文件失败: {exc}", field="image")
        cache_key = hashlib.sha256(image_bytes + b"\x1f" + vision_question.encode("utf-8")).hexdigest()
        cached = self._vision_cache_get(cache_key)
        online_error: str | None = None
        fallback = False
        if cached is not None:
            source_latency = cached.get("latency_ms")
            vision_data = dict(cached)
            vision_data["source_latency_ms"] = source_latency
            vision_data["latency_ms"] = 0.0
            vision_data["cache_hit"] = True
            fallback = bool(vision_data.get("fallback"))
            online_error = str(vision_data.get("online_error") or "") or None
        else:
            allowed, retry_after = self._vision_rate_allow()
            if not allowed:
                return _err(
                    ErrorCode.INVALID_INPUT,
                    f"识图请求过于频繁，请约 {retry_after} 秒后再试",
                    field="vision",
                    retry_after_s=retry_after,
                )
            vl = None
            if self.vision_api is not None:
                vl = self.vision_api.describe_image(str(frame), vision_question)
                if not vl.ok:
                    online_error = str(vl.error or "在线识图失败")
                    if not settings.VISION_API_CONFIG["fallback_to_local"]:
                        return _err(ErrorCode.MODALITY_UNAVAILABLE, f"在线识图失败: {online_error}")
            if vl is None or not vl.ok:
                fallback = bool(online_error)
                local_started = time.perf_counter()
                vl = ad.describe_image(str(frame), vision_question)
                if vl.ok and isinstance(vl.data, dict):
                    vl.data["provider"] = "local"
                    vl.data["latency_ms"] = round((time.perf_counter() - local_started) * 1000, 1)
            if not vl.ok:
                msg = f"视觉理解失败: {vl.error}"
                hint = _dep_hint(msg)
                if online_error:
                    msg += f"；在线识图失败并已尝试本地回退: {online_error}"
                return _err(
                    ErrorCode.MODALITY_UNAVAILABLE,
                    msg + (f"（{hint}）" if hint else ""),
                    hint=hint,
                    online_error=online_error,
                )
            vision_data = dict(vl.data or {})
            vision_data.setdefault("provider", "local")
            vision_data.setdefault("model", "")
            vision_data["cache_hit"] = False
            vision_data["fallback"] = fallback
            vision_data["online_error"] = online_error
            if str(vision_data.get("response") or "").strip():
                self._vision_cache_put(cache_key, vision_data)

        desc = vision_data.get("response") or ""
        vision_meta = {
            "vision_provider": vision_data.get("provider") or "local",
            "vision_model": vision_data.get("model") or "",
            "vision_latency_ms": vision_data.get("latency_ms"),
            "vision_cache_hit": bool(vision_data.get("cache_hit")),
            "vision_online_error": online_error,
        }
        if not str(desc).strip():
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                "没有识别出画面中的主要物品。请把物品举到画面中央并保持清晰后重试。",
                description=desc,
                vision_question=vision_question,
                vision_provider=vision_meta["vision_provider"],
                vision_model=vision_meta["vision_model"],
                vision_latency_ms=vision_meta["vision_latency_ms"],
                vision_cache_hit=vision_meta["vision_cache_hit"],
                vision_online_error=vision_meta["vision_online_error"],
            )
        # 新一轮“手上的物品”识图不应继承上一轮商品指代/相似约束。
        # 用户长期点赞/倒赞偏好仍保留，仅清理当前轮的槽位与约束。
        with self.lock:
            session = self.orch.store.get_or_create(self.session_id, self.user_id)
            session.slot_state.clear()
            session.constraints.clear()
            session.focus_item_id = None
            session.pending_clarification = None
        out = self._handle_spec_fast(
            # 没有提问时 text 必须传 None：传空串会被 parse_event 当成「空 ASR 文本」
            # 直接返回 E_EMPTY_TRANSCRIPT，视觉标签根本走不到（modality=visual 分支）。
            {
                "source": "vision",
                "text": question_text,
                "visual_tags": [{"tag": desc[:64], "confidence": 0.85}],
            },
            top_k=top_k,
        )
        out["description"] = desc
        out["vision_question"] = vision_question
        out["user_question"] = question_text
        out.update(vision_meta)
        self._attach_vision_trace(out, vision_meta, cache_key=cache_key, fallback=fallback)
        slots = ((out.get("response") or {}).get("intent_result") or {}).get("slots") or {}
        if not slots.get("category"):
            return _err(
                ErrorCode.LOW_CONFIDENCE_MODALITY,
                f"识别到「{desc}」，但未能确定商品品类。请把物品放近、避免遮挡后重试。",
                description=desc,
                vision_question=vision_question,
                vision_provider=vision_meta["vision_provider"],
                vision_model=vision_meta["vision_model"],
                vision_latency_ms=vision_meta["vision_latency_ms"],
                vision_cache_hit=vision_meta["vision_cache_hit"],
                vision_online_error=vision_meta["vision_online_error"],
            )
        return out

    def control(self, action: str) -> dict:
        if action == "undo":
            return self.handle_spec({"source": "text", "text": "撤销"})
        if action == "reset":
            _camera = getattr(self.orch, "camera", None)
            if _camera is not None:
                _camera.stop()
            with self.lock:
                self.orch.store.end(self.session_id)
                self.session_id = f"{self.session_id}-{self.turn}"
            _chat = getattr(self.orch, "chat", None)
            if _chat is not None:
                _chat.reset()
            return {
                "ok": True,
                "response": {
                    "status": "ok",
                    "recommendations": [],
                    "trace": {"latency_ms": {}},
                    "intent_result": {"intent": "reset"},
                },
            }
        return _err(ErrorCode.INVALID_INPUT, "未知操作")

    def product(self, item_id: str) -> dict:
        """商品详情：本地目录事实 + 相似商品（供详情弹窗使用）。"""
        cat = self.orch.catalog
        product = cat.product(item_id)
        if product is None:
            return _err(ErrorCode.ITEM_NOT_FOUND, "商品不在目录中")
        similar = []
        for sid in cat.similar_of(item_id, k=8):
            sp = cat.product(sid) or {}
            similar.append(
                {
                    "item_id": sid,
                    "title": sp.get("title", ""),
                    "category": sp.get("category", ""),
                    "price": sp.get("price"),
                    "emoji": category_emoji(sp.get("category", "")),
                    "img_url": "/api/img/" + urllib.parse.quote(sid),
                }
            )
        return {
            "ok": True,
            "product": {
                "item_id": item_id,
                "title": product.get("title", ""),
                "english_title": product.get("english_title", ""),
                "category": product.get("category", ""),
                "price": product.get("price"),
                "currency": product.get("currency", ""),
                "price_source": product.get("price_source"),
                "transactions": product.get("transactions"),
                "emoji": category_emoji(product.get("category", "")),
                "img_url": "/api/img/" + urllib.parse.quote(item_id),
            },
            "similar": similar,
        }

    def home(self, limit: int = 8) -> dict:
        """首页商品：用**独立会话**跑一次发现型查询，不污染对话历史。

        这样打开页面就有货可看（电商首页观感），而对话面板保持干净。
        """
        account = self.account()
        home_query = str(account.get("home_query") or "推荐一些热门商品")
        spec = {"source": "text", "text": home_query}
        limit = max(1, min(40, int(limit)))
        # 首页只需要文本货架；合成语音会让 /api/home 从 ~5ms 涨到 ~3.3s，
        # 直接把 LCP 拖到 4s+。首页也不调用 LLM 解释。
        with self.lock, self._runtime_flags(dry_run_voice=True, llm_enabled=False):
            res = self.orch.handle(spec, user_id=self.user_id, session_id=self.session_id + "-home", top_k=limit)
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        items = resp.get("recommendations", [])
        for item in items:
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        categories = sorted(
            {
                str(item.get("category")).strip()
                for item in items
                if item.get("category") and str(item.get("category")).strip()
            }
        )
        category_text = "、".join(categories) if categories else "个性化商品"
        return {
            "ok": True,
            "items": items,
            "title": f"{account.get('persona', '')} · 猜你喜欢",
            "subtitle": f"当前分类：{category_text} · 按{account.get('persona', '你的偏好')}排序",
        }

    def more(self, text: str, top_k: int = 16) -> dict:
        """把当前商品列表扩到 top_k 件；用独立会话，不污染对话历史。"""
        top_k = max(1, min(120, int(top_k)))
        spec = {"source": "text", "text": text or "推荐一些热门商品"}
        sid = f"{self.session_id}-more-{int(time.time() * 1000)}"
        with self.lock, self._runtime_flags(dry_run_voice=True, llm_enabled=False):
            res = self.orch.handle(spec, user_id=self.user_id, session_id=sid, top_k=top_k)
        if not res.ok:
            return _from_app_error(res.error)
        resp = res.data
        items = list(resp.get("recommendations", []))
        # 推荐系统的候选池通常只到 40 件；无限滚动再往后时用目录热门补齐，
        # 明确标注「商品目录补充」，不冒充个性化精排结果。
        if len(items) < top_k:
            catalog = self.orch.catalog
            seen = {str(item.get("item_id")) for item in items}
            slots = (resp.get("effective_request") or {}).get("slots") or {}
            category_slot = slots.get("category") or {}
            type_slot = slots.get("item_type_terms") or {}
            category = str(category_slot.get("value") or "").strip()
            type_terms = type_slot.get("value") if isinstance(type_slot.get("value"), list) else []
            if type_terms:
                candidates = catalog.title_matches(
                    [str(term) for term in type_terms],
                    category=category or None,
                    k=len(catalog.catalog),
                )
            elif category:
                candidates = catalog.category_top(category, len(catalog.catalog))
            else:
                candidates = list(catalog.popular.get("global_top") or [])
            if len(candidates) < top_k:
                candidates.extend(k for k in catalog.catalog if k not in candidates)
            for iid in candidates:
                if len(items) >= top_k:
                    break
                iid = str(iid)
                if iid in seen:
                    continue
                product = catalog.product(iid)
                if not product:
                    continue
                if category and product.get("category") != category:
                    continue
                title = f"{product.get('title', '')} {product.get('english_title', '')}".lower()
                if type_terms and not any(str(term).lower() in title for term in type_terms):
                    continue
                items.append(
                    {
                        "item_id": iid,
                        "title": product.get("title") or product.get("english_title") or iid,
                        "category": product.get("category", "其他"),
                        "price": product.get("price"),
                        "currency": product.get("currency", catalog.currency),
                        "score": max(0.10, 0.30 - 0.001 * len(items)),
                        "rank": len(items) + 1,
                        "sources": ["商品目录补充"],
                        "evidence": {"matched_constraints": []},
                        "explanation": "来自商品目录的更多商品，未参与个性化精排。",
                        "explanation_source": "template",
                        "explanation_grounded": True,
                        "evidence_count": 0,
                    }
                )
                seen.add(iid)
        for item in items:
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        return {"ok": True, "items": items, "top_k": top_k}

    def similar_preview(self, item_id: str, top_k: int = 4) -> dict:
        """点赞后自动生成相似好物，使用独立会话，不改动当前货架与会话约束。"""
        item_id = str(item_id).strip()
        if not self.orch.catalog.exists(item_id):
            return _err(ErrorCode.ITEM_NOT_FOUND, "商品不在目录中", field="item_id")
        top_k = max(1, min(8, int(top_k)))
        sid = f"{self.session_id}-similar-preview-{item_id}-{time.monotonic_ns()}"
        with self.lock:
            session = self.orch.store.get_or_create(sid, self.user_id)
            session.focus_item_id = item_id
            session.last_item_ids = [item_id]
            try:
                with self._runtime_flags(dry_run_voice=True, llm_enabled=False):
                    res = self.orch.handle(
                        {"source": "text", "text": "和这个商品相似的商品"},
                        user_id=self.user_id,
                        session_id=sid,
                        top_k=top_k,
                    )
            finally:
                self.orch.store.end(sid)
        if not res.ok:
            return _from_app_error(res.error)
        items = list((res.data or {}).get("recommendations", []))[:top_k]
        for item in items:
            iid = str(item.get("item_id") or "")
            if iid:
                item["img_url"] = "/api/img/" + urllib.parse.quote(iid)
                item["emoji"] = category_emoji(item.get("category", ""))
        return {"ok": True, "seed_id": item_id, "items": items}

    def view(self, item_id: str) -> dict:
        """「继续浏览」弱反馈：用户打开商品详情即上报一次。

        与点赞共用一条链路，但走 source=browse（弱正反馈，只轻微上浮，
        不改变正负反馈状态，也不屏蔽任何商品）。
        """
        if not self.orch.catalog.exists(str(item_id)):
            return _err(ErrorCode.ITEM_NOT_FOUND, "商品不在目录中")
        return self.handle_spec({"source": "browse", "item_id": str(item_id)})

    def feedback(self, item_id: str, polarity: str, top_k: int | None = None) -> dict:
        """商品卡上的点赞/倒赞：先把会话焦点指到该商品，再走正规反馈链路。

        复用 Orchestrator 的反馈闭环（正负互斥、会话隔离、反馈后重排），
        不另起一套逻辑 —— 这样页面上的点赞与语音/手势点赞行为完全一致。
        """
        raw_polarity = str(polarity).lower()
        if raw_polarity in ("none", "clear", "cancel", "off"):
            with self._runtime_flags(dry_run_voice=True, llm_enabled=False):
                result = self.orch.clear_feedback(
                    str(item_id),
                    user_id=self.user_id,
                    session_id=self.session_id,
                    top_k=top_k,
                )
                return {"ok": True, "response": result.data} if result.ok else _from_app_error(result.error)

        polarity = "positive" if raw_polarity in ("positive", "up", "like", "1") else "negative"
        name = "thumbs_up" if polarity == "positive" else "thumbs_down"
        with self.lock:
            sess = self.orch.store.get_or_create(self.session_id, self.user_id)
            if self.orch.catalog.exists(str(item_id)):
                sess.focus_item_id = str(item_id)
                if str(item_id) not in sess.last_item_ids:
                    sess.last_item_ids = [str(item_id), *sess.last_item_ids]
        with self._runtime_flags(dry_run_voice=True, llm_enabled=False):
            return self.handle_spec({"source": "gesture", "gesture": {"name": name, "confidence": 1.0}}, top_k=top_k)

    def products(self, item_ids: list[str]) -> dict:
        """批量取商品详情 —— 供对比栏做字段对照。

        设计文档 5.4 建议的接口：避免前端为 N 件商品发 N 次请求。
        数据只来自本地目录，不依赖外部服务。
        """
        cat = self.orch.catalog
        out = []
        for item_id in (item_ids or [])[:8]:
            product = cat.product(str(item_id))
            if not product:
                continue
            out.append(
                {
                    "item_id": str(item_id),
                    "title": product.get("title", ""),
                    "english_title": product.get("english_title", ""),
                    "category": product.get("category", ""),
                    "price": product.get("price"),
                    "currency": product.get("currency", ""),
                    "price_source": product.get("price_source"),
                    "transactions": product.get("transactions"),
                    "emoji": category_emoji(product.get("category", "")),
                    "img_url": "/api/img/" + urllib.parse.quote(str(item_id)),
                }
            )
        if not out:
            return _err(ErrorCode.NO_RESULT, "没有可用的商品")
        return {"ok": True, "products": out}

    def state(self) -> dict:
        with self.lock:
            sess = self.orch.store.get(self.session_id)
            return sess.snapshot() if sess else {}

    def info(self) -> dict:
        with self.lock:
            chat_report = (
                self.orch.chat.report() if self.orch.chat is not None and hasattr(self.orch.chat, "report") else {}
            )
            return {
                "user_id": self.user_id,
                "session_id": self.session_id,
                "modes": self.orch.modes(),
                "ok": True,
                "account": self.account(),
                "accounts": settings.DEMO_ACCOUNTS,
                "voice": self.with_voice,
                "llm": bool(self.orch.llm_explainer.enabled),
                "llm_chat": chat_report,
                "vision": self.vision_api.public_info()
                if self.vision_api is not None
                else {"configured": False, "provider": "local", "model": "Qwen2-VL-2B-Instruct"},
            }


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "IntegratedAI"
        sys_version = ""

        def setup(self):
            super().setup()
            self.connection.settimeout(float(settings.WEB_SECURITY_CONFIG["socket_timeout_s"]))

        def log_message(self, fmt, *args):  # 静默访问日志，避免刷屏
            pass

        def _reject(self, status: int, message: str, field: str = "request", **extra) -> None:
            self._json(_err(ErrorCode.INVALID_INPUT, message, field=field, **extra), status)

        def _host_header_ok(self) -> bool:
            raw = str(self.headers.get("Host") or "").strip()
            if not raw:
                return False
            host = urllib.parse.urlsplit("//" + raw).hostname
            allowed = {str(item).lower() for item in settings.WEB_SECURITY_CONFIG["allowed_hosts"]}
            return bool(host and host.lower() in allowed)

        def _origin_ok(self) -> bool:
            origin = str(self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            parsed = urllib.parse.urlsplit(origin)
            allowed = {str(item).lower() for item in settings.WEB_SECURITY_CONFIG["allowed_hosts"]}
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                return False
            if parsed.hostname.lower() not in allowed:
                return False
            bound_port = getattr(self.server, "server_port", None)
            return not (parsed.port is not None and bound_port is not None and int(parsed.port) != int(bound_port))

        def _validate_request(self) -> bool:
            if not self._host_header_ok():
                self._reject(403, "Host 不在本地允许列表", "host")
                return False
            fetch_site = str(self.headers.get("Sec-Fetch-Site") or "").lower()
            if fetch_site and fetch_site not in ("same-origin", "none"):
                self._reject(403, "拒绝跨站请求", "origin")
                return False
            if not self._origin_ok():
                self._reject(403, "Origin 与本地服务不匹配", "origin")
                return False
            return True

        def _validate_json_post(self) -> bool:
            if not self._validate_request():
                return False
            ctype = str(self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._reject(415, "POST 请求必须使用 application/json", "content-type")
                return False
            if self.headers.get("Transfer-Encoding"):
                self._reject(400, "不支持 Transfer-Encoding 请求体", "transfer-encoding")
                return False
            if self.headers.get("Content-Encoding"):
                self._reject(400, "不支持压缩请求体", "content-encoding")
                return False
            header_name, expected = settings.WEB_SECURITY_CONFIG["required_header"]
            if str(self.headers.get(header_name) or "") != str(expected):
                self._reject(403, "缺少同源请求标记", "header")
                return False
            return True

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            # 安全响应头（本地演示也加上，避免把页面当内嵌框架/嗅探类型）
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header("Permissions-Policy", "camera=(self), microphone=(self), geolocation=()")
            image_response = ctype.split(";", 1)[0].strip().lower() in _ALLOWED_IMAGE_TYPES
            self.send_header(
                "Content-Security-Policy",
                (
                    "sandbox; default-src 'none'"
                    if image_response
                    else "default-src 'self'; img-src 'self' data:; media-src 'self'; "
                    "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                    "connect-src 'self'; object-src 'none'; frame-ancestors 'none'; "
                    "base-uri 'none'; form-action 'self'"
                ),
            )
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code: int = 200) -> None:
            self._send(
                code,
                json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _body(self) -> dict | None:
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length or 0)
            except (TypeError, ValueError):
                self._reject(400, "Content-Length 必须是整数", "content-length")
                return None
            if length < 0:
                self._reject(400, "Content-Length 不能为负数", "content-length")
                return None
            if not length:
                return {}
            max_bytes = int(settings.WEB_SECURITY_CONFIG["max_body_bytes"])
            if length > max_bytes:
                self._reject(413, f"请求体过大，最多 {max_bytes} 字节", "content-length")
                return None
            try:
                raw = self.rfile.read(length)
            except Exception:
                self._reject(400, "读取请求体失败", "body")
                return None
            if len(raw) != length:
                self._reject(400, "请求体长度与 Content-Length 不一致", "body")
                return None
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._reject(400, "请求体必须是合法 JSON", "body")
                return None
            if not isinstance(payload, dict):
                self._reject(400, "请求体必须是 JSON 对象", "body")
                return None
            return payload

        def _static(self, name: str, ctype: str) -> None:
            target = (WEB_ROOT / name).resolve()
            if not target.is_relative_to(WEB_ROOT.resolve()) or not target.is_file():
                self._json(_err(ErrorCode.ITEM_NOT_FOUND, "静态文件不存在", field="path"), 404)
                return
            self._send(200, target.read_bytes(), ctype)

        def _stream_camera(self) -> None:
            camera = getattr(app.orch, "camera", None)
            if camera is None or not camera.running:
                self._json(_err(ErrorCode.MODALITY_UNAVAILABLE, "手势直播未开启"), 503)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
            self.end_headers()
            try:
                while True:
                    data = camera.frame()
                    if data:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    time.sleep(0.06)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self._validate_request():
                return
            path_only = self.path.split("?", 1)[0]
            if path_only == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if path_only in ("/", "/index.html"):
                self._static("index.html", "text/html; charset=utf-8")
                return
            if path_only == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
                return
            if path_only == "/app.js":
                self._static("app.js", "application/javascript; charset=utf-8")
                return
            if self.path == "/api/info":
                self._json(app.info())
                return
            if self.path.startswith("/camera/stream"):
                self._stream_camera()
                return
            if self.path == "/api/camera/state":
                self._json(app.camera_state())
                return
            if self.path.startswith("/api/home"):
                parsed = urllib.parse.urlparse(self.path)
                qs = urllib.parse.parse_qs(parsed.query)
                try:
                    limit = int((qs.get("limit") or ["8"])[0])
                except ValueError:
                    limit = 8
                self._json(app.home(limit))
                return
            if self.path.startswith("/capture/"):
                rel = urllib.parse.unquote(self.path[len("/capture/") :].split("?")[0])
                base = (settings.PATHS["artifacts"] / "webui").resolve()
                target = (base / rel).resolve()
                if target.is_relative_to(base) and target.is_file():
                    ctype = "image/png" if target.suffix.lower() == ".png" else "image/jpeg"
                    self._send(200, target.read_bytes(), ctype)
                else:
                    self._json(_err(ErrorCode.ITEM_NOT_FOUND, "抓拍画面不存在", field="path"), 404)
                return
            if self.path.startswith("/api/img/"):
                item_id = urllib.parse.unquote(self.path[len("/api/img/") :].split("?")[0])
                if not _valid_item_id(item_id):
                    self._reject(400, "商品编号格式非法", "item_id")
                    return
                got = fetch_product_image(item_id)
                if got is None:
                    # 缺图时返回透明占位图，让前端类目 emoji 兜底层继续显示，避免 404 噪音。
                    self._send(200, _PLACEHOLDER_IMAGE, "image/png")
                else:
                    data, ctype = got
                    safe_type = ctype if ctype in _ALLOWED_IMAGE_TYPES else "image/jpeg"
                    self._send(200, data, safe_type)
                return
            if self.path.startswith("/api/product/"):
                item_id = urllib.parse.unquote(self.path[len("/api/product/") :].split("?")[0])
                self._json(app.product(item_id))
                return
            if self.path.startswith("/audio/"):
                name = self.path[len("/audio/") :].split("?")[0]
                target = settings.PATHS["artifacts"] / Path(name).name
                if target.exists():
                    ctype = "audio/wav" if target.suffix == ".wav" else "audio/mpeg"
                    self._send(200, target.read_bytes(), ctype)
                else:
                    self._json(_err(ErrorCode.ITEM_NOT_FOUND, "音频不存在", field="name"), 404)
                return
            self._json(_err(ErrorCode.INVALID_INPUT, f"未知路径：{self.path}", field="path"), 404)

        def do_POST(self):
            if not self._validate_json_post():
                return
            path_only = self.path.split("?", 1)[0]
            allowed, retry_after = app.rate_limit(path_only, str(self.client_address[0]))
            if not allowed:
                self._json(
                    _err(
                        ErrorCode.INVALID_INPUT,
                        f"请求过于频繁，请约 {retry_after} 秒后再试",
                        field="rate",
                        retry_after_s=retry_after,
                    ),
                    429,
                )
                return
            body = self._body()
            if body is None:
                return
            try:
                if path_only == "/api/chat":
                    raw_k = body.get("top_k")
                    self._json(
                        app.chat(
                            str(body.get("text", "")),
                            int(raw_k) if raw_k else None,
                            str(body.get("focus_item_id") or ""),
                        )
                    )
                elif path_only == "/api/more":
                    raw_k = body.get("top_k")
                    self._json(app.more(str(body.get("text", "")), int(raw_k) if raw_k else 16))
                elif path_only == "/api/similar-preview":
                    raw_k = body.get("top_k")
                    self._json(
                        app.similar_preview(
                            str(body.get("item_id", "")),
                            int(raw_k) if raw_k else 4,
                        )
                    )
                elif path_only == "/api/tts":
                    self._json(app.tts(str(body.get("text", ""))))
                elif path_only == "/api/camera/start":
                    self._json(app.camera_start())
                elif path_only == "/api/camera/stop":
                    self._json(app.camera_stop())
                elif path_only == "/api/voice":
                    raw_k = body.get("top_k")
                    self._json(app.voice(float(body.get("seconds", 4)), int(raw_k) if raw_k else None))
                elif path_only == "/api/gesture":
                    raw_k = body.get("top_k")
                    self._json(
                        app.gesture(
                            str(body.get("item_id") or "") or None,
                            int(raw_k) if raw_k else None,
                            bool(body.get("apply_feedback", True)),
                        )
                    )
                elif path_only == "/api/vision":
                    raw_k = body.get("top_k")
                    self._json(app.vision(str(body.get("question", "")), int(raw_k) if raw_k else None))
                elif path_only == "/api/undo":
                    self._json(app.control("undo"))
                elif path_only == "/api/reset":
                    self._json(app.control("reset"))
                elif path_only == "/api/products":
                    ids = body.get("item_ids") or []
                    if isinstance(ids, list):
                        self._json(app.products(list(ids)))
                    else:
                        self._json(_err(ErrorCode.INVALID_INPUT, "item_ids 必须是数组", field="item_ids"), 400)
                elif path_only == "/api/view":
                    self._json(app.view(str(body.get("item_id", ""))))
                elif path_only == "/api/feedback":
                    raw_k = body.get("top_k")
                    self._json(
                        app.feedback(
                            str(body.get("item_id", "")),
                            str(body.get("polarity", "positive")),
                            int(raw_k) if raw_k else None,
                        )
                    )
                elif path_only == "/api/user":
                    self._json(app.switch_user(str(body.get("user_id", ""))))
                elif path_only == "/api/state":
                    self._json({"ok": True, "state": app.state()})
                elif path_only == "/api/llm":
                    with app.lock:
                        enabled = not bool(app.orch.llm_explainer.enabled)
                        app.orch.llm_explainer.enabled = enabled
                    self._json({"ok": True, "enabled": enabled})
                else:
                    self._json(_err(ErrorCode.INVALID_INPUT, f"未知接口：{path_only}", field="path"), 404)
            except Exception as exc:  # 任何异常都回结构化错误，不让页面卡死
                msg = f"{type(exc).__name__}: {exc}"
                hint = _dep_hint(msg)
                payload = _err(ErrorCode.INTERNAL_ERROR, msg + (f"（{hint}）" if hint else ""), hint=hint)
                self._json(payload, 500)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="网页对话界面")
    parser.add_argument("--host", default="127.0.0.1", help="仅允许 127.0.0.1 / localhost / ::1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--user", default="15547")
    parser.add_argument("--mode", default=None, choices=["offline", "auto", "online"])
    parser.add_argument("--voice", action="store_true", help="开启语音播报")
    parser.add_argument("--real", action="store_true", help="真实多模态")
    parser.add_argument("--llm", action="store_true", help="真实 LLM 生成解释")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    settings.ensure_dirs()
    mode = args.mode or ("auto" if args.real else None)
    allow_real = True if args.real else None
    if args.real:
        os.environ.setdefault("INTEGRATED_DEMO_USER", "15547")
    allowed_hosts = {str(item).lower() for item in settings.WEB_SECURITY_CONFIG["allowed_hosts"]}
    if str(args.host).lower() not in allowed_hosts:
        print("安全限制：网页服务只允许绑定本地回环地址（127.0.0.1 / localhost / ::1）。")
        return 2

    app = App(args.user, mode, args.voice, args.llm, allow_real)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(app))
    display_host = f"[{args.host}]" if ":" in args.host else args.host
    url = f"http://{display_host}:{args.port}"
    modes = app.orch.modes()
    print("=" * 70)
    print("多模态个性化推荐助手 · 网页对话界面")
    print("=" * 70)
    print(f"地址： {url}")
    print(
        f"用户： {args.user}   多模态：{modes['multimodal']}   推荐：{modes['recommender']}   "
        f"LLM：{modes.get('llm_explain')}   对话后端：{modes.get('llm_provider')}   "
        f"语音播报：{'开' if args.voice else '关'}"
    )
    print("按 Ctrl+C 停止服务\n")
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止")
    finally:
        server.server_close()
        _chat = getattr(app.orch, "chat", None)
        if _chat is not None:
            _chat.stop()
        _asr = getattr(app.orch, "asr", None)
        if _asr is not None:
            _asr.stop()
        _camera = getattr(app.orch, "camera", None)
        if _camera is not None:
            _camera.stop()
    return 0


if __name__ == "__main__":
    ensure_utf8_stdio()
    raise SystemExit(main())
