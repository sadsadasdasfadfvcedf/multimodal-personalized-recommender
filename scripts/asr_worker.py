"""
scripts/asr_worker.py
=====================

常驻 ASR worker：**一次加载 SenseVoiceSmall，多轮复用**。

为什么需要
----------
`multimodal_ai` 的 `ASREngine` 每次在新进程里构造都会重新加载模型。
实测单次 `/api/voice` 中「录音 4.3s + 转写 25s」，其中 25s 几乎全是模型加载。
常驻 worker 让后续每轮只付推理时间（约 1~2s）。

协议
----
stdin 每行一个 JSON：{"id": 1, "audio": "C:/.../x.wav", "sample_rate": 16000}
stdout 每行一个 JSON：{"event": "ready"} / {"id": 1, "ok": true, "text": "..."}
（库自身日志全部重定向到 stderr，避免污染协议）
"""

from __future__ import annotations

import json
import os
import sys


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

    from src.asr import ASREngine

    engine = ASREngine()
    try:
        engine._ensure_model()  # 加载 + 预热
    except Exception as exc:
        proto.write(json.dumps({"event": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False) + "\n")
        proto.flush()
        return 1

    proto.write(json.dumps({"event": "ready"}) + "\n")
    proto.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            text = engine.transcribe(str(req.get("audio") or ""), int(req.get("sample_rate", 16000)))
            out = {"id": req.get("id"), "ok": True, "text": text or ""}
        except Exception as exc:
            out = {"id": req.get("id"), "ok": False, "error": f"{type(exc).__name__}: {exc}"}
        proto.write(json.dumps(out, ensure_ascii=False) + "\n")
        proto.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
