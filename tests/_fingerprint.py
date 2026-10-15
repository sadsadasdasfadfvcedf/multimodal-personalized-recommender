"""证据指纹：把每次运行必然变化的随机量归一化后哈希。

为什么需要
----------
证据文件里含有 trace_id / session_id / 时间戳 等每次运行都不同的字段，
直接比对文件会永远「有差异」。归一化后哈希，才能把「行为是否变化」和
「随机 ID 是否变化」区分开，作为重构的回归判据。

归一化规则
----------
tr-<hex> / tr-error-<hex> / sess-...  ->  <前缀>-<ID>
形如 xxx-<hex10> 的后缀               ->  前缀 + <SUF>
ISO 时间戳                            ->  <TS>
bytes / total_bytes / files_scanned   ->  剔除（随内容与工具缓存变化）
键名含 time/latency/ms/duration/elapsed/generated -> 剔除
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

DROP_KEY = re.compile(r"(ms$|_ms$|latency|duration|elapsed|_at$|timestamp|generated|_seconds)", re.I)
ID_RE = re.compile(r"\b((?:tr|sess|session|run|req|ev|msg)(?:-[a-z]+)?)-[0-9a-fA-F]{6,}\b")
SUFFIX = re.compile(r"^(.+?)-[0-9a-f]{10}$")
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
DROP_VALUE_KEYS = {"bytes", "total_bytes", "total_mb", "files_scanned"}


def norm(obj):
    if isinstance(obj, dict):
        return {k: norm(v) for k, v in obj.items() if not DROP_KEY.search(str(k)) and str(k) not in DROP_VALUE_KEYS}
    if isinstance(obj, list):
        return [norm(x) for x in obj]
    if isinstance(obj, str):
        if ID_RE.search(obj):
            return ID_RE.sub(r"\1-<ID>", obj)
        m = SUFFIX.match(obj)
        if m:
            return m.group(1) + "-<SUF>"
        if ISO.match(obj):
            return "<TS>"
    return obj


def load(path: Path):
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return None
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return json.loads(text)


def fingerprint_dir(directory, names=None) -> dict:
    """返回 {文件名: sha256}。names 为 None 时扫描目录下全部 .json/.jsonl。"""
    directory = Path(directory)
    if names is None:
        names = sorted(p.name for p in directory.iterdir() if p.is_file() and p.suffix in (".json", ".jsonl"))
    out = {}
    for name in names:
        path = directory / name
        if not path.is_file():
            continue
        canon = json.dumps(norm(load(path)), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        out[name] = hashlib.sha256(canon.encode("utf-8")).hexdigest()
    return out
