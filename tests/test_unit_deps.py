"""src/deps.py —— 依赖兼容性检查。

回归锁：mediapipe 0.10.14 要求 protobuf<5；torchvision 0.21.0 要求 torch==2.6.0。
这两条约束必须读自包元数据，不能写死在代码里（否则升级依赖后检查会失真）。
"""

from __future__ import annotations

from src.deps import _fallback_satisfies, _satisfies, check_stack


def test_fast_check_shape():
    r = check_stack(deep=False)
    assert set(r) == {"deep", "ok", "ready_for_real_mode", "installed", "checks", "problems", "hint"}
    assert r["deep"] is False
    assert set(r["installed"]) == {"mediapipe", "torch", "torchvision"}


def test_pair_checks_come_from_package_metadata():
    r = check_stack(deep=False)
    mp = r["checks"]["mediapipe__protobuf"]
    assert mp["dependency"] == "protobuf"
    assert mp["requires"], "约束应来自 mediapipe 的 Requires-Dist"
    tv = r["checks"]["torchvision__torch"]
    assert tv["dependency"] == "torch"
    assert tv["requires"], "约束应来自 torchvision 的 Requires-Dist"


def test_satisfies_handles_local_version_suffix():
    """torch 常见 2.6.0+cu124 这种本地版本号，必须能匹配 ==2.6.0。"""
    assert _satisfies("2.6.0+cu124", "==2.6.0") is True
    assert _satisfies("0.23.0", "==2.6.0") is False
    assert _satisfies("4.25.3", ">=4.25.3,<5") is True
    assert _satisfies("7.36.2", ">=4.25.3,<5") is False
    assert _satisfies(None, ">=1") is None


def test_fallback_satisfies_without_packaging():
    """零依赖环境下 packaging 可能不存在；回退实现必须覆盖常见约束。"""
    assert _fallback_satisfies("2.6.0+cu124", "==2.6.0") is True
    assert _fallback_satisfies("0.23.0", "==2.6.0") is False
    assert _fallback_satisfies("4.25.3", ">=4.25.3,<5") is True
    assert _fallback_satisfies("7.36.2", ">=4.25.3,<5") is False
