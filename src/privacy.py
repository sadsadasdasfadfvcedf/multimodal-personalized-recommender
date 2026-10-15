"""
src/privacy.py
==============

隐私与提交包安全检查（阶段6）。

检查三类风险：
    1. 禁止文件进入提交包（__pycache__ / *.pyc / 模型权重 / 音视频 / 数据库 / 压缩包 / .env）
    2. 源码中硬编码密钥（password / token / api_key 等出现字面量赋值）
    3. 绝对路径硬编码（Windows 盘符路径 / 用户目录绝对路径）

返回结构化报告，verdict 只有 PASS / FAIL 两种，便于验收引用。
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from config import settings

CFG = settings.PRIVACY_CONFIG

# 允许出现在 package 中的证据目录（这些目录本身是交付物）
ALLOWED_DIRS = set(CFG["allowed_dirs"])
# 只对源码目录检查「绝对路径硬编码」；outputs/ 属于运行留痕，允许记录外部系统位置
CODE_DIRS = {"config", "src", "scripts"}

_SENSITIVE_ASSIGN = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|api_key|apikey|access_token|auth_token|private_key)\b\s*[:=]\s*"
    r"([\"'])(?P<value>.+?)\2")
_ABS_WIN = re.compile(r"[A-Za-z]:\\\\?[Uu]sers|C:\\\\|D:\\\\")

# 值里出现这些占位内容不算硬编码密钥
_PLACEHOLDER = ("", "none", "null", "xxx", "***", "changeme", "your_", "<", "${", "os.getenv", "env[")


def _iter_files(root: Path):
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def scan_tree(root: Path | str | None = None) -> dict[str, Any]:
    """扫描项目树，返回隐私/包体检查报告。"""
    root = Path(root) if root else settings.PROJECT_ROOT
    forbidden: list[dict] = []
    sensitive_hits: list[dict] = []
    absolute_path_hits: list[dict] = []
    scanned = 0

    for path in _iter_files(root):
        rel = _relative(path, root)
        parts = Path(rel).parts
        in_allowed = any(part in ALLOWED_DIRS for part in parts[:-1]) or (parts and parts[0] in ALLOWED_DIRS)
        scanned += 1

        # 1) 禁止文件类型
        matched = next((pat for pat in CFG["forbidden_patterns"] if fnmatch.fnmatch(path.name, pat)), None)
        if matched and not in_allowed:
            forbidden.append({"path": rel, "pattern": matched,
                              "size_bytes": path.stat().st_size,
                              "reason": "禁止进入提交包的文件类型"})

        # 2) 文本内容检查
        if path.suffix.lower() in CFG["scan_text_extensions"]:
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not in_allowed:
                for m in _SENSITIVE_ASSIGN.finditer(text):
                    value = m.group("value").strip()
                    if any(value.lower().startswith(p) or value.lower() == p for p in _PLACEHOLDER):
                        continue
                    sensitive_hits.append({"path": rel, "key": m.group(1),
                                           "line": text[:m.start()].count("\n") + 1,
                                           "value_preview": value[:12] + "…" if len(value) > 12 else value})
            # 绝对路径：只检查源码目录，且跳过正则定义行
            if parts and parts[0] in CODE_DIRS:
                for i, line in enumerate(text.splitlines(), start=1):
                    stripped = line.strip()
                    if not _ABS_WIN.search(line):
                        continue
                    if stripped.startswith("#") or "re.compile(" in stripped:
                        continue
                    absolute_path_hits.append({"path": rel, "line": i, "snippet": stripped[:100]})
                    break

    verdict = "PASS" if not forbidden and not sensitive_hits and not absolute_path_hits else "FAIL"
    return {
        "root": str(root),
        "files_scanned": scanned,
        "verdict": verdict,
        "forbidden_files": forbidden,
        "sensitive_literals": sensitive_hits,
        "absolute_path_literals": absolute_path_hits,
        "allowed_evidence_dirs": sorted(ALLOWED_DIRS),
        "policy": {
            "禁止文件类型": CFG["forbidden_patterns"],
            "敏感键名": CFG["sensitive_keys"],
            "允许的证据目录": sorted(ALLOWED_DIRS),
        },
    }


def package_manifest(root: Path | str | None = None) -> dict[str, Any]:
    """列出「应当进入最终提交包」的内容（不含缓存/模型/音视频）。"""
    root = Path(root) if root else settings.PROJECT_ROOT
    include_dirs = ["config", "src", "scripts", "docs", "data", "outputs"]
    include_files = ["README.md", "requirements.txt", "main.py", "environment.yml", ".gitignore"]
    entries: list[dict] = []
    for name in include_dirs:
        target = root / name
        if not target.exists():
            continue
        for path in sorted(target.rglob("*")):
            if not path.is_file():
                continue
            if any(fnmatch.fnmatch(path.name, pat) for pat in CFG["forbidden_patterns"]):
                continue
            entries.append({"path": _relative(path, root), "size_bytes": path.stat().st_size})
    for name in include_files:
        target = root / name
        if target.exists():
            entries.append({"path": name, "size_bytes": target.stat().st_size})
    total = sum(e["size_bytes"] for e in entries)
    return {"entries": len(entries), "total_bytes": total,
            "total_mb": round(total / 1024 / 1024, 2),
            "excluded_dirs": ["artifacts (可再生的语音产物)", "__pycache__", "*.pyc"],
            "files": entries}
