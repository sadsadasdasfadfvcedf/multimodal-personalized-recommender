"""
scripts/09_real_multimodal_check.py
===================================

真机多模态链路验证（阶段6 的可选加强项）。

验证目标：**真实模型输出与离线契约完全一致** ——
外部 multimodal_ai 的真实 ASR / VL / 手势结果，喂进本项目适配器后，
产出与离线场景桩同样结构的 InteractionEvent，并能驱动完整反馈闭环。

链路：
    真实麦克风 → WAV → SenseVoiceSmall ASR ─┐
    真实摄像头 → 帧 → Qwen2-VL 视觉理解      ├→ MultimodalAdapter → InteractionEvent → Orchestrator
    真实手势快照 → MediaPipe 手势识别        ┘

前置条件（缺一即跳过并记录，不影响离线可复现性）：
    - 环境变量 INTEGRATED_ALLOW_REAL_MODELS=1
    - 外部 multimodal_ai 根目录可探测、权重完整、设备可用

输出：outputs/real_multimodal_check.json
产物：artifacts/real/（抓帧、录音，可再生）
"""
from __future__ import annotations

import os

from _bootstrap import banner

from config import settings
from src.adapters import MultimodalAdapter
from src.contracts import InteractionEvent
from src.orchestrator import Orchestrator
from src.utils import now_iso, write_json

MEDIA_DIR = settings.PATHS["artifacts"] / "real"


def _skip(reason: str, report: dict) -> int:
    report["mode"] = "skipped"
    report["reason"] = reason
    path = write_json(settings.PATHS["outputs"] / "real_multimodal_check.json", report)
    print("跳过：" + reason)
    print("[09_real_multimodal_check] 证据已写入 " + path.name)
    return 0


def _contract_check(event, label: str) -> dict:
    """把真实模型产出的事件往返序列化，确认与契约一致。"""
    payload = event.to_dict()
    back = InteractionEvent.from_dict(payload)
    required = ("trace_id", "user_id", "timestamp", "source")
    return {
        "label": label,
        "ok": bool(back.ok),
        "envelope_complete": all(k in payload and payload[k] for k in required),
        "modality": payload.get("modality"),
        "trace_id": payload.get("trace_id"),
        "roundtrip_error": (back.error.to_dict() if back.error else None),
    }


def main() -> int:
    banner("真机多模态链路验证 · 真实模型输出 vs 离线契约")
    settings.ensure_dirs()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    report: dict = {"checked_at": now_iso(), "mode": "real", "steps": [], "contracts": []}

    if not settings.RUNTIME["allow_real_models"]:
        return _skip("未启用真实模型（设置 INTEGRATED_ALLOW_REAL_MODELS=1 后重试）", report)
    if settings.MULTIMODAL_ROOT is None:
        return _skip("未找到 multimodal_ai 根目录", report)

    adapter = MultimodalAdapter(mode="auto", allow_real=True)
    report["adapter"] = {"active_mode": adapter.active_mode,
                         "degradations": adapter.degradation_report()["events"]}
    if adapter.active_mode != "real":
        return _skip("适配器未能进入 real 模式：" + str(adapter.degradation_report()["events"]), report)

    # ---------- 1) 设备 ----------
    health = adapter.worker_health()
    report["devices"] = health.data if health.ok else {"error": health.error.to_dict()}
    report["steps"].append({"step": "devices", "ok": health.ok,
                            "detail": {"cameras": (health.data or {}).get("cameras"),
                                       "microphones": len((health.data or {}).get("microphones", []))}})
    cams = (health.data or {}).get("cameras") or []
    has_cam = bool(cams)
    has_mic = bool((health.data or {}).get("microphones"))
    print(f"设备：摄像头 {len(cams)} 个，麦克风 {len((health.data or {}).get('microphones', []))} 个")

    # ---------- 2) 真实录音 → ASR ----------
    if has_mic:
        wav = MEDIA_DIR / "live_mic.wav"
        rec = adapter.record_audio(str(wav), seconds=4.0)
        if rec.ok:
            asr = adapter.transcribe(str(wav))
            report["asr"] = {"recording": rec.data, "ok": asr.ok,
                             "text": (asr.data or {}).get("text"),
                             "latency_ms": (asr.meta or {}).get("latency_ms"),
                             "error": asr.error.to_dict() if asr.error else None}
            report["steps"].append({"step": "mic_to_asr", "ok": asr.ok,
                                    "detail": {"text": (asr.data or {}).get("text"),
                                               "peak_amplitude": rec.data.get("peak_amplitude")}})
            print(f"麦克风→ASR：『{(asr.data or {}).get('text')}』")
        else:
            report["steps"].append({"step": "mic_to_asr", "ok": False,
                                    "detail": {"error": rec.error.to_dict() if rec.error else None}})
            print("录音失败：" + str(rec.error))
    else:
        report["steps"].append({"step": "mic_to_asr", "ok": False, "detail": "无麦克风"})

    # ---------- 3) ASR 对照（已知语音样例） ----------
    example = settings.MULTIMODAL_ROOT / "models" / "iic" / "SpeechRecognition" / "SenseVoiceSmall" / "example" / "zh.mp3"
    if example.exists():
        control = adapter.transcribe(str(example))
        report["asr_control"] = {"audio": example.name, "ok": control.ok,
                                 "text": (control.data or {}).get("text"),
                                 "expected_contains": "开饭时间",
                                 "error": control.error.to_dict() if control.error else None}
        report["steps"].append({"step": "asr_control", "ok": control.ok,
                                "detail": {"text": (control.data or {}).get("text")}})
        print(f"ASR 对照（已知语音）：『{(control.data or {}).get('text')}』")

    # ---------- 4) 真实抓帧 → VL ----------
    frame = MEDIA_DIR / "live_frame.jpg"
    if has_cam:
        cap = adapter.capture_frame(str(frame))
        if cap.ok:
            vl = adapter.describe_image(str(frame), "画面里有什么人或物品？简短描述。")
            report["vision"] = {"frame": cap.data, "ok": vl.ok,
                                "response": (vl.data or {}).get("response"),
                                "model": (vl.data or {}).get("model"),
                                "latency_ms": (vl.meta or {}).get("latency_ms"),
                                "error": vl.error.to_dict() if vl.error else None}
            report["steps"].append({"step": "camera_to_vl", "ok": vl.ok,
                                    "detail": {"response": (vl.data or {}).get("response")}})
            print(f"摄像头→VL：『{(vl.data or {}).get('response')}』")
        else:
            report["steps"].append({"step": "camera_to_vl", "ok": False,
                                    "detail": {"error": cap.error.to_dict() if cap.error else None}})
            print("抓帧失败：" + str(cap.error))

    # ---------- 5) 真实手势：单帧（边界：可能无手） ----------
    if has_cam:
        g1 = adapter.recognize_gesture(str(frame))
        report["gesture_live"] = {"ok": g1.ok, "data": g1.data,
                                  "error": g1.error.to_dict() if g1.error else None}
        report["steps"].append({"step": "gesture_live_frame", "ok": g1.ok,
                                "detail": {"hands": (g1.data or {}).get("hands_detected"),
                                           "gestures": (g1.data or {}).get("gestures"),
                                           "note": (g1.data or {}).get("note")}})
        print(f"手势（实景帧）：手 {(g1.data or {}).get('hands_detected')} 只，"
              f"手势 {(g1.data or {}).get('gestures')}，{(g1.data or {}).get('note')}")

    # ---------- 6) 真实手势：快照批量 ----------
    pattern = str(settings.MULTIMODAL_ROOT / "outputs" / "gestures" / "*.jpg")
    batch = adapter.guard("gesture_batch", lambda: adapter._run_worker(
        "gesture_batch", {"pattern": pattern, "limit": 60}))
    if batch.ok:
        data = batch.data or {}
        report["gesture_batch"] = {k: v for k, v in data.items() if k != "results"}
        report["gesture_batch_samples"] = [r for r in data.get("results", []) if r.get("gestures")][:6]
        report["steps"].append({"step": "gesture_batch", "ok": True,
                                "detail": {"scanned": data.get("scanned"),
                                           "with_hand": data.get("with_hand"),
                                           "with_gesture": data.get("with_gesture"),
                                           "gesture_counts": data.get("gesture_counts")}})
        print(f"手势批量：扫描 {data.get('scanned')} 张 → 检测到手 {data.get('with_hand')} 张 → "
              f"确认手势 {data.get('with_gesture')} 张，分布 {data.get('gesture_counts')}")
    else:
        report["steps"].append({"step": "gesture_batch", "ok": False,
                                "detail": {"error": batch.error.to_dict() if batch.error else None}})
        print("手势批量失败：" + str(batch.error))

    # ---------- 7) 契约一致性：真实输出 → InteractionEvent ----------
    samples = []
    asr_text = (report.get("asr") or {}).get("text") or ""
    if asr_text:
        ev = adapter.parse_event({"source": "voice", "text": asr_text}, user_id="15547")
        if ev.ok:
            samples.append((ev.data, "真实 ASR 文本 → InteractionEvent"))
    vl_text = (report.get("vision") or {}).get("response") or ""
    if vl_text:
        ev = adapter.parse_event({"source": "vision", "visual_tags": [
            {"tag": vl_text[:24], "confidence": 0.85}]}, user_id="15547")
        if ev.ok:
            samples.append((ev.data, "真实 VL 描述 → 视觉标签 → InteractionEvent"))
    for row in (report.get("gesture_batch_samples") or [])[:1]:
        g = row["gestures"][0]
        ev = adapter.parse_event({"source": "gesture",
                                  "gesture": {"name": g["name"], "confidence": g["confidence"]}},
                                 user_id="15547")
        if ev.ok:
            samples.append((ev.data, f"真实手势 {g['name']} → InteractionEvent"))

    for event, label in samples:
        report["contracts"].append(_contract_check(event, label))
    report["contracts_all_ok"] = all(c["ok"] and c["envelope_complete"] for c in report["contracts"])
    for c in report["contracts"]:
        print(f"契约 {label_marker(c)} {c['label']} → modality={c['modality']}, 信封完整={c['envelope_complete']}")

    # ---------- 8) 真实手势驱动反馈闭环 ----------
    thumbs = next((r for r in (report.get("gesture_batch_samples") or [])
                   if r.get("gestures") and r["gestures"][0]["name"] == "thumbs_up"), None)
    if thumbs:
        g = thumbs["gestures"][0]
        orch = Orchestrator(mode="auto", allow_real=True, with_voice=False)
        query = orch.handle({"source": "text", "text": "推荐一些装饰品"},
                            user_id="15547", session_id="real-gesture").data
        top1 = (query.get("recommendations") or [{}])[0].get("item_id")
        sess = orch.store.get("real-gesture")
        if sess is not None and top1:
            sess.focus_item_id = top1
            sess.last_item_ids = [top1] + list(sess.last_item_ids)
        fb = orch.handle({"source": "gesture",
                          "gesture": {"name": g["name"], "confidence": g["confidence"]}},
                         user_id="15547", session_id="real-gesture").data
        report["real_feedback_chain"] = {
            "gesture": g,
            "source_image": thumbs["image"],
            "status": fb.get("status"),
            "feedback": fb.get("feedback"),
            "before_after": fb.get("before_after"),
            "note": "真实手势快照 → MediaPipe 识别 → 适配器 → 反馈闭环",
        }
        report["steps"].append({"step": "real_gesture_feedback", "ok": fb.get("status") == "ok",
                                "detail": {"gesture": g["name"], "polarity": (fb.get("feedback") or {}).get("polarity")}})
        print(f"真实手势闭环：{g['name']} → polarity={(fb.get('feedback') or {}).get('polarity')}，"
              f"status={fb.get('status')}")

    report["adapter_calls"] = adapter.call_report()
    report["contracts_same_as_offline"] = report.get("contracts_all_ok", False)
    path = write_json(settings.PATHS["outputs"] / "real_multimodal_check.json", report)
    print("")
    print("[09_real_multimodal_check] 真实链路证据已写入 " + path.name)
    return 0


def label_marker(c: dict) -> str:
    return "[PASS]" if (c["ok"] and c["envelope_complete"]) else "[FAIL]"


if __name__ == "__main__":
    raise SystemExit(main())
