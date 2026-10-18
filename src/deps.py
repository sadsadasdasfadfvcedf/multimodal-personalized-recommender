"""
src/deps.py
===========

真实多模态模式的依赖兼容性检查。

为什么单独一个模块
------------------
mediapipe / torchvision / transformers 是一组隐式耦合的依赖，版本错配时
报错完全不指向根因（均为实测）：

    protobuf 7 + mediapipe 0.10.14
        -> 'MessageFactory' object has no attribute 'GetPrototype'
    torchvision 0.23 + torch 2.6
        -> RuntimeError: operator torchvision::nms does not exist
    torchvision 坏了
        -> transformers: ModuleNotFoundError: Could not import module 'AutoProcessor'

这里把「包能不能用」变成一条可读结论：
    check_stack(deep=False)  只读包元数据，快，适合每次跑套件；
    check_stack(deep=True)   真正 import + 跑一次 NMS 冒烟，适合演示前深检。
"""

from __future__ import annotations

import importlib.metadata as md
from typing import Any

# packaging 是**可选**依赖：有则用精确 PEP 440 比较；缺失时走下面的标准库回退。
# 这一点很重要 —— README 承诺「离线演示零第三方依赖」，顶层硬导入会让
# 00_check_env 在干净环境直接 ModuleNotFoundError（实测 python -S 复现）。
try:  # pragma: no cover - 取决于环境
    from packaging.requirements import Requirement
    from packaging.version import Version
except Exception:  # pragma: no cover
    Requirement = None  # type: ignore[assignment,misc]
    Version = None  # type: ignore[assignment,misc]

# 需要互相校验版本的两组依赖（前者声明约束，后者是被约束方）
PAIRS: tuple[tuple[str, str], ...] = (
    ("mediapipe", "protobuf"),
    ("torchvision", "torch"),
)

REAL_MODE_PACKAGES: tuple[str, ...] = ("mediapipe", "torch", "torchvision")


def _installed(pkg: str) -> str | None:
    try:
        return md.version(pkg)
    except md.PackageNotFoundError:
        return None


def _requires(pkg: str, dep: str) -> str | None:
    """从包元数据里取出 pkg 对 dep 的版本约束（如 '>=4.25.3,<5'）。"""
    for raw in md.requires(pkg) or []:
        try:
            req = Requirement(raw)
        except Exception:
            continue
        if req.name.lower() == dep.lower():
            return str(req.specifier)
    return None


def _version_tuple(value: str) -> tuple[int, ...]:
    """把 '2.6.0+cu124' / '4.25.3' 转成可比较的数字元组（回退用）。"""
    base = value.split("+", 1)[0]
    parts: list[int] = []
    for piece in base.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def _fallback_satisfies(version: str, spec: str) -> bool:
    """无 packaging 时的简化约束判断，覆盖 >= <= == != > < 的逗号组合。"""
    current = _version_tuple(version)
    for clause in spec.split(","):
        clause = clause.strip()
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if not clause.startswith(op):
                continue
            target = _version_tuple(clause[len(op) :])
            cmp = (current > target) - (current < target)
            ok = {
                ">=": cmp >= 0,
                "<=": cmp <= 0,
                "==": cmp == 0,
                "!=": cmp != 0,
                ">": cmp > 0,
                "<": cmp < 0,
            }[op]
            if not ok:
                return False
            break
    return True


def _satisfies(version: str | None, spec: str | None) -> bool | None:
    if not version or not spec:
        return None
    if Requirement is not None and Version is not None:
        try:
            return Version(version) in Requirement(f"x{spec}").specifier
        except Exception:
            pass
    try:
        return _fallback_satisfies(version, spec)
    except Exception:
        return None


def _pair(pkg: str, dep: str) -> dict[str, Any]:
    pkg_ver = _installed(pkg)
    dep_ver = _installed(dep)
    spec = _requires(pkg, dep) if pkg_ver else None
    ok = _satisfies(dep_ver, spec) if (pkg_ver and spec) else None
    row: dict[str, Any] = {
        "installed": pkg_ver,
        "dependency": dep,
        "dependency_installed": dep_ver,
        "requires": spec,
        "ok": ok,
    }
    if ok is False:
        row["hint"] = f"{pkg} {pkg_ver} 要求 {dep}{spec}，当前是 {dep} {dep_ver}"
    return row


def _deep_checks() -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}

    try:
        import mediapipe as mp

        out["mediapipe_import"] = {"ok": True, "version": getattr(mp, "__version__", None)}
    except Exception as exc:
        out["mediapipe_import"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        import torch
        from torchvision.ops import nms

        boxes = torch.tensor([[0.0, 0.0, 1.0, 1.0], [0.1, 0.1, 1.1, 1.1]])
        scores = torch.tensor([0.9, 0.8])
        keep = nms(boxes, scores, 0.5)
        out["torchvision_nms"] = {"ok": True, "torch": torch.__version__, "kept": keep.tolist()}
    except Exception as exc:
        out["torchvision_nms"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        from transformers import AutoProcessor  # noqa: F401

        out["auto_processor"] = {"ok": True}
    except Exception as exc:
        out["auto_processor"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    try:
        import cv2

        out["opencv"] = {"ok": True, "version": cv2.__version__}
    except Exception as exc:
        out["opencv"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    return out


def check_stack(deep: bool = False) -> dict[str, Any]:
    """检查真实多模态依赖组合。deep=True 会真正 import 并跑一次 NMS。"""
    checks: dict[str, Any] = {}
    problems: list[str] = []

    for pkg, dep in PAIRS:
        row = _pair(pkg, dep)
        checks[f"{pkg}__{dep}"] = row
        if row.get("ok") is False:
            problems.append(str(row.get("hint") or f"{pkg} 与 {dep} 版本不匹配"))

    if deep:
        for name, row in _deep_checks().items():
            checks[name] = row
            if row.get("ok") is False:
                problems.append(f"{name}: {row.get('error')}")

    installed = {pkg: _installed(pkg) for pkg in REAL_MODE_PACKAGES}
    ready = all(installed.values()) and not problems
    if deep:
        ready = ready and all(
            checks[key].get("ok") is True
            for key in ("mediapipe_import", "torchvision_nms", "auto_processor")
            if key in checks
        )

    return {
        "deep": deep,
        "ok": not problems,
        "ready_for_real_mode": bool(ready),
        "installed": installed,
        "checks": checks,
        "problems": problems,
        "hint": None
        if not problems
        else "按 requirements.txt 的「真实多模态」注释重装依赖（protobuf<5、torchvision 与 torch 配套）",
    }
