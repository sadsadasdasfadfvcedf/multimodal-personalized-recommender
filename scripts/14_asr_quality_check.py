"""
scripts/14_asr_quality_check.py
===============================

真实 ASR 识别质量评测（SenseVoiceSmall）。

为什么需要这个脚本
------------------
`09_real_multimodal_check.py` 验证的是「调用成功 + 契约一致」：它把一次返回空文本的
真机识别也记成 ok=true。那是「链路通」，不是「识别准」。本脚本补的是后者：

    同一批固定测试音频 -> 同一个 ASR 引擎 -> 与参考文本逐字比对
    -> CER / 术语召回 / 空文本率 / 无效结果率 / 热推理延迟

评测口径（对齐 asr-benchmarking skill）
---------------------------------------
1. 采集端与转写端拆开测：本脚本把采集端固定为「SAPI 合成 16kHz 单声道」，
   只评转写端。因此结论是**转写端上限**，不等于「真人 + 麦克风」的端到端体验。
2. 测试集覆盖本项目六类意图与槽位词（预算/品类/颜色/场景/反馈/对比/解释），
   用例定义在 data/cases/asr_eval_cases.json（数据与代码分离）。
3. 术语锚点：每条用例声明 must_have，逐条检查是否出现在识别结果中。
4. 指标：
   - cer：字符级编辑距离 / 参考文本长度（双方先做数字与标点归一化）
   - cer_raw：不做数字归一化的原始 CER（透明披露，不作主指标）
   - empty：识别为空
   - invalid：非空但被引擎自带 is_valid_transcript 判为无效（幻觉/噪声）
   - latency_ms：热推理延迟（模型已加载并预热，不含冷启动）

前置条件（缺一即跳过并落盘，不影响离线可复现性）：
    INTEGRATED_ALLOW_REAL_MODELS=1，且 multimodal_ai 权重完整。

输出：outputs/asr_quality_check.json
产物：artifacts/asr_eval/*.wav（由 TTS 合成，可再生成）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

from _bootstrap import banner

from config import settings
from src.utils import now_iso, write_json

MEDIA_DIR = settings.PATHS["artifacts"] / "asr_eval"
CASE_FILE = settings.PATHS["cases"] / "asr_eval_cases.json"

# 预注册的判定阈值（先定标准再看数据，避免事后调参）
PASS_CER = 0.15
PASS_TERM_RECALL = 0.90

CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_RUN = re.compile("[" + "".join(CN_DIGITS) + "".join(CN_UNITS) + "]+")
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def _skip(reason: str, report: dict) -> int:
    report.update({"mode": "skipped", "reason": reason})
    path = write_json(settings.PATHS["outputs"] / "asr_quality_check.json", report)
    print("跳过：" + reason)
    print("[14_asr_quality_check] 证据已写入 " + path.name)
    return 0


# ------------------------------------------------------------
# 文本归一化与 CER
# ------------------------------------------------------------
def _cn_to_int(text: str) -> int | None:
    """中文数字转整数（评测侧独立实现，刻意不 import 产品代码，避免同源错误）。"""
    if not text:
        return None
    section = number = 0
    last_unit = 0
    for ch in text:
        if ch in CN_DIGITS:
            number = CN_DIGITS[ch]
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            section += (number or 1) * unit
            last_unit = unit
            number = 0
        else:
            return None
    if number and last_unit >= 10:
        section += number * (last_unit // 10)
        number = 0
    return (section + number) or None


def _canon(text: str) -> str:
    """数字与标点归一化：中文数字 -> 阿拉伯数字，去空格与标点，小写化。"""
    out: list[str] = []
    last = 0
    for m in _CN_RUN.finditer(text or ""):
        out.append((text or "")[last : m.start()])
        value = _cn_to_int(m.group(0))
        out.append(str(value) if value else m.group(0))
        last = m.end()
    out.append((text or "")[last:])
    return _PUNCT.sub("", "".join(out).lower())


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _cer(ref: str, hyp: str) -> float:
    base = _canon(ref)
    return round(_levenshtein(base, _canon(hyp)) / len(base), 4) if base else 0.0


def _cer_raw(ref: str, hyp: str) -> float:
    base = _PUNCT.sub("", (ref or "").lower())
    return round(_levenshtein(base, _PUNCT.sub("", (hyp or "").lower())) / len(base), 4) if base else 0.0


# ------------------------------------------------------------
# 音频合成与统计
# ------------------------------------------------------------
def _synthesize(cases: list[dict]) -> str:
    """用 Windows SAPI 合成 16kHz/16bit/单声道 WAV，返回音色描述。"""
    ps1 = MEDIA_DIR / "_tts.ps1"
    meta = MEDIA_DIR / "_tts_voice.txt"
    lines = [
        '$ErrorActionPreference = "Stop"',
        "$voice = New-Object -ComObject SAPI.SpVoice",
        "$fmt = New-Object -ComObject SAPI.SpAudioFormat",
        "$fmt.Type = 18",
        "$voice.Rate = 0",
        "$zh = $voice.GetVoices() | Where-Object { $_.GetDescription() -match 'Chinese|Huihui' } | Select-Object -First 1",
        "if ($zh) { $voice.Voice = $zh }",
        "$desc = if ($zh) { $zh.GetDescription() } else { 'default' }",
        "Set-Content -Path '" + str(meta).replace("'", "''") + "' -Value $desc -Encoding UTF8",
        "function Say([string]$text, [string]$path) {",
        "  $fs = New-Object -ComObject SAPI.SpFileStream",
        "  $fs.Format = $fmt",
        "  $fs.Open($path, 3, $false)",
        "  $voice.AudioOutputStream = $fs",
        "  $null = $voice.Speak($text)",
        "  $fs.Close()",
        "}",
    ]
    for case in cases:
        text = case["text"].replace("'", "''")
        wav = str((MEDIA_DIR / (case["id"] + ".wav")).resolve()).replace("'", "''")
        lines.append(f"Say '{text}' '{wav}'")
    ps1.write_bytes(("\r\n".join(lines) + "\r\n").encode("utf-8"))

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise RuntimeError("找不到 PowerShell，无法调用 SAPI 合成测试音频")
    proc = subprocess.run(
        [shell, "-NoProfile", "-File", str(ps1)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError("TTS 合成失败：" + (proc.stderr or proc.stdout or "")[-800:])
    return meta.read_text(encoding="utf-8").strip() if meta.exists() else "unknown"


def _audio_stats(path: Path) -> dict:
    import numpy as np

    with wave.open(str(path), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(frames)
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0 if width == 2 else np.zeros(0)
    peak = float(np.max(np.abs(samples))) if samples.size else 0.0
    rms = float(np.sqrt(np.mean(samples**2))) if samples.size else 0.0
    clipped = int(np.sum(np.abs(samples) >= 0.999)) if samples.size else 0
    return {
        "duration_s": round(frames / rate, 3) if rate else 0.0,
        "sample_rate": rate,
        "channels": channels,
        "sample_width": width,
        "peak_dbfs": round(20 * np.log10(peak), 1) if peak > 0 else None,
        "rms_dbfs": round(20 * np.log10(rms), 1) if rms > 0 else None,
        "clipped_samples": clipped,
    }


def _write_silence(path: Path, seconds: float = 0.5) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * int(16000 * seconds))


# ------------------------------------------------------------
# 子进程批量推理
# ------------------------------------------------------------
def _run_worker(items: list[dict], silence: Path, out_path: Path) -> dict:
    root = settings.MULTIMODAL_ROOT
    if root is None:
        raise RuntimeError("未找到 multimodal_ai 根目录")
    model_path = os.environ.get("MULTIMODAL_ASR_PATH") or str(
        Path(root) / "models" / "iic" / "SpeechRecognition" / "SenseVoiceSmall"
    )
    if not Path(model_path).is_dir():
        raise RuntimeError("找不到 ASR 权重目录：" + model_path)

    worker = settings.PROJECT_ROOT / "scripts" / "asr_eval_worker.py"
    params = {
        "root": str(root),
        "model_path": model_path,
        "files": items,
        "out": str(out_path),
        "silence": str(silence),
    }
    env = dict(os.environ)
    env["MM_ROOT"] = str(root)
    env["PYTHONPATH"] = str(root)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    if not os.getenv("INTEGRATED_MM_ONLINE"):
        env.setdefault("HF_HUB_OFFLINE", "1")
        env.setdefault("TRANSFORMERS_OFFLINE", "1")

    started = time.time()
    proc = subprocess.run(
        [sys.executable, "-B", str(worker), json.dumps(params, ensure_ascii=False)],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    wall_ms = round((time.time() - started) * 1000, 1)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError("ASR worker 失败：" + (proc.stderr or proc.stdout or "")[-1200:])
    payload = json.loads(out_path.read_text(encoding="utf-8"))
    payload["model_path"] = model_path
    payload["worker_wall_ms"] = wall_ms
    payload["stdout_tail"] = "\n".join((proc.stdout or "").strip().splitlines()[-6:])
    return payload


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main() -> int:
    banner("真实 ASR 识别质量评测 · SenseVoiceSmall")
    report: dict = {"checked_at": now_iso(), "mode": "real", "cases": []}

    if not settings.RUNTIME["allow_real_models"]:
        return _skip("未启用真实模型（设置 INTEGRATED_ALLOW_REAL_MODELS=1 后重试）", report)
    if settings.MULTIMODAL_ROOT is None:
        return _skip("未找到 multimodal_ai 根目录", report)
    if not CASE_FILE.exists():
        return _skip("缺少测试集 " + str(CASE_FILE), report)

    spec = json.loads(CASE_FILE.read_text(encoding="utf-8"))
    cases = spec["cases"]
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    # 1) 合成音频
    print(f"合成 {len(cases)} 条测试音频 ...")
    voice = _synthesize(cases)

    # 2) 音频客观指标 + 准备 worker 入参
    items: list[dict] = []
    audio_meta: dict[str, dict] = {}
    for case in cases:
        wav = MEDIA_DIR / (case["id"] + ".wav")
        if not wav.exists():
            raise RuntimeError("缺少合成音频：" + str(wav))
        stats = _audio_stats(wav)
        audio_meta[case["id"]] = stats
        items.append(
            {
                "id": case["id"],
                "path": str(wav.resolve()),
                "duration_ms": int(stats["duration_s"] * 1000),
            }
        )

    # 3) 子进程批量推理
    silence = MEDIA_DIR / "_silence.wav"
    _write_silence(silence)
    print("加载模型并批量转写（首次约 25s）...")
    payload = _run_worker(items, silence, MEDIA_DIR / "_asr_results.json")

    # 4) 指标
    by_id = {r["id"]: r for r in payload["results"]}
    rows: list[dict] = []
    for case in cases:
        result = by_id.get(case["id"], {"text": "", "valid": False, "latency_ms": None})
        hyp = result.get("text") or ""
        canon_hyp = _canon(hyp)
        hit = [t for t in case["must_have"] if _canon(t) and _canon(t) in canon_hyp]
        rows.append(
            {
                "id": case["id"],
                "intent": case["intent"],
                "edge": bool(case.get("edge")),
                "ref": case["text"],
                "hyp": hyp,
                "cer": _cer(case["text"], hyp),
                "cer_raw": _cer_raw(case["text"], hyp),
                "empty": not hyp.strip(),
                "valid": bool(result.get("valid")),
                "must_have": case["must_have"],
                "hit_terms": hit,
                "missed_terms": [t for t in case["must_have"] if t not in hit],
                "latency_ms": result.get("latency_ms"),
                "audio": audio_meta[case["id"]],
            }
        )

    main_rows = [r for r in rows if not r["edge"]]
    terms = [t for r in rows for t in r["must_have"]]
    hits = [t for r in rows for t in r["hit_terms"]]
    number_terms = [t for r in rows for t in r["must_have"] if t.isdigit()]
    number_hits = [t for r in rows for t in r["hit_terms"] if t.isdigit()]
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]

    cer_mean_main = round(sum(r["cer"] for r in main_rows) / len(main_rows), 4) if main_rows else 0.0
    cer_mean_all = round(sum(r["cer"] for r in rows) / len(rows), 4) if rows else 0.0
    term_recall = round(len(hits) / len(terms), 4) if terms else 0.0
    number_recall = round(len(number_hits) / len(number_terms), 4) if number_terms else None

    by_intent: dict[str, dict] = {}
    for row in rows:
        bucket = by_intent.setdefault(row["intent"], {"cases": 0, "cer_sum": 0.0, "term_total": 0, "term_hit": 0})
        bucket["cases"] += 1
        bucket["cer_sum"] += row["cer"]
        bucket["term_total"] += len(row["must_have"])
        bucket["term_hit"] += len(row["hit_terms"])
    for bucket in by_intent.values():
        bucket["cer_mean"] = round(bucket.pop("cer_sum") / bucket["cases"], 4)
        bucket["term_recall"] = round(bucket["term_hit"] / bucket["term_total"], 4) if bucket["term_total"] else 0.0

    verdict = "PASS" if (cer_mean_main <= PASS_CER and term_recall >= PASS_TERM_RECALL) else "WARN"

    report.update(
        {
            "engine": {"name": "SenseVoiceSmall", "model_path": payload.get("model_path")},
            "setup": {
                "tts_voice": voice,
                "sample_rate": 16000,
                "channels": 1,
                "case_count": len(cases),
                "case_file": str(CASE_FILE.relative_to(settings.PROJECT_ROOT)),
                "thresholds": {"cer_mean_main": PASS_CER, "term_recall": PASS_TERM_RECALL},
            },
            "load_ms": payload.get("load_ms"),
            "worker_wall_ms": payload.get("worker_wall_ms"),
            "summary": {
                "cases": len(rows),
                "empty": sum(1 for r in rows if r["empty"]),
                "empty_ids": [r["id"] for r in rows if r["empty"]],
                "invalid": sum(1 for r in rows if not r["valid"]),
                "invalid_ids": [r["id"] for r in rows if not r["valid"]],
                "cer_mean_all": cer_mean_all,
                "cer_mean_main": cer_mean_main,
                "term_recall": term_recall,
                "term_hits": len(hits),
                "term_total": len(terms),
                "number_recall": number_recall,
                "latency_ms": {
                    "mean": round(sum(latencies) / len(latencies), 1) if latencies else None,
                    "p50": round(sorted(latencies)[len(latencies) // 2], 1) if latencies else None,
                    "max": round(max(latencies), 1) if latencies else None,
                },
                "verdict": verdict,
            },
            "by_intent": by_intent,
            "cases": rows,
            "limitations": [
                "采集端固定为 SAPI 合成语音（无口音、无背景噪声、无停顿），结论是转写端上限，不等于真人+麦克风的端到端体验。",
                "热推理延迟不含冷启动；冷启动耗时单独记在 load_ms。",
                "X-01（单字「嗯」）属于边界用例，计入 cer_mean_all，不计入 cer_mean_main。",
                "CER 对中文数字与标点做了归一化；cer_raw 保留未归一化结果供核对。",
            ],
        }
    )

    # 采集端对照：09 真机检查里那段实录音频的客观指标。
    # 用于把「模型识别不准」与「麦克风输入太小」区分开（双变量拆分）。
    real_mic = settings.PATHS["artifacts"] / "real" / "live_mic.wav"
    if real_mic.exists():
        report["capture_reference"] = {
            "path": str(real_mic.relative_to(settings.PROJECT_ROOT)),
            "audio": _audio_stats(real_mic),
            "observed_asr_text": "",
            "note": "见 outputs/real_multimodal_check.json：真机 4s 录音经 ASR 返回空文本但被记为 ok=true",
        }

    path = write_json(settings.PATHS["outputs"] / "asr_quality_check.json", report)

    s = report["summary"]
    print("\n用例  意图         CER   术语命中  识别结果")
    for row in rows:
        print(
            f"{row['id']:<5} {row['intent']:<11} {row['cer']:<5} "
            f"{len(row['hit_terms'])}/{len(row['must_have'])}      {row['hyp'] or '(空)'}"
        )
    print("\n汇总：")
    print(f"  用例数        {s['cases']}（空文本 {s['empty']}，无效 {s['invalid']}）")
    print(f"  CER（主体）   {s['cer_mean_main']:.3f}  (阈值 {PASS_CER})")
    print(f"  CER（全部）   {s['cer_mean_all']:.3f}")
    print(f"  术语召回      {s['term_recall']:.3f}  ({s['term_hits']}/{s['term_total']}，阈值 {PASS_TERM_RECALL})")
    print(f"  数字召回      {s['number_recall']}")
    print(f"  热推理延迟    {s['latency_ms']}")
    print(f"  冷启动        {report['load_ms']} ms")
    print(f"  判定          {s['verdict']}")
    print(f"\n[14_asr_quality_check] 证据已写入 {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
