"""
scripts/camera_worker.py
========================

常驻摄像头 + 手势识别 worker：**摄像头只打开一次，持续输出预览与手势事件**。

与 `mm_worker.py gesture` 的区别
--------------------------------
- 单次识别：每次点击都重新打开摄像头、重新加载 MediaPipe；
- 本 worker：摄像头常开，MediaPipe Hands 只初始化一次，持续写：
    <out_dir>/latest.jpg   最新一帧（供 MJPEG 预览）
    <out_dir>/state.json   当前手势 / 稳定事件（供网页轮询）

协议
----
启动参数（argv[1]，JSON）：
    {"root": "<multimodal_ai>", "out_dir": "...", "camera_index": 0,
     "width": 640, "height": 480, "fps": 12}
stdout 只在启动阶段输出 {"event":"ready"} 或 {"event":"error"}。
stdin 收到 {"op":"stop"} 时退出。

识别策略
--------
网页端是用户主动开启的直播，因此使用宽松档：低检测阈值、不做人脸误检过滤；
同一手势连续 3 帧且当前处于「已武装」状态才触发一次事件；手离开画面 3 帧后重新武装，
避免一直举着 👍 时每 2 秒重复点赞。
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path


def _atomic_json(path: Path, data: dict) -> None:
    """直接写最终文件，避免 Windows 上 replace 被并发读者锁住导致掉帧。"""
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _imwrite(path: Path, frame) -> None:
    import cv2

    ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
    if ok:
        buf.tofile(str(path))


def main() -> int:
    params = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    proto = os.fdopen(os.dup(sys.stdout.fileno()), "w", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr

    root = params.get("root")
    if not root:
        proto.write(json.dumps({"event": "error", "error": "missing root"}) + "\n")
        proto.flush()
        return 2
    sys.path.insert(0, root)

    out_dir = Path(params.get("out_dir") or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    index = int(params.get("camera_index", 0))
    width = int(params.get("width", 640))
    height = int(params.get("height", 480))
    fps_target = max(2.0, min(30.0, float(params.get("fps", 12))))

    try:
        import cv2
        import mediapipe as mp
        from src.gesture import GestureRecognizer

    except Exception as exc:
        proto.write(json.dumps({"event": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
        proto.flush()
        return 1

    # 与网页单次手势一致的宽松档。运行时 cwd/PYTHONPATH 指向 multimodal_ai；
    # mypy 会把 `config` 解析成本项目的 config 包，因此用动态导入避免静态误报。
    detection = importlib.import_module("config").DETECTION_CONFIG
    detection["gesture_min_confidence"] = 0.50
    detection["gesture_thumb_min_confidence"] = 0.45
    detection["gesture_margin"] = 0.03

    cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        proto.write(
            json.dumps({"event": "error", "error": f"摄像头打开失败: index={index}"}, ensure_ascii=False) + "\n"
        )
        proto.flush()
        return 1
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    hands = mp.solutions.hands.Hands(
        max_num_hands=1,
        min_detection_confidence=0.05,
        min_tracking_confidence=0.05,
    )
    mp_draw = mp.solutions.drawing_utils
    mp_hands = mp.solutions.hands
    recognizer = GestureRecognizer(stable_frames=1)

    stop = threading.Event()

    def _reader() -> None:
        for line in sys.stdin:
            try:
                req = json.loads(line)
            except Exception:
                continue
            if req.get("op") == "stop":
                stop.set()
                return

    threading.Thread(target=_reader, daemon=True).start()

    for _ in range(10):
        cap.read()

    proto.write(json.dumps({"event": "ready"}) + "\n")
    proto.flush()

    event_id = 0
    events: list[dict] = []
    last_event_gesture: str | None = None
    last_event_confidence = 0.0
    last_event_at = 0.0
    streak_name: str | None = None
    streak_count = 0
    armed = True
    no_hand_frames = 0
    frame_times: list[float] = []
    last_state_write = 0.0
    state = {
        "running": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "fps": 0.0,
        "hands": 0,
        "current_gesture": None,
        "current_confidence": 0.0,
        "event_id": 0,
        "last_event_gesture": None,
        "last_event_confidence": 0.0,
        "last_event_at": None,
        "events": [],
    }

    try:
        while not stop.is_set():
            t0 = time.time()
            ok, frame = cap.read()
            if not ok or frame is None:
                time.sleep(0.05)
                continue
            frame = cv2.flip(frame, 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            rgb.flags.writeable = False
            res = hands.process(rgb)

            current = None
            conf = 0.0
            best_name = None
            best_score = 0.0
            n_hands = 0
            if res.multi_hand_landmarks:
                n_hands = len(res.multi_hand_landmarks)
                lm = res.multi_hand_landmarks[0]
                picked = GestureRecognizer.pick_hand([lm], getattr(res, "multi_handedness", None))
                if picked is not None:
                    current, conf = recognizer._classify(picked.landmark)
                    scores = recognizer.score_candidates(picked.landmark)
                    if scores:
                        best_name, best_score = max(scores.items(), key=lambda item: item[1])
                    mp_draw.draw_landmarks(
                        frame,
                        picked,
                        mp_hands.HAND_CONNECTIONS,
                        mp_draw.DrawingSpec(color=(0, 255, 128), thickness=2, circle_radius=2),
                        mp_draw.DrawingSpec(color=(255, 255, 255), thickness=1),
                    )

            if current:
                no_hand_frames = 0
                if current == streak_name:
                    streak_count += 1
                else:
                    streak_name, streak_count = current, 1
                # 换了一个手势（例如 👍 → 👎）时立即重新武装，不要求手先离开画面
                if last_event_gesture is not None and current != last_event_gesture:
                    armed = True
                if armed and streak_count >= 3:
                    event_id += 1
                    last_event_gesture = current
                    last_event_confidence = conf
                    last_event_at = time.time()
                    events.append(
                        {
                            "id": event_id,
                            "gesture": current,
                            "confidence": round(conf, 4),
                            "at": datetime.fromtimestamp(last_event_at, UTC).isoformat(),
                        }
                    )
                    events = events[-10:]
                    armed = False
            else:
                streak_name, streak_count = None, 0
                no_hand_frames += 1
                if no_hand_frames >= 3:
                    armed = True

            label = current or best_name
            score = conf if current else best_score
            text = f"{label} {score:.2f}" if label else "no gesture"
            cv2.rectangle(frame, (8, 8), (360, 48), (0, 0, 0), -1)
            color = (0, 255, 128) if current else (200, 200, 200)
            cv2.putText(frame, text, (16, 38), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2, cv2.LINE_AA)

            _imwrite(out_dir / "latest.jpg", frame)

            now = time.time()
            frame_times.append(now)
            frame_times = [t for t in frame_times if now - t < 2.0]
            fps = len(frame_times) / 2.0
            if now - last_state_write >= 0.1:
                state.update(
                    {
                        "running": True,
                        "timestamp": datetime.now(UTC).isoformat(),
                        "fps": round(fps, 1),
                        "hands": n_hands,
                        "current_gesture": label,
                        "current_confidence": round(score, 4),
                        "event_id": event_id,
                        "last_event_gesture": last_event_gesture,
                        "last_event_confidence": round(last_event_confidence, 4),
                        "last_event_at": (
                            datetime.fromtimestamp(last_event_at, UTC).isoformat() if last_event_at else None
                        ),
                        "events": events[-10:],
                    }
                )
                _atomic_json(out_dir / "state.json", state)
                last_state_write = now

            delay = 1.0 / fps_target - (time.time() - t0)
            if delay > 0:
                time.sleep(delay)
    finally:
        with contextlib.suppress(Exception):
            hands.close()
        with contextlib.suppress(Exception):
            cap.release()
        state["running"] = False
        state["timestamp"] = datetime.now(UTC).isoformat()
        with contextlib.suppress(Exception):
            _atomic_json(out_dir / "state.json", state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
