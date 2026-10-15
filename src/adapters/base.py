"""
src/adapters/base.py
====================

适配器公共设施：降级记录、调用日志、超时包装。

阶段1 要求「记录输入、关键中间状态、输出与耗时」，并「保留离线降级」。
所有降级事件都以结构化形式记录，最终写入 outputs/ 证据文件。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ..errors import Result, fail


class AdapterBase:
    """所有适配器的基类。"""

    name: str = "adapter"

    def __init__(self, requested_mode: str = "offline") -> None:
        self.requested_mode = requested_mode
        self.active_mode: str = "unresolved"
        self.degradations: list[dict[str, Any]] = []
        self.call_log: list[dict[str, Any]] = []

    # ---------------- 降级 ----------------
    def record_degradation(self, *, level: str, reason: str, from_mode: str, to_mode: str, **extra: Any) -> None:
        self.degradations.append(
            {
                "adapter": self.name,
                "level": level,
                "reason": reason,
                "from": from_mode,
                "to": to_mode,
                **extra,
            }
        )

    def degradation_report(self) -> dict[str, Any]:
        return {
            "adapter": self.name,
            "requested_mode": self.requested_mode,
            "active_mode": self.active_mode,
            "degraded": bool(self.degradations),
            "events": list(self.degradations),
        }

    # ---------------- 调用记录 ----------------
    def log_call(
        self,
        method: str,
        *,
        ok_flag: bool,
        latency_ms: float,
        mode: str,
        error: dict | None = None,
        detail: dict | None = None,
    ) -> None:
        self.call_log.append(
            {
                "adapter": self.name,
                "method": method,
                "mode": mode,
                "ok": ok_flag,
                "latency_ms": round(latency_ms, 3),
                "error": error,
                "detail": detail or {},
            }
        )

    def call_report(self) -> dict[str, Any]:
        total = len(self.call_log)
        failed = sum(1 for c in self.call_log if not c["ok"])
        latencies = [c["latency_ms"] for c in self.call_log]
        return {
            "adapter": self.name,
            "calls": total,
            "failed": failed,
            "success_rate": round((total - failed) / total, 4) if total else None,
            "latency_ms": {
                "avg": round(sum(latencies) / len(latencies), 3) if latencies else None,
                "max": round(max(latencies), 3) if latencies else None,
            },
            "calls_detail": list(self.call_log),
        }

    # ---------------- 超时包装 ----------------
    def guard(self, method: str, fn: Callable[[], Result], *, timeout_s: float | None = None) -> Result:
        """执行一次适配器调用，自动记录耗时与失败；超时由具体实现负责（HTTP 层）。"""
        start = time.perf_counter()
        try:
            result = fn()
        except Exception as exc:  # 统一转成结构化错误，绝不返回空字典
            from ..errors import as_error_dict

            err = as_error_dict(exc)
            self.log_call(
                method, ok_flag=False, latency_ms=(time.perf_counter() - start) * 1000, mode=self.active_mode, error=err
            )
            return fail(err["code"], err["message"], error_context=err.get("context", {}))
        latency = (time.perf_counter() - start) * 1000
        self.log_call(
            method,
            ok_flag=result.ok,
            latency_ms=latency,
            mode=self.active_mode,
            error=result.error.to_dict() if result.error else None,
        )
        return result

    def health(self) -> Result:
        raise NotImplementedError
