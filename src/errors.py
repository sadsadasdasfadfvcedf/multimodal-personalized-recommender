"""
src/errors.py
=============

统一错误码与「可解释错误」信封。

阶段1 验收项：非法输入必须返回明确错误，禁止「异常被捕获后返回空字典」。
因此全项目统一使用 :class:`Result` 作为返回值：

    {"ok": True,  "error": None, "data": {...}}
    {"ok": False, "error": {"code": "E_INVALID_INPUT", "message": "...", "field": "budget"}, "data": None}
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """全项目错误码表。字符串枚举，便于直接落盘为 JSON。"""

    # 输入 / 契约类
    INVALID_INPUT = "E_INVALID_INPUT"
    MISSING_FIELD = "E_MISSING_FIELD"
    UNKNOWN_FIELD = "E_UNKNOWN_FIELD"
    TYPE_MISMATCH = "E_TYPE_MISMATCH"
    INVALID_TIMESTAMP = "E_INVALID_TIMESTAMP"
    INVALID_SOURCE = "E_INVALID_SOURCE"
    SCHEMA_VERSION_MISMATCH = "E_SCHEMA_VERSION_MISMATCH"

    # 意图 / 会话类
    UNKNOWN_INTENT = "E_UNKNOWN_INTENT"
    MISSING_SLOT = "E_MISSING_SLOT"
    CONSTRAINT_CONFLICT = "E_CONSTRAINT_CONFLICT"
    SESSION_NOT_FOUND = "E_SESSION_NOT_FOUND"
    NOTHING_TO_UNDO = "E_NOTHING_TO_UNDO"

    # 多模态类
    EMPTY_TRANSCRIPT = "E_EMPTY_TRANSCRIPT"
    LOW_CONFIDENCE_MODALITY = "E_LOW_CONFIDENCE_MODALITY"
    MODALITY_UNAVAILABLE = "E_MODALITY_UNAVAILABLE"

    # 推荐链路类
    UPSTREAM_UNAVAILABLE = "E_UPSTREAM_UNAVAILABLE"
    TIMEOUT = "E_TIMEOUT"
    NO_RESULT = "E_NO_RESULT"
    HARD_CONSTRAINT_EMPTY = "E_HARD_CONSTRAINT_EMPTY"
    NOT_PURCHASABLE = "E_NOT_PURCHASABLE"

    # 证据 / 解释类
    NO_EVIDENCE = "E_NO_EVIDENCE"
    EVIDENCE_INCONSISTENT = "E_EVIDENCE_INCONSISTENT"
    ITEM_NOT_FOUND = "E_ITEM_NOT_FOUND"

    # 反馈类
    INVALID_POLARITY = "E_INVALID_POLARITY"
    FEEDBACK_CONFLICT = "E_FEEDBACK_CONFLICT"

    # 基础设施类
    INTERNAL_ERROR = "E_INTERNAL_ERROR"
    DEGRADED = "E_DEGRADED"


# 错误码 → 人类可读的默认说明（中文）
DEFAULT_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.INVALID_INPUT: "输入不合法",
    ErrorCode.MISSING_FIELD: "缺少必填字段",
    ErrorCode.UNKNOWN_FIELD: "出现未定义字段",
    ErrorCode.TYPE_MISMATCH: "字段类型不匹配",
    ErrorCode.INVALID_TIMESTAMP: "时间戳格式非法（需 ISO8601）",
    ErrorCode.INVALID_SOURCE: "来源取值非法",
    ErrorCode.SCHEMA_VERSION_MISMATCH: "Schema 版本不兼容",
    ErrorCode.UNKNOWN_INTENT: "无法识别意图",
    ErrorCode.MISSING_SLOT: "必选槽位缺失",
    ErrorCode.CONSTRAINT_CONFLICT: "约束之间存在冲突",
    ErrorCode.SESSION_NOT_FOUND: "会话不存在或已结束",
    ErrorCode.NOTHING_TO_UNDO: "没有可撤销的操作",
    ErrorCode.EMPTY_TRANSCRIPT: "语音识别结果为空或过短",
    ErrorCode.LOW_CONFIDENCE_MODALITY: "模态置信度过低",
    ErrorCode.MODALITY_UNAVAILABLE: "该模态当前不可用",
    ErrorCode.UPSTREAM_UNAVAILABLE: "上游推荐服务不可用",
    ErrorCode.TIMEOUT: "上游调用超时",
    ErrorCode.NO_RESULT: "没有满足条件的推荐结果",
    ErrorCode.HARD_CONSTRAINT_EMPTY: "硬约束过滤后没有任何候选",
    ErrorCode.NOT_PURCHASABLE: "该商品不可购买",
    ErrorCode.NO_EVIDENCE: "没有任何可核验证据",
    ErrorCode.EVIDENCE_INCONSISTENT: "证据与商品字段不一致",
    ErrorCode.ITEM_NOT_FOUND: "商品不存在",
    ErrorCode.INVALID_POLARITY: "反馈极性非法",
    ErrorCode.FEEDBACK_CONFLICT: "同一商品的反馈极性冲突",
    ErrorCode.INTERNAL_ERROR: "内部错误",
    ErrorCode.DEGRADED: "已降级运行",
}


@dataclass
class AppError:
    """结构化错误：code + 人类可读 message + 相关字段 + 上下文。"""

    code: ErrorCode | str
    message: str = ""
    field: str | None = None
    context: dict[str, Any] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.code, str) and not isinstance(self.code, ErrorCode):
            try:
                self.code = ErrorCode(self.code)
            except ValueError:
                self.code = ErrorCode.INTERNAL_ERROR
        if not self.message:
            self.message = DEFAULT_MESSAGES.get(self.code, "未知错误")  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value if isinstance(self.code, ErrorCode) else str(self.code),
            "message": self.message,
            "field": self.field,
            "context": self.context,
        }

    def __str__(self) -> str:  # pragma: no cover - 便于日志
        parts = [self.to_dict()["code"], self.message]
        if self.field:
            parts.append(f"field={self.field}")
        if self.context:
            parts.append(f"context={self.context}")
        return " | ".join(parts)


class IntegrationError(Exception):
    """内部异常：带错误码，供上层转换为 Result。"""

    def __init__(self, error: AppError):
        super().__init__(str(error))
        self.error = error


@dataclass
class Result:
    """全项目统一返回信封。"""

    ok: bool
    data: Any = None
    error: AppError | None = None
    meta: dict[str, Any] = dataclasses.field(default_factory=dict)

    # ---------- 构造 ----------
    @classmethod
    def success(cls, data: Any = None, **meta: Any) -> "Result":
        return cls(ok=True, data=data, error=None, meta=meta)

    @classmethod
    def failure(
        cls,
        code: ErrorCode | str,
        message: str = "",
        field: str | None = None,
        **context: Any,
    ) -> "Result":
        return cls(ok=False, data=None, error=AppError(code, message, field, context))

    # ---------- 序列化 ----------
    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data": self.data,
            "error": self.error.to_dict() if self.error else None,
            "meta": self.meta,
        }

    def raise_if_error(self) -> "Result":
        """在必须失败的链路上把 Result 转成异常。"""
        if not self.ok and self.error:
            raise IntegrationError(self.error)
        return self

    def __bool__(self) -> bool:  # 允许 `if result:`
        return self.ok


def ok(data: Any = None, **meta: Any) -> Result:
    return Result.success(data, **meta)


def fail(code: ErrorCode | str, message: str = "", field: str | None = None, **context: Any) -> Result:
    return Result.failure(code, message, field, **context)


def as_error_dict(exc: BaseException) -> dict[str, Any]:
    """把任意异常转成可落盘的结构化错误（避免只打印 traceback）。"""
    if isinstance(exc, IntegrationError):
        return exc.error.to_dict()
    return AppError(
        ErrorCode.INTERNAL_ERROR,
        f"{type(exc).__name__}: {exc}",
    ).to_dict()

