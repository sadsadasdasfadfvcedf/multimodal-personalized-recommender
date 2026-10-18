"""
scripts/mm_worker.py
====================

在外部项目 multimodal_ai 的模块命名空间内执行 **一次** 模态推理。

为什么用子进程而不是直接 import
--------------------------------
外部项目与本项目都有名为 `src` 的包，同进程内 import 会命名空间冲突。
本脚本以 `cwd = MULTIMODAL_ROOT`、`PYTHONPATH = MULTIMODAL_ROOT` 启动，
自身位于 `integrated_ai_project/scripts/`（该目录下没有 src/config 包），
因此 `from src.asr import ASREngine` 一定解析到外部项目的实现 ——
同时形成清晰的进程边界，等价于一次「远程调用」。

输出协议：stdout 最后一行是 JSON，形如
    {"ok": true, "action": "asr", "data": {...}, "latency_ms": 123.4}
失败：{"ok": false, "action": ..., "error": {"type": ..., "message": ...}}

用法（一般由 src/adapters/multimodal_adapter.py 调用）：
    python mm_worker.py asr '{"audio": "...", "sample_rate": 16000}'
    python mm_worker.py vl '{"image": "...", "question": "画面里有什么"}'
    python mm_worker.py gesture '{"image": "..."}'
    python mm_worker.py devices '{}'
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

NL = chr(10)


def _fail(action: str, exc: BaseException) -> int:
    payload = {"ok": False, "action": action, "error": {"type": type(exc).__name__, "message": str(exc)[:500]}}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + NL)
    return 0


def _ok(action: str, data, latency_ms: float) -> int:
    payload = {"ok": True, "action": action, "data": data, "latency_ms": round(latency_ms, 3)}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str) + NL)
    return 0


# ------------------------------------------------------------
# 模型可用性判定：目录里必须有 **完整** 权重，否则跳过
# ------------------------------------------------------------
# ------------------------------------------------------------
# 非 ASCII 路径安全的图像读写
# cv2.imread / cv2.imwrite 在 Windows 上无法处理含中文的路径，
# 且 imwrite 会**静默返回 False**（不抛异常），必须显式检查。
# ------------------------------------------------------------
def _truncate_repetition(text: str, window: int = 8, max_repeat: int = 2) -> tuple:
    """截断生成式模型常见的「循环复读」。

    实测 Qwen2-VL-2B 在开放式提问下会陷入死循环：
    「…桌子上放着一些物品，桌子后面有一面墙，墙上挂着一些物品，桌子后面有一面墙…」
    这里用滑窗统计：某个 window 长度的片段第 (max_repeat+1) 次出现时，
    从该位置截断，并在句末补上句号。
    """
    if not text:
        return text, False
    seen: dict = {}
    index = 0
    while index + window <= len(text):
        gram = text[index : index + window]
        count = seen.get(gram, 0) + 1
        seen[gram] = count
        if count > max_repeat:
            head = text[:index].rstrip("，。、；：,. ")
            return (head + "。") if head else (text[: max(window, 12)] + "。"), True
        index += 1
    return text, False


def _imread(path: str):
    import cv2
    import numpy as np

    buf = np.fromfile(path, dtype=np.uint8)
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _imwrite(path: str | Path, image) -> None:
    import cv2
    import numpy as np  # noqa: F401

    target = Path(path)
    ext = target.suffix or ".jpg"
    ok_flag, buf = cv2.imencode(ext, image)
    if not ok_flag:
        raise RuntimeError("图像编码失败: " + str(target))
    target.parent.mkdir(parents=True, exist_ok=True)
    buf.tofile(str(target))
    if not target.exists() or target.stat().st_size == 0:
        raise RuntimeError("图像写入失败: " + str(target))


def _usable(model_dir: str | Path) -> bool:
    base = Path(model_dir)
    if not base.is_dir():
        return False
    # Transformers 用 config.json；FunASR 用 configuration.json / config.yaml
    has_config = any((base / n).exists() for n in ("config.json", "configuration.json", "config.yaml"))
    if not has_config:
        return False
    index = base / "model.safetensors.index.json"
    if index.exists():
        try:
            meta = json.loads(index.read_text(encoding="utf-8"))
            shards = sorted(set(meta.get("weight_map", {}).values()))
            if not shards:
                return False
            return all((base / s).exists() for s in shards)
        except Exception:
            return False
    return any(
        entry.name.endswith((".safetensors", ".bin", ".pt")) and not entry.name.endswith(".incomplete")
        for entry in base.iterdir()
    )


def resolve_model(kind: str) -> str:
    """按候选顺序挑第一个权重完整的模型目录（外部盘 → 项目内）。"""
    models = Path(_root()) / "models"
    env_map = {"asr": "MULTIMODAL_ASR_PATH", "llm": "MULTIMODAL_LLM_PATH", "vl": "MULTIMODAL_VL_PATH"}
    explicit = os.environ.get(env_map.get(kind, ""))
    candidates = {
        "asr": [models / "iic" / "SpeechRecognition" / "SenseVoiceSmall"],
        "llm": [models / "Qwen" / "Qwen3-4B-Instruct-2507", models / "Qwen" / "Qwen2-1.5B-Instruct"],
        "vl": [
            models / "Qwen" / "Qwen2.5-VL-3B-Instruct",
            models / "Qwen" / "Qwen2-VL-2B-Instruct",
            models / "Qwen" / "Qwen2-VL-2B-Instruct".replace("2B", "2B-Instruct"),
        ],
    }.get(kind, [])
    if explicit and _usable(explicit):
        return explicit
    for path in candidates:
        if _usable(path):
            return str(path)
    raise FileNotFoundError("找不到可用（权重完整）的 " + kind + " 模型；候选：" + str(candidates))


def _root() -> str:
    root = os.environ.get("MM_ROOT")
    if not root or not Path(root).is_dir():
        raise OSError("环境变量 MM_ROOT 未设置或目录不存在")
    if root not in sys.path:
        sys.path.insert(0, root)
    return root


# ------------------------------------------------------------
# actions
# ------------------------------------------------------------
def action_devices(params: dict):
    out: dict[str, Any] = {"microphones": [], "cameras": [], "default_input_index": None}
    try:
        import pyaudio

        pa = pyaudio.PyAudio()
        with contextlib.suppress(Exception):
            out["default_input_index"] = pa.get_default_input_device_info()["index"]
        for i in range(pa.get_device_count()):
            try:
                d = pa.get_device_info_by_index(i)
                if d.get("maxInputChannels", 0) > 0:
                    out["microphones"].append(
                        {"index": i, "name": d["name"], "sample_rate": int(d["defaultSampleRate"])}
                    )
            except Exception:
                continue
        pa.terminate()
    except Exception as exc:
        out["microphone_error"] = type(exc).__name__ + ": " + str(exc)

    try:
        import cv2

        for i in range(4):
            cap = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if cap.isOpened():
                ok_flag, frame = cap.read()
                if ok_flag and frame is not None:
                    out["cameras"].append({"index": i, "width": int(frame.shape[1]), "height": int(frame.shape[0])})
            cap.release()
    except Exception as exc:
        out["camera_error"] = type(exc).__name__ + ": " + str(exc)
    return out


def action_asr(params: dict):
    from src.asr import ASREngine

    audio = params["audio"]
    sample_rate = int(params.get("sample_rate", 16000))
    engine = ASREngine(model_path=resolve_model("asr"))
    text = engine.transcribe(audio, sample_rate)
    with contextlib.suppress(Exception):
        engine.release()
    return {"text": text or "", "audio": audio, "sample_rate": sample_rate, "text_length": len(text or "")}


def action_vl(params: dict):
    from src.vl_model import VLModel

    image_path = params["image"]
    question = params.get("question") or "用一句话说明画面中最主要的人或物品，不要逐处列举。"
    frame = _imread(image_path)
    if frame is None:
        raise FileNotFoundError("无法读取图片: " + image_path)
    model = VLModel(model_path=resolve_model("vl"))
    response = model.generate_response(
        [frame],
        question,
        max_new_tokens=int(params.get("max_new_tokens", 96)),
        max_pixels=int(params.get("max_pixels", 320 * 240)),
    )
    with contextlib.suppress(Exception):
        model.release()
    cleaned, truncated = _truncate_repetition(response or "")
    return {
        "response": cleaned,
        "raw_response": response,
        "repetition_truncated": truncated,
        "image": image_path,
        "question": question,
        "frame_shape": list(frame.shape),
        "model": Path(resolve_model("vl")).name,
    }


def _configure_lenient_gesture() -> None:
    """网页端显式手势：放宽阈值，容忍「手挡住脸」和「手掌贴画面下沿」。

    严格模式（外部 GUI / 脚本）仍用 0.5 阈值 + 人脸误检过滤；
    只有用户主动点「手势」并保持姿势时才走宽松模式。
    """
    # 运行时 cwd/PYTHONPATH 指向 multimodal_ai；mypy 会把 `config` 解析成本项目的
    # config 包（没有 DETECTION_CONFIG），因此这里用动态导入避免静态误报。
    import importlib

    detection = importlib.import_module("config").DETECTION_CONFIG
    detection["min_detection_confidence"] = 0.05
    detection["min_tracking_confidence"] = 0.05
    detection["gesture_min_confidence"] = 0.50
    detection["gesture_thumb_min_confidence"] = 0.45
    detection["gesture_margin"] = 0.03


def _lenient_gesture_name(recognizer, hand_landmarks) -> str | None:
    """绕过 face-overlap / 掌心尺寸门控，直接做几何分类。

    实测用户把手举到脸前时，MediaPipe 会检测到手，但严格门控把它判成
    「人脸误检」，且掌心贴画面下沿会让 is_plausible_hand 返回 False。
    """
    lm = hand_landmarks.landmark
    xs = [p.x for p in lm]
    ys = [p.y for p in lm]
    width = max(xs) - min(xs)
    height = max(ys) - min(ys)
    if width < 0.02 or height < 0.04 or width > 0.9 or height > 0.98:
        return None
    name, confidence = recognizer._classify(lm)
    recognizer.last_confidence = confidence
    return name if name else None


def _pick_best_gesture(recognizer, hands, face, lenient: bool):
    """返回 (name, confidence)；宽松模式对每帧只取置信度最高的一个。"""
    from src.gesture import GestureRecognizer

    best: tuple[str, float] | None = None
    for lm in hands.multi_hand_landmarks:
        picked = GestureRecognizer.pick_hand([lm], getattr(hands, "multi_handedness", None))
        if picked is None:
            continue
        name = _lenient_gesture_name(recognizer, picked) if lenient else recognizer.recognize(picked, face)
        if name:
            confidence = float(recognizer.last_confidence or 0.0)
            if best is None or confidence > best[1]:
                best = (name, confidence)
    return best


def action_gesture(params: dict):
    from src.gesture import GestureRecognizer
    from src.human_detector import HumanDetector

    image_path = params["image"]
    frame = _imread(image_path)
    if frame is None:
        raise FileNotFoundError("无法读取图片: " + image_path)

    lenient = bool(params.get("lenient"))
    if lenient:
        _configure_lenient_gesture()
    detector = HumanDetector()
    try:
        res = detector.process_frame(frame)
    finally:
        with contextlib.suppress(Exception):
            detector.close()

    hands = res.get("hands")
    face = res.get("face")
    out = {
        "image": image_path,
        "hands_detected": 0,
        "gestures": [],
        "pose_detected": res.get("pose") is not None,
        "note": "",
    }
    if not hands or not getattr(hands, "multi_hand_landmarks", None):
        out["note"] = "画面中未检测到手"
        return out

    out["hands_detected"] = len(hands.multi_hand_landmarks)
    # stable_frames=1：单张图片也要能出结果（正常链路是连续帧确认）
    recognizer = GestureRecognizer(stable_frames=int(params.get("stable_frames", 1)))
    best = _pick_best_gesture(recognizer, hands, face, lenient)
    if best:
        out["gestures"].append({"name": best[0], "confidence": round(best[1], 4)})
    if not out["gestures"]:
        out["note"] = "检测到手，但未确认为已定义手势"
    return out


def action_capture(params: dict):
    """抓取一帧真实摄像头画面并落盘。"""
    import cv2
    import numpy as np

    out = params["out"]
    index = int(params.get("camera_index", 0))
    # 摄像头首帧常为全黑（预热不足），因此多读几帧并挑**最亮**的一帧，
    # 避免把黑帧当成有效画面送给 VL（实测会导致 VL 只能回答「不确定」）。
    warmup = int(params.get("warmup_frames", 20))
    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError("摄像头打开失败: index=" + str(index))
    best = None
    best_mean = -1.0
    try:
        for _ in range(max(1, warmup)):
            ok_flag, fr = cap.read()
            if not ok_flag or fr is None:
                continue
            mean = float(np.mean(fr))
            if mean > best_mean:
                best, best_mean = fr, mean
    finally:
        cap.release()
    if best is None:
        raise RuntimeError("摄像头抓帧失败")
    frame = best
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    _imwrite(out, frame)
    return {"path": out, "shape": list(frame.shape), "bytes": Path(out).stat().st_size}


def action_capture_burst(params: dict):
    """连续抓取多帧（同一进程内完成），供手势识别挑选最佳帧。

    为什么需要：单帧抓拍时用户往往还没把手举到镜头前，实测抓到的是
    「只有人脸、没有手」的画面。连拍 + 前端倒计时可以显著降低这种误判。
    """
    import time as _time

    import cv2

    out_dir = Path(params["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(params.get("prefix") or "frame")
    count = max(1, min(10, int(params.get("count", 5))))
    interval_ms = max(0, min(1000, int(params.get("interval_ms", 250))))
    warmup = max(1, min(30, int(params.get("warmup_frames", 8))))
    index = int(params.get("camera_index", 0))

    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        raise RuntimeError("摄像头打开失败: index=" + str(index))
    paths: list[str] = []
    try:
        for _ in range(warmup):
            cap.read()
        for i in range(count):
            ok_flag, frame = cap.read()
            if not ok_flag or frame is None:
                continue
            path = out_dir / f"{prefix}_{i:02d}.jpg"
            _imwrite(path, frame)
            paths.append(str(path))
            if interval_ms and i < count - 1:
                _time.sleep(interval_ms / 1000.0)
    finally:
        cap.release()
    if not paths:
        raise RuntimeError("摄像头连拍失败")
    return {"paths": paths, "count": len(paths), "interval_ms": interval_ms}


def action_record(params: dict):
    """从真实麦克风录制一段音频并落盘为 WAV（16bit PCM）。"""
    import wave

    import pyaudio

    out = params["out"]
    seconds = float(params.get("seconds", 3.0))
    index = params.get("device_index")
    rate = int(params.get("sample_rate", 16000))
    frames_per_chunk = 1024
    pa = pyaudio.PyAudio()
    try:
        if index is None:
            index = pa.get_default_input_device_info()["index"]
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=rate,
            input=True,
            input_device_index=int(index),
            frames_per_buffer=frames_per_chunk,
        )
    except Exception:
        pa.terminate()
        raise
    chunks = []
    try:
        total = int(rate / frames_per_chunk * seconds)
        for _ in range(total):
            chunks.append(stream.read(frames_per_chunk, exception_on_overflow=False))
    finally:
        try:
            stream.stop_stream()
            stream.close()
        finally:
            pa.terminate()
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"".join(chunks))
    peak = 0
    for chunk in chunks[:50]:
        for i in range(0, len(chunk) - 1, 2):
            v = abs(int.from_bytes(chunk[i : i + 2], "little", signed=True))
            if v > peak:
                peak = v
    return {"path": out, "seconds": seconds, "sample_rate": rate, "device_index": int(index), "peak_amplitude": peak}


def action_gesture_batch(params: dict):
    """批量识别一组图片中的手势（MediaPipe 只加载一次）。

    用于在没有真人配合做手势时，用历史手势快照回放验证识别链路。
    """
    import glob

    from src.gesture import GestureRecognizer
    from src.human_detector import HumanDetector

    pattern = params.get("pattern") or str(Path("outputs") / "gestures" / "*.jpg")
    limit = int(params.get("limit", 20))
    lenient = bool(params.get("lenient"))
    if lenient:
        _configure_lenient_gesture()
    # 保留 glob.glob：Path.glob 不接受绝对路径模式，且对本处的 "**" 语义不同，
    # 直接替换会改变行为，故此处不采纳 PTH207。
    files = sorted(glob.glob(pattern))[:limit]  # noqa: PTH207
    if not files:
        raise FileNotFoundError("没有匹配的图片: " + pattern)

    detector = HumanDetector()
    results: list[dict] = []
    try:
        for path in files:
            frame = _imread(path)
            if frame is None:
                results.append({"image": Path(path).name, "hands_detected": 0, "gestures": [], "note": "图片不可读"})
                continue
            res = detector.process_frame(frame)
            hands = res.get("hands")
            face = res.get("face")
            entry: dict[str, Any] = {
                "image": Path(path).name,
                "hands_detected": 0,
                "gestures": [],
                "pose_detected": res.get("pose") is not None,
                "note": "",
            }
            if hands and getattr(hands, "multi_hand_landmarks", None):
                entry["hands_detected"] = len(hands.multi_hand_landmarks)
                recognizer = GestureRecognizer(stable_frames=1)
                best = _pick_best_gesture(recognizer, hands, face, lenient)
                if best:
                    entry["gestures"].append({"name": best[0], "confidence": round(best[1], 4)})
            if not entry["gestures"]:
                entry["note"] = "未确认出手势" if entry["hands_detected"] else "未检测到手"
            results.append(entry)
    finally:
        with contextlib.suppress(Exception):
            detector.close()

    hit = [r for r in results if r["gestures"]]
    summary: dict[str, Any] = {}
    for r in hit:
        for g in r["gestures"]:
            summary[g["name"]] = summary.get(g["name"], 0) + 1
    return {
        "scanned": len(results),
        "with_hand": sum(1 for r in results if r["hands_detected"]),
        "with_gesture": len(hit),
        "gesture_counts": summary,
        "results": results,
    }


def _chat_wrap(llm, prompt: str) -> str:
    """把提示词套上模型自带的 chat template。

    这一步是必须的：TextLLM.generate() 只做 tokenizer(prompt)，
    不会自动套 chat template。若直接传裸文本，模型会当成**文本续写**任务，
    表现为把提示词当上下文继续编造（实测会生成「事实：商品=小熊玩偶」这类假内容），
    指令遵循度急剧下降。套上 chat template 后才是真正的指令跟随。
    """
    tokenizer = getattr(llm, "tokenizer", None)
    if tokenizer is None:
        try:
            llm._ensure_model()
            tokenizer = getattr(llm, "tokenizer", None)
        except Exception:
            tokenizer = None
    if tokenizer is None or not getattr(tokenizer, "chat_template", None):
        return prompt
    messages = [{"role": "user", "content": prompt}]
    try:
        return tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    except Exception:
        return prompt


def action_llm(params: dict):
    """真实文本 LLM 生成（Qwen）。

    直接调用 generate(prompt, ...) 而不是 chat()，以便完全控制提示词 ——
    解释生成必须严格限定在证据范围内，不能带上上游的通用系统提示与历史对话。
    """
    from src.text_llm import TextLLM

    prompt = params["prompt"]
    model_path = resolve_model("llm")
    llm = TextLLM(model_path=model_path)
    try:
        llm._ensure_model()  # 先加载，拿到 tokenizer
        wrapped = _chat_wrap(llm, prompt)  # 套 chat template（关键）
        text = llm.generate(
            wrapped,
            max_new_tokens=int(params.get("max_new_tokens", 220)),
            temperature=float(params.get("temperature", 0.2)),
        )
    finally:
        with contextlib.suppress(Exception):
            llm.release()
    return {
        "text": (text or "").strip(),
        "model": Path(model_path).name,
        "prompt_chars": len(prompt),
        "output_chars": len((text or "").strip()),
    }


def action_llm_batch(params: dict):
    """批量文本生成：模型只加载一次。

    子进程每次调用都要重新加载权重（4B 级模型约 15-20s），
    批量模式把 N 条解释合并到一次调用里，显著降低平均耗时。
    """
    from src.text_llm import TextLLM

    prompts = params["prompts"]
    if not isinstance(prompts, list) or not prompts:
        raise ValueError("prompts 必须是非空数组")
    model_path = resolve_model("llm")
    llm = TextLLM(model_path=model_path)
    outputs = []
    try:
        llm._ensure_model()
        for prompt in prompts:
            wrapped = _chat_wrap(llm, str(prompt))
            text = llm.generate(
                wrapped,
                max_new_tokens=int(params.get("max_new_tokens", 120)),
                temperature=float(params.get("temperature", 0.2)),
            )
            outputs.append({"text": (text or "").strip(), "prompt_chars": len(str(prompt))})
    finally:
        with contextlib.suppress(Exception):
            llm.release()
    return {"outputs": outputs, "model": Path(model_path).name, "count": len(outputs)}


def action_list_llm_models(params: dict):
    """列出外部项目中「权重完整可用」的 LLM（用于模型选型对比）。"""
    base = Path(_root()) / "models" / "Qwen"
    found = []
    if base.is_dir():
        for entry in sorted(base.iterdir()):
            if not _usable(entry):
                continue
            size = 0
            for dirpath, _dirnames, filenames in os.walk(entry):
                for filename in filenames:
                    size += (Path(dirpath) / filename).stat().st_size
            found.append({"name": entry.name, "path": str(entry), "size_mb": round(size / 1024 / 1024, 1)})
    return {"models": found, "default": Path(resolve_model("llm")).name}


ACTIONS = {
    "devices": action_devices,
    "asr": action_asr,
    "vl": action_vl,
    "gesture": action_gesture,
    "gesture_batch": action_gesture_batch,
    "capture": action_capture,
    "capture_burst": action_capture_burst,
    "record": action_record,
    "llm": action_llm,
    "llm_batch": action_llm_batch,
    "list_llm_models": action_list_llm_models,
}


def main() -> int:
    parser = argparse.ArgumentParser(description="multimodal_ai 子进程 worker")
    parser.add_argument("action", choices=sorted(ACTIONS))
    parser.add_argument("params", nargs="?", default="{}")
    args = parser.parse_args()
    try:
        params = json.loads(args.params or "{}")
    except json.JSONDecodeError as exc:
        return _fail(args.action, ValueError("params 不是合法 JSON: " + str(exc)))
    started = time.perf_counter()
    try:
        _root()
        data = ACTIONS[args.action](params)
    except BaseException as exc:
        return _fail(args.action, exc)
    return _ok(args.action, data, (time.perf_counter() - started) * 1000)


if __name__ == "__main__":
    raise SystemExit(main())
