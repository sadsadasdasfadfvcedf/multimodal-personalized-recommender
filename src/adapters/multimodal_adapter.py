"""
src/adapters/multimodal_adapter.py
==================================

「智能多模态AI助手」适配器。

职责
----
把语音(ASR)、视觉(VL)、手势(gesture)三类原始输入，统一转换为
:class:`contracts.InteractionEvent`，并做置信度治理：

    - 低于 drop 阈值 的视觉标签：丢弃
    - 低于 low  阈值 的视觉标签：降权（confidence × 0.5），并标记 low_confidence=True
    - 手势置信度低于门限：丢弃，不参与反馈映射
    - ASR 文本为空/过短：返回 E_EMPTY_TRANSCRIPT（不臆测）

模式
----
real   : 调用外部 multimodal_ai 的真实模型（需要 torch/mediapipe/麦克风/摄像头）
offline: 使用离线场景桩（场景里的模态输入直接结构化），保证离线可演示

接口约定：本适配器不读取 multimodal_ai 的内部变量；real 模式只 import 其公开模块入口。
"""

from __future__ import annotations

from typing import Any

from config import settings

from ..catalog import Catalog  # noqa: F401  (类型提示用，实际不访问其内部)
from ..contracts import InteractionEvent, VisualTag
from ..errors import ErrorCode, Result, fail, ok
from ..text_norm import normalize_input
from ..utils import is_non_empty_str
from .base import AdapterBase

CFG = settings.MULTIMODAL_ADAPTER


class MultimodalAdapter(AdapterBase):
    name = "multimodal_adapter"

    def __init__(self, mode: str | None = None, *, allow_real: bool | None = None) -> None:
        runtime_mode = (mode or settings.RUNTIME["mode"]).lower()
        super().__init__(requested_mode=runtime_mode)
        self.allow_real = settings.RUNTIME["allow_real_models"] if allow_real is None else allow_real
        self._real: dict[str, Any] = {}
        self.active_mode = "unresolved"
        self._resolve()

    # ---------------- 模式解析 ----------------
    def _resolve(self) -> None:
        if self.requested_mode == "offline" or not self.allow_real:
            self.active_mode = "offline_stub"
            if self.requested_mode != "offline":
                self.record_degradation(
                    level="L1",
                    reason="未启用真实模型（INTEGRATED_ALLOW_REAL_MODELS=false）",
                    from_mode="real",
                    to_mode="offline_stub",
                )
            return
        if settings.MULTIMODAL_ROOT is None:
            self.active_mode = "offline_stub"
            self.record_degradation(
                level="L1",
                reason="未找到 multimodal_ai 根目录",
                from_mode="real",
                to_mode="offline_stub",
            )
            return
        try:
            self._probe_real()
            self.active_mode = "real"
        except Exception as exc:
            self.active_mode = "offline_stub"
            self.record_degradation(
                level="L1",
                reason=f"真实模型不可用: {type(exc).__name__}: {exc}",
                from_mode="real",
                to_mode="offline_stub",
            )

    def _probe_real(self) -> None:
        """探测真实后端是否就绪：依赖 + 外部项目模块 + worker 脚本。"""
        import importlib.util

        root = settings.MULTIMODAL_ROOT
        assert root is not None
        for dep in ("torch", "numpy"):
            if importlib.util.find_spec(dep) is None:
                raise RuntimeError(f"缺少依赖 {dep}")
        for key in ("asr_module", "vl_module", "gesture_module"):
            rel = settings.MULTIMODAL_INTEROP[key]
            if not (root / rel).exists():
                raise FileNotFoundError(f"缺少外部模块 {rel}")
        worker = settings.PROJECT_ROOT / CFG["real"]["worker"]
        if not worker.exists():
            raise FileNotFoundError(f"缺少 worker 脚本: {worker}")
        self._real["root"] = root

    def health(self) -> Result:
        return ok(
            {
                "adapter": self.name,
                "active_mode": self.active_mode,
                "allow_real": self.allow_real,
                "multimodal_root": str(settings.MULTIMODAL_ROOT) if settings.MULTIMODAL_ROOT else None,
                "degradations": list(self.degradations),
            }
        )

    # ---------------- 视觉标签治理 ----------------
    def _govern_tags(self, tags: list[dict]) -> tuple[list[VisualTag], list[dict]]:
        """置信度治理：丢弃 / 降权。返回 (保留标签, 处置明细)。"""
        vision = CFG["vision"]
        kept: list[VisualTag] = []
        actions: list[dict] = []
        for raw in tags[: vision["max_tags"] * 2]:
            tag = VisualTag.from_dict(raw) if isinstance(raw, dict) else VisualTag(tag=str(raw), confidence=1.0)
            if not tag.tag:
                continue
            if tag.confidence < vision["drop_confidence_threshold"]:
                actions.append(
                    {
                        "tag": tag.tag,
                        "confidence": tag.confidence,
                        "action": "dropped",
                        "reason": f"置信度 < drop 阈值 {vision['drop_confidence_threshold']}",
                    }
                )
                continue
            low = tag.confidence < vision["low_confidence_threshold"]
            weight = vision["low_confidence_weight"] if low else 1.0
            tag.raw = dict(tag.raw or {})
            tag.raw.update(
                {"low_confidence": low, "weight": weight, "effective_confidence": round(tag.confidence * weight, 4)}
            )
            kept.append(tag)
            actions.append(
                {
                    "tag": tag.tag,
                    "confidence": tag.confidence,
                    "action": "downweighted" if low else "kept",
                    "weight": weight,
                }
            )
            if len(kept) >= vision["max_tags"]:
                break
        return kept, actions

    # ---------------- 手势治理 ----------------
    def _govern_gesture(self, gesture: dict | None) -> tuple[dict | None, list[dict]]:
        if not gesture:
            return None, []
        name = str(gesture.get("name") or gesture.get("gesture") or "").strip()
        confidence = float(gesture.get("confidence", 0.0) or 0.0)
        min_conf = CFG["gesture"]["min_confidence"]
        if not name:
            return None, [{"gesture": None, "action": "dropped", "reason": "缺少手势名称"}]
        if confidence < min_conf:
            return None, [
                {"gesture": name, "confidence": confidence, "action": "dropped", "reason": f"置信度 < 门限 {min_conf}"}
            ]
        polarity = CFG["gesture"]["feedback_map"].get(name)
        governed = {
            "name": name,
            "confidence": round(confidence, 4),
            "polarity": polarity,
            "is_feedback": polarity is not None,
            "source": gesture.get("source", "gesture"),
        }
        return governed, [{"gesture": name, "confidence": confidence, "action": "kept", "polarity": polarity}]

    # ---------------- 事件解析（核心接口） ----------------
    def parse_event(
        self,
        spec: dict,
        *,
        user_id: str,
        session_id: str | None = None,
        trace_id: str | None = None,
        timestamp: str | None = None,
    ) -> Result:
        """把一次交互的模态输入解析为标准 InteractionEvent。

        spec 形如::

            {"source": "voice", "text": "帮我找个礼物", "visual_tags": [...], "gesture": {...}}

        离线桩与真实模式共用同一入口；真实模式仅替换底层模型调用。
        """
        if not isinstance(spec, dict):
            return fail(ErrorCode.INVALID_INPUT, "事件输入必须是 JSON 对象", field="spec")

        from ..utils import new_trace_id, now_iso

        source = str(spec.get("source", "text"))
        text = spec.get("text")
        visual_tags = list(spec.get("visual_tags") or [])
        gesture_in = spec.get("gesture")

        steps: list[dict] = []
        norm_actions: list[dict] = []

        # 1) ASR 文本治理
        if text is not None:
            asr_cfg = CFG["asr"]
            candidate = str(text).strip()
            too_short = (not is_non_empty_str(candidate)) or len(candidate) < asr_cfg["min_text_len"]
            if too_short:
                # 空文本有两种含义，必须区分：
                #   a) 语音输入识别为空 —— 真错误（FAULT-04 依赖这个行为）；
                #   b) 视觉/手势按钮没带提问 —— 只是「没有文本」，不该挡住视觉标签。
                # 早期版本把 b 也当 a，导致「👁 画面」不提问时必然 E_EMPTY_TRANSCRIPT。
                if visual_tags or gesture_in:
                    text = None
                    steps.append({"stage": "asr", "action": "empty_text_ignored", "reason": "other_modality_present"})
                else:
                    return fail(
                        ErrorCode.EMPTY_TRANSCRIPT,
                        f"语音识别结果为空或过短（< {asr_cfg['min_text_len']} 字符），不做意图推测",
                        field="text",
                    )
            else:
                text = candidate
                steps.append({"stage": "asr", "action": "accepted", "text_len": len(text)})

            # 1.5) 输入侧归一化：繁转简 / 全角转半角 / 定向错别字 / 折叠被空格拆开的数字。
            # 只做等价改写，不删除任何内容；原文仍保留在 event.raw.spec.text 中以便追溯。
            # 注意：上面的分支可能把 text 置为 None（视觉/手势没带提问），此时不能再归一化，
            # 否则 normalize_input(None) 会返回 ""，把「无文本」又变回空串。
            if text is not None:
                text, norm_actions = normalize_input(text)
                if norm_actions:
                    steps.append({"stage": "text_norm", "action": "normalized", "detail": norm_actions})

        # 2) 视觉标签治理
        tags, tag_actions = self._govern_tags(visual_tags)
        if visual_tags:
            steps.append(
                {
                    "stage": "vision",
                    "action": "governed",
                    "input": len(visual_tags),
                    "kept": len(tags),
                    "detail": tag_actions,
                }
            )

        # 3) 手势治理
        gesture, gesture_actions = self._govern_gesture(gesture_in)
        if gesture_in:
            steps.append({"stage": "gesture", "action": "governed", "detail": gesture_actions})

        # 3.5) 「继续浏览」事件：没有文本/视觉/手势，但携带被浏览商品
        if str(source) == "browse" and spec.get("item_id"):
            item_id = str(spec["item_id"])
            steps.append({"stage": "browse", "action": "accepted", "item_id": item_id})
            event = InteractionEvent(
                trace_id=trace_id or new_trace_id(),
                user_id=str(user_id),
                timestamp=timestamp or now_iso(),
                source="browse",
                modality="none",
                text=None,
                visual_tags=[],
                gesture=None,
                session_id=session_id,
                raw={"item_id": item_id, "spec": spec},
            )
            return ok(event, steps=steps, mode=self.active_mode)

        # 4) 模态判定
        if gesture and gesture.get("is_feedback"):
            modality = "feedback"
        elif tags and not text:
            modality = "visual"
        elif gesture and not text:
            modality = "gesture"
        elif text:
            modality = "asr"
        else:
            modality = "none"
            return fail(
                ErrorCode.INVALID_INPUT, "事件不包含任何可用模态输入（text/visual_tags/gesture 至少一项）", field="spec"
            )

        event = InteractionEvent(
            trace_id=trace_id or new_trace_id(),
            user_id=str(user_id),
            timestamp=timestamp or now_iso(),
            source=source,
            modality=modality,
            text=text,
            visual_tags=tags,
            gesture=gesture,
            session_id=session_id,
            raw={"spec": spec, "text_norm": norm_actions} if norm_actions else {"spec": spec},
        )
        return ok(event, steps=steps, mode=self.active_mode)

    # ---------------- 真实模型接口（子进程调用外部项目） ----------------
    @staticmethod
    def _abs(path) -> str:
        """把路径解析为绝对路径。

        worker 子进程的 cwd 是外部项目根目录，因此**相对路径会解析错**。
        适配器作为进程边界，负责在传参前统一转成绝对路径。
        """
        from pathlib import Path as _Path

        return str(_Path(str(path)).resolve())

    def _worker_command(self, action: str, params: dict) -> list[str]:
        import json
        import sys

        worker = settings.PROJECT_ROOT / CFG["real"]["worker"]
        return [sys.executable, "-B", str(worker), action, json.dumps(params, ensure_ascii=False)]

    def _run_worker(self, action: str, params: dict, *, timeout_s: float | None = None) -> Result:
        """在外部项目的命名空间里执行一次模态推理（子进程隔离）。

        为什么用子进程：外部项目与本项目都有 `src` 包，同进程 import 会冲突；
        子进程同时形成清晰的进程边界，等价于一次「远程调用」。
        """
        import json as _json
        import os
        import subprocess

        root = settings.MULTIMODAL_ROOT
        if root is None:
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "未找到 multimodal_ai 根目录", field="MULTIMODAL_ROOT")
        worker = settings.PROJECT_ROOT / CFG["real"]["worker"]
        if not worker.exists():
            return fail(ErrorCode.MODALITY_UNAVAILABLE, f"缺少 worker 脚本: {worker}", field="worker")

        env = dict(os.environ)
        env["MM_ROOT"] = str(root)
        env["PYTHONPATH"] = str(root)
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
        # 强制离线加载：模型权重都在本地，若不禁用联网，transformers 会去访问
        # 不可达的 huggingface.co 并长时间挂起（实测卡住 30 分钟无进展）。
        # 需要联网时把 INTEGRATED_MM_ONLINE=1 即可放行。
        if not os.getenv("INTEGRATED_MM_ONLINE"):
            env.setdefault("HF_HUB_OFFLINE", "1")
            env.setdefault("TRANSFORMERS_OFFLINE", "1")
        timeout = timeout_s or CFG["real"]["timeout_s"]
        try:
            proc = subprocess.run(
                self._worker_command(action, params),
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except Exception as exc:
            return fail(
                ErrorCode.TIMEOUT if "Timeout" in type(exc).__name__ else ErrorCode.MODALITY_UNAVAILABLE,
                f"{action} 子进程失败: {type(exc).__name__}: {exc}",
                field="action",
            )

        payload = None
        for line in reversed((proc.stdout or "").strip().splitlines()):
            line = line.strip()
            if line.startswith("{") and line.endswith("}"):
                try:
                    payload = _json.loads(line)
                    break
                except _json.JSONDecodeError:
                    continue
        if payload is None:
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                f"{action} 未返回可解析结果（rc={proc.returncode}）",
                field="stdout",
                stderr=(proc.stderr or "")[-300:],
            )
        if not payload.get("ok"):
            err = payload.get("error") or {}
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                f"{action} 执行失败: {err.get('type')}: {err.get('message')}",
                field="action",
            )
        return ok(payload.get("data"), latency_ms=payload.get("latency_ms"), action=action)

    def worker_health(self) -> Result:
        """真实后端健康检查：worker 可执行 + 设备可见。"""
        return self.guard(
            "worker_health", lambda: self._run_worker("devices", {}, timeout_s=CFG["real"]["probe_timeout_s"])
        )

    def transcribe(self, audio_path: str, sample_rate: int | None = None) -> Result:
        """真实语音识别（SenseVoiceSmall）。offline 模式明确返回不可用。"""
        if self.active_mode != "real":
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                "offline 模式不执行真实 ASR；请在场景中提供 text 字段",
                field="audio_path",
                active_mode=self.active_mode,
            )
        params = {"audio": self._abs(audio_path), "sample_rate": int(sample_rate or CFG["asr"]["sample_rate"])}
        return self.guard("transcribe", lambda: self._run_worker("asr", params))

    def describe_image(
        self, image_path: str, question: str = "", max_new_tokens: int = 96, max_pixels: int | None = None
    ) -> Result:
        """真实视觉语言理解（Qwen-VL）。"""
        if self.active_mode != "real":
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                "offline 模式不执行真实 VL；请在场景中提供 visual_tags",
                field="image_path",
                active_mode=self.active_mode,
            )
        params = {
            "image": self._abs(image_path),
            "question": question or "用一句话说明画面中最主要的人或物品，不要逐处列举。",
            "max_new_tokens": int(max_new_tokens),
        }
        if max_pixels:
            params["max_pixels"] = int(max_pixels)
        return self.guard("describe_image", lambda: self._run_worker("vl", params))

    def recognize_gesture(self, image_path: str, stable_frames: int = 1, *, lenient: bool = False) -> Result:
        """真实手势识别（MediaPipe + 几何判定）。"""
        if self.active_mode != "real":
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                "offline 模式不执行真实手势识别；请在场景中提供 gesture",
                field="image_path",
                active_mode=self.active_mode,
            )
        params = {"image": self._abs(image_path), "stable_frames": int(stable_frames), "lenient": bool(lenient)}
        return self.guard("recognize_gesture", lambda: self._run_worker("gesture", params))

    def recognize_gesture_batch(self, pattern: str, *, limit: int = 8, lenient: bool = False) -> Result:
        """对一组连拍帧做手势识别（MediaPipe 只初始化一次）。"""
        if self.active_mode != "real":
            return fail(
                ErrorCode.MODALITY_UNAVAILABLE,
                "offline 模式不执行真实手势识别",
                field="pattern",
                active_mode=self.active_mode,
            )
        from pathlib import Path as _Path

        p = _Path(str(pattern))
        resolved = str(p.parent.resolve() / p.name)
        return self.guard(
            "recognize_gesture_batch",
            lambda: self._run_worker(
                "gesture_batch", {"pattern": resolved, "limit": int(limit), "lenient": bool(lenient)}
            ),
        )

    def generate_batch(self, prompts: list[str], *, max_new_tokens: int = 120, temperature: float = 0.2) -> Result:
        """批量真实文本生成（模型只加载一次，显著降低平均耗时）。"""
        if self.active_mode != "real":
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "offline 模式不执行真实 LLM", field="prompts")
        return self.guard(
            "generate_batch",
            lambda: self._run_worker(
                "llm_batch",
                {"prompts": list(prompts), "max_new_tokens": int(max_new_tokens), "temperature": float(temperature)},
            ),
        )

    def capture_frame(self, out_path: str, camera_index: int = 0) -> Result:
        """抓取一帧真实摄像头画面。"""
        if self.active_mode != "real":
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "offline 模式不访问摄像头", field="out_path")
        return self.guard(
            "capture_frame",
            lambda: self._run_worker("capture", {"out": self._abs(out_path), "camera_index": int(camera_index)}),
        )

    def capture_burst(
        self,
        out_dir: str,
        *,
        count: int = 5,
        interval_ms: int = 250,
        prefix: str = "frame",
        camera_index: int = 0,
    ) -> Result:
        """连续抓取多帧（单进程内完成），供手势识别挑选最佳结果。"""
        if self.active_mode != "real":
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "offline 模式不访问摄像头", field="out_dir")
        params = {
            "out_dir": self._abs(out_dir),
            "count": int(count),
            "interval_ms": int(interval_ms),
            "prefix": prefix,
            "camera_index": int(camera_index),
        }
        return self.guard("capture_burst", lambda: self._run_worker("capture_burst", params))

    def record_audio(self, out_path: str, seconds: float = 3.0, device_index: int | None = None) -> Result:
        """从真实麦克风录制一段音频。"""
        if self.active_mode != "real":
            return fail(ErrorCode.MODALITY_UNAVAILABLE, "offline 模式不访问麦克风", field="out_path")
        params = {"out": self._abs(out_path), "seconds": float(seconds), "sample_rate": int(CFG["asr"]["sample_rate"])}
        if device_index is not None:
            params["device_index"] = int(device_index)
        return self.guard("record_audio", lambda: self._run_worker("record", params))
