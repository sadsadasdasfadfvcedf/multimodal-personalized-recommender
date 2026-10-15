"""
src/presentation.py
===================

推荐结果的交互呈现（阶段4：文本 / 语音）。

语音引擎优先级：
    1. edge-tts（在线，音质好；需要网络）
    2. Windows SAPI（离线，System.Speech；随 Windows 提供）
    3. 不可用 → 仅文本呈现，voice_available=false（L2 降级，会记录到证据里）

所有失败都被记录为结构化降级事件，不会静默吞掉。
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from config import settings

CFG = settings.PRESENTATION_CONFIG


def _cleanup_partial(path: Path) -> None:
    """引擎失败时删掉它留下的半成品/空文件，避免 0 字节产物被误当成有效音频。"""
    try:
        if path.exists() and path.stat().st_size == 0:
            path.unlink()
    except OSError:
        pass


def build_text(items: list[dict], *, header: str | None = None) -> str:
    """把推荐结果组装成一段可读中文文本。"""
    limit = CFG["max_items_in_text"]
    lines: list[str] = []
    if header:
        lines.append(header)
    for i, item in enumerate(items[:limit], start=1):
        title = item.get("title") or item.get("item_id")
        price = item.get("price")
        symbol = settings.CURRENCY_SYMBOLS.get(str(item.get("currency", "GBP")).upper(), "")
        price_text = f"（参考价 {symbol}{price:g}）" if isinstance(price, (int, float)) else ""
        reason = item.get("explanation") or item.get("reason") or ""
        reason = reason.replace("推荐理由：", "") if reason else ""
        if len(reason) > 60:
            reason = reason[:59] + "…"
        lines.append(f"{i}. {title}{price_text}" + (f" —— {reason}" if reason else ""))
    if not lines:
        return "暂时没有可展示的推荐结果。"
    return "\n".join(lines)


def build_spoken_text(items: list[dict], *, header: str | None = None) -> str:
    """组装适合朗读的短文本（去掉序号符号、截断长解释）。"""
    limit = min(2, CFG["max_items_in_text"])
    parts: list[str] = [header] if header else []
    for item in items[:limit]:
        title = item.get("title") or item.get("item_id")
        parts.append(f"{title}。")
    if len(parts) <= (1 if header else 0):
        return "暂时没有可播报的推荐结果。"
    return "".join(parts)


def _edge_tts_synthesize(text: str, out_path: Path, voice: str, timeout_s: int) -> tuple[bool, str]:
    try:
        import asyncio

        import edge_tts  # type: ignore
    except Exception as exc:  # 未安装
        return False, f"edge-tts 不可用: {type(exc).__name__}"

    async def _run() -> None:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(out_path))

    try:
        asyncio.run(asyncio.wait_for(_run(), timeout=timeout_s))
    except Exception as exc:
        _cleanup_partial(out_path)
        return False, f"edge-tts 合成失败: {type(exc).__name__}: {exc}"
    if not (out_path.exists() and out_path.stat().st_size > 0):
        _cleanup_partial(out_path)
        return False, "edge-tts 未产生有效音频"
    return True, "edge-tts"


def _sapi_synthesize(text: str, out_path: Path, timeout_s: int) -> tuple[bool, str]:
    """Windows 离线 TTS（System.Speech）。"""
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return False, "未找到 PowerShell，无法使用 Windows 离线 TTS"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as fh:
        fh.write(text)
        text_path = Path(fh.name)
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        f"$t = Get-Content -Raw -Encoding UTF8 -LiteralPath '{text_path}';"
        f"$s.SetOutputToWaveFile('{out_path}');"
        "$s.Speak($t);"
        "$s.Dispose();"
    )
    try:
        proc = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script],
                              capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _cleanup_partial(out_path)
        return False, "Windows 离线 TTS 超时"
    finally:
        text_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        _cleanup_partial(out_path)
        return False, f"Windows 离线 TTS 失败: {(proc.stderr or '').strip()[:160]}"
    if not (out_path.exists() and out_path.stat().st_size > 0):
        _cleanup_partial(out_path)
        return False, "Windows 离线 TTS 未产生有效音频"
    return True, "windows-sapi"


def synthesize(text: str, *, out_dir: Path | None = None, voice: str | None = None,
               engine: str | None = None) -> dict:
    """合成语音。返回 {"available", "path", "engine", "error", "bytes"}。"""
    voice_cfg = CFG["voice"]
    out_dir = Path(out_dir) if out_dir else settings.PATHS["artifacts"]
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(voice_cfg["output_name"]).stem
    edge_path = out_dir / f"{stem}.mp3"     # edge-tts 输出 mp3
    sapi_path = out_dir / f"{stem}.wav"     # Windows SAPI 输出 wav
    timeout_s = int(voice_cfg["timeout_s"])
    zh_voice = voice or voice_cfg["voice_zh"]
    prefer = engine or voice_cfg["engine"]

    if not text.strip():
        return {"available": False, "path": None, "engine": None, "error": "待合成文本为空", "bytes": 0}

    attempts: list[tuple[str, Path, callable]] = []
    if prefer == "edge-tts":
        attempts.append(("edge-tts", edge_path, lambda: _edge_tts_synthesize(text, edge_path, zh_voice, timeout_s)))
    attempts.append(("windows-sapi", sapi_path, lambda: _sapi_synthesize(text, sapi_path, timeout_s)))
    if prefer != "edge-tts":
        attempts.append(("edge-tts", edge_path, lambda: _edge_tts_synthesize(text, edge_path, zh_voice, timeout_s)))

    errors: list[str] = []
    for name, path, fn in attempts:
        ok_flag, detail = fn()
        if ok_flag:
            return {"available": True, "path": str(path), "engine": detail,
                    "error": None, "bytes": path.stat().st_size, "fallbacks": errors}
        errors.append(f"{name}: {detail}")
    return {"available": False, "path": None, "engine": None,
            "error": "; ".join(errors), "bytes": 0}


def present(items: list[dict], *, channel: str = "text", header: str | None = None,
            with_voice: bool = True, dry_run: bool = False) -> dict:
    """统一呈现入口：文本 + 可选语音。"""
    text = build_text(items, header=header)
    spoken = build_spoken_text(items, header=header)
    result = {
        "channel": channel,
        "text": text,
        "spoken_text": spoken,
        "item_count": len(items),
        "voice_available": False,
        "audio_path": None,
        "engine": None,
        "degradations": [],
    }
    voice_cfg = CFG["voice"]
    if with_voice and voice_cfg["enabled"] and "voice" in CFG["channels"] and not dry_run:
        voice_result = synthesize(spoken)
        result["voice_available"] = voice_result["available"]
        result["audio_path"] = voice_result["path"]
        result["engine"] = voice_result["engine"]
        if not voice_result["available"]:
            result["degradations"].append({
                "level": "L2", "reason": f"语音呈现不可用，已降级为纯文本: {voice_result['error']}",
                "from": "voice+text", "to": "text",
            })
    return result
