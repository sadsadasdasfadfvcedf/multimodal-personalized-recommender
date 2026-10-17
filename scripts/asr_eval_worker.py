"""
scripts/asr_eval_worker.py
==========================

批量 ASR 评测 worker —— 在 multimodal_ai 的命名空间里执行。

为什么必须单独一个子进程
------------------------
外部 multimodal_ai 与本项目都有 `config` / `src` 包，同进程 import 会互相覆盖
（`src/asr.py` 用的是 `from config import ASR_CONFIG` 这种绝对导入）。
因此本 worker 由 `14_asr_quality_check.py` 以 `cwd=<multimodal_ai 根目录>` 启动。

与 mm_worker.action_asr 的区别
-----------------------------
`action_asr` 每调用一次就重新构造 ASREngine（冷启动约 25s）。
评测「识别质量 / 热推理延迟」必须把冷启动排除在外，所以这里一次加载、
批量转写，并把加载耗时单独记录。

入参（argv[1]，JSON）：
    {"root": "...", "model_path": "...", "files": [{"id","path","duration_ms"}],
     "out": "...", "silence": "..."}

出参：写入 out 指向的 JSON 文件
    {"load_ms": ..., "results": [{"id","text","valid","latency_ms"}]}
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: asr_eval_worker.py '<json params>'", file=sys.stderr)
        return 2

    params = json.loads(sys.argv[1])
    sys.path.insert(0, params["root"])

    from src.asr import ASREngine

    out_path = Path(params["out"])
    results: list[dict] = []

    started = time.time()
    engine = ASREngine(model_path=params["model_path"])
    silence = params.get("silence")
    if silence:
        # 首次 transcribe 会触发懒加载 + 内置 warmup；用静音把它顶掉，
        # 这样 load_ms 包含冷启动，而用例延迟只反映热推理。
        engine.transcribe(silence)
    load_ms = (time.time() - started) * 1000

    for item in params["files"]:
        t0 = time.time()
        text = engine.transcribe(item["path"]) or ""
        elapsed = (time.time() - t0) * 1000
        # 用引擎自带的有效性过滤（短噪声/单字母/语气词/过短音频）作为「幻觉」判据
        with contextlib.suppress(Exception):
            valid = bool(engine.is_valid_transcript(text, int(item.get("duration_ms") or 0)))
        results.append(
            {
                "id": item["id"],
                "text": text,
                "valid": valid,
                "latency_ms": round(elapsed, 2),
            }
        )
        print(f"[asr_eval] {item['id']}: {text!r} valid={valid} ({elapsed:.0f}ms)", flush=True)

    with contextlib.suppress(Exception):
        engine.release()

    payload = {"load_ms": round(load_ms, 2), "results": results}
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"__ASR_EVAL_DONE__ {len(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
