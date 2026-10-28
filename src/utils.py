"""
src/utils.py
============

通用工具：trace_id、时间戳、JSON 落盘、轻量校验、耗时记录。

所有输出统一 UTF-8；所有时间戳统一带时区的 ISO8601（由 UTC 生成，避免本机时区漂移）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ------------------------------------------------------------
# trace / id
# ------------------------------------------------------------
TRACE_PREFIX = "tr"


def ensure_utf8_stdio() -> None:
    """把命令行入口的 stdout/stderr 固定为 UTF-8，避免中文 Windows GBK 崩溃。"""
    if os.name == "nt":
        with suppress(Exception):
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        reconfig = getattr(stream, "reconfigure", None)
        if callable(reconfig):
            with suppress(Exception):
                reconfig(encoding="utf-8", errors="replace")


def new_trace_id(seed_hint: str | None = None) -> str:
    """生成 trace_id。给定 hint 时生成确定性 ID（便于复现同一请求）。"""
    if seed_hint:
        digest = hashlib.sha1(seed_hint.encode("utf-8")).hexdigest()[:16]
        return f"{TRACE_PREFIX}-{digest}"
    return f"{TRACE_PREFIX}-{uuid.uuid4().hex[:16]}"


def stable_hash_int(*parts: str, mod: int = 10**12) -> int:
    """跨进程稳定的数值摘要（用于必须可复现的派生 ID）。

    为什么不能用内置 hash()：str.__hash__ 受 PYTHONHASHSEED 随机化影响，
    同一输入在不同进程会得到不同结果 —— 会让证据与文档失去可复现性。
    """
    digest = hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()
    return int(digest, 16) % mod


def now_iso() -> str:
    """当前时间（UTC，ISO8601，秒级）。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_iso(value: str) -> datetime:
    """解析 ISO8601；支持以 Z 结尾。"""
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def is_iso8601(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parse_iso(value)
        return True
    except Exception:
        return False


def stable_key(*parts: Any) -> str:
    """把若干字段拼成稳定字符串（用于确定性 trace 与去重）。"""
    return "|".join("" if p is None else str(p) for p in parts)


# ------------------------------------------------------------
# JSON 读写
# ------------------------------------------------------------
def _json_default(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return str(obj)


def dumps(obj: Any, *, indent: int | None = 2, ensure_ascii: bool = False) -> str:
    return json.dumps(obj, ensure_ascii=ensure_ascii, indent=indent, default=_json_default, sort_keys=False)


def write_json(path: str | Path, obj: Any, *, indent: int | None = 2) -> Path:
    """原子写 JSON（先写临时文件再替换），避免半截文件。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(dumps(obj, indent=indent) + "\n", encoding="utf-8")
    tmp.replace(target)
    return target


def read_json(path: str | Path, default: Any = None) -> Any:
    target = Path(path)
    if not target.exists():
        return default
    with target.open("r", encoding="utf-8-sig") as fh:
        return json.load(fh)


def append_jsonl(path: str | Path, records: Iterable[dict]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    return target


def write_jsonl(path: str | Path, records: Iterable[dict]) -> Path:
    """覆盖写 JSONL。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False, default=_json_default) + "\n")
    return target


def read_jsonl(path: str | Path) -> list[dict]:
    target = Path(path)
    if not target.exists():
        return []
    out: list[dict] = []
    with target.open("r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


# ------------------------------------------------------------
# 轻量校验（不引入 pydantic 依赖，保证离线可跑）
# ------------------------------------------------------------
def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and value.strip() != ""


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def minmax_normalize(values: dict[str, float]) -> dict[str, float]:
    """把 {key: score} 归一化到 [0,1]；全相等时统一给 1.0。"""
    if not values:
        return {}
    lo = min(values.values())
    hi = max(values.values())
    if hi - lo < 1e-12:
        return dict.fromkeys(values, 1.0)
    return {k: (v - lo) / (hi - lo) for k, v in values.items()}


# ------------------------------------------------------------
# 耗时记录
# ------------------------------------------------------------
@contextmanager
def timed(collector: dict, key: str):
    """with timed(stage, "recall_ms"): ...  → 把耗时(ms)写入 collector。"""
    start = time.perf_counter()
    try:
        yield
    finally:
        collector[key] = round((time.perf_counter() - start) * 1000, 3)


def new_timer() -> Timer:
    return Timer()


class Timer:
    """累计型计时器，输出 {name: ms} 与总耗时。"""

    def __init__(self) -> None:
        self._marks: dict[str, float] = {}
        self._start = time.perf_counter()

    @contextmanager
    def track(self, name: str):
        t0 = time.perf_counter()
        try:
            yield
        finally:
            self._marks[name] = round(self._marks.get(name, 0.0) + (time.perf_counter() - t0) * 1000, 3)

    @property
    def total_ms(self) -> float:
        return round((time.perf_counter() - self._start) * 1000, 3)

    def as_dict(self) -> dict[str, float]:
        out = dict(self._marks)
        out["total_ms"] = self.total_ms
        return out


# ------------------------------------------------------------
# 文本
# ------------------------------------------------------------
def truncate(text: str, limit: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def contains_any(text: str, needles: Iterable[str]) -> str | None:
    """返回第一个命中的关键词（大小写不敏感）。"""
    if not text:
        return None
    lowered = text.lower()
    for needle in needles:
        if needle and needle.lower() in lowered:
            return needle
    return None


_ASCII_WORD = re.compile(r"[a-z0-9]+")
_ASCII_TERM_CACHE: dict[str, re.Pattern[str]] = {}


def title_has_term(title_lower: str, term: str) -> bool:
    """标题是否命中某个词。

    中文用子串（「相框」能命中「白色木质相框」）；英文必须按单词边界匹配，
    否则 `wood` 会命中 `WOODLAND`、`sea` 会命中 `SEASON`、`star` 会命中
    `STARFISH` —— 材质/主题/颜色召回会因此混入语义无关的商品。
    英文允许常见词形变化（复数、-ed、-ing），如 `FLOWERS`、`KNITTED`。
    """
    term = str(term or "").strip().lower()
    if not term:
        return False
    if not _ASCII_WORD.fullmatch(term):
        return term in title_lower
    pattern = _ASCII_TERM_CACHE.get(term)
    if pattern is None:
        # 不用 \b：Python 把中文也当单词字符，「木质black相框」这类中英相邻
        # 的标题会失去边界。显式用「前后不是英文字母/数字」定义词边界。
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?:s|es|ed|ing)?(?![a-z0-9])")
        _ASCII_TERM_CACHE[term] = pattern
    return pattern.search(title_lower) is not None
