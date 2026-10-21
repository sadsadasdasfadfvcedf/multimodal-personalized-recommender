"""
src/contracts.py
================

统一事件与请求/响应 Schema（阶段1 核心交付）。

包含四类数据结构：
    InteractionEvent      —— 多模态交互事件（语音/视觉/手势/文本/反馈）
    RecommendationRequest —— 结构化推荐请求（意图 + 槽位 + 约束）
    RecommendationItem    —— 推荐结果条目（含来源、分数、证据）
    EvidencePack          —— 商品证据包（阶段4 解释的事实来源）

所有结构都满足验收要求：
    1. 公共信封字段包含 trace_id、user_id、timestamp、source；
    2. 提供 to_dict / from_dict，from_dict 对非法输入返回可解释错误；
    3. 不依赖 pydantic，保证离线可运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import ErrorCode, Result, fail, ok
from .utils import is_iso8601, is_non_empty_str, is_number

SCHEMA_VERSION = "1.0.0"

# 事件来源枚举（source 字段的合法取值）
VALID_SOURCES = ("voice", "vision", "gesture", "text", "feedback", "browse", "system")
# browse：用户「继续浏览」类弱反馈（打开商品详情/点击卡片），阶段5 要求纳入会话偏好
# 模态枚举
VALID_MODALITIES = ("asr", "visual", "multimodal", "gesture", "text", "feedback", "none")
# 意图枚举（与 config.INTENT_CONFIG["intents"] 对齐）
VALID_INTENTS = ("discover", "similar", "filter", "compare", "feedback", "explain")
VALID_POLARITIES = ("positive", "negative")


# ============================================================
# 校验小工具
# ============================================================
class _Validator:
    """聚合式校验：一次收集所有字段问题，返回首个决定性错误 + 全部明细。"""

    def __init__(self, payload: Any, schema_name: str):
        self.payload = payload
        self.schema_name = schema_name
        self.problems: list[dict[str, Any]] = []

    def require_object(self) -> bool:
        if not isinstance(self.payload, dict):
            self.problems.append(
                {"code": ErrorCode.TYPE_MISMATCH.value, "field": "$", "message": "顶层必须是 JSON 对象"}
            )
            return False
        return True

    def envelope(self) -> None:
        """校验公共信封字段 trace_id / user_id / timestamp / source。"""
        for name in ("trace_id", "user_id", "timestamp", "source"):
            if name not in self.payload:
                self.problems.append(
                    {"code": ErrorCode.MISSING_FIELD.value, "field": name, "message": f"缺少必填字段 {name}"}
                )
            elif not is_non_empty_str(self.payload.get(name)):
                self.problems.append(
                    {"code": ErrorCode.TYPE_MISMATCH.value, "field": name, "message": f"{name} 必须是非空字符串"}
                )
        ts = self.payload.get("timestamp")
        if is_non_empty_str(ts) and not is_iso8601(ts):
            self.problems.append(
                {
                    "code": ErrorCode.INVALID_TIMESTAMP.value,
                    "field": "timestamp",
                    "message": "timestamp 必须是 ISO8601，例如 2026-01-01T00:00:00+00:00",
                }
            )
        src = self.payload.get("source")
        if is_non_empty_str(src):
            base = src.split(":")[0]
            if base not in VALID_SOURCES:
                self.problems.append(
                    {
                        "code": ErrorCode.INVALID_SOURCE.value,
                        "field": "source",
                        "message": f"source 必须是 {VALID_SOURCES} 之一，或 '<source>:<detail>' 形式",
                    }
                )

    def require(self, name: str, *, types: tuple = (str,), allow_none: bool = False) -> None:
        if name not in self.payload:
            self.problems.append(
                {"code": ErrorCode.MISSING_FIELD.value, "field": name, "message": f"缺少必填字段 {name}"}
            )
            return
        value = self.payload.get(name)
        if value is None and allow_none:
            return
        if types and not isinstance(value, types):
            self.problems.append(
                {
                    "code": ErrorCode.TYPE_MISMATCH.value,
                    "field": name,
                    "message": f"{name} 类型应为 {[t.__name__ for t in types]}",
                }
            )

    def optional(self, name: str, *, types: tuple, default: Any = None) -> None:
        if name not in self.payload or self.payload.get(name) is None:
            return
        if not isinstance(self.payload[name], types):
            self.problems.append(
                {
                    "code": ErrorCode.TYPE_MISMATCH.value,
                    "field": name,
                    "message": f"{name} 类型应为 {[t.__name__ for t in types]}",
                }
            )

    def enum(self, name: str, allowed: tuple) -> None:
        value = self.payload.get(name)
        if value is None:
            return
        if value not in allowed:
            self.problems.append(
                {
                    "code": ErrorCode.INVALID_INPUT.value,
                    "field": name,
                    "message": f"{name}={value!r} 非法，应为 {allowed} 之一",
                }
            )

    def fail_if_any(self) -> Result | None:
        if not self.problems:
            return None
        head = self.problems[0]
        return fail(
            head["code"],
            head["message"],
            field=head["field"],
            problems=self.problems,
            schema=self.schema_name,
        )


def _result_of(schema_name: str, payload: Any, builder) -> Result:
    v = _Validator(payload, schema_name)
    if not v.require_object():
        # require_object() 返回 False 时必定登记了 problem，故 fail_if_any() 不会是 None；
        # 显式断言把这个不变量写出来，同时让类型收窄。
        failure = v.fail_if_any()
        assert failure is not None, "校验失败但未登记问题"
        return failure
    problem = builder(v)
    if problem is not None:
        return problem
    failure = v.fail_if_any()
    return failure if failure is not None else ok()


# ============================================================
# 1. InteractionEvent
# ============================================================
@dataclass
class VisualTag:
    """视觉标签：tag + 置信度 + 来源（阶段4 要求低置信度降权/忽略）。"""

    tag: str
    confidence: float = 0.0
    source: str = "vl"
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"tag": self.tag, "confidence": round(float(self.confidence), 4), "source": self.source, "raw": self.raw}

    @classmethod
    def from_dict(cls, payload: dict) -> VisualTag:
        return cls(
            tag=str(payload.get("tag", "")).strip(),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            source=str(payload.get("source", "vl")),
            raw=payload.get("raw", {}) or {},
        )


@dataclass
class InteractionEvent:
    """多模态交互事件：系统统一入口。"""

    trace_id: str
    user_id: str
    timestamp: str
    source: str
    modality: str = "none"
    text: str | None = None
    visual_tags: list[VisualTag] = field(default_factory=list)
    gesture: dict | None = None
    session_id: str | None = None
    schema_version: str = SCHEMA_VERSION
    raw: dict = field(default_factory=dict)
    # 运行时附加（不参与契约校验）
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "modality": self.modality,
            "text": self.text,
            "visual_tags": [t.to_dict() for t in self.visual_tags],
            "gesture": self.gesture,
            "session_id": self.session_id,
            "raw": self.raw,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Result:
        def build(v: _Validator) -> Result | None:
            v.envelope()
            v.optional("modality", types=(str,))
            v.optional("text", types=(str,))
            v.optional("visual_tags", types=(list,))
            v.optional("gesture", types=(dict,))
            v.optional("session_id", types=(str,))
            v.optional("raw", types=(dict,))
            v.enum("modality", VALID_MODALITIES)
            return None

        validated = _result_of("InteractionEvent", payload, build)
        if not validated.ok:
            return validated
        tags: list[VisualTag] = []
        for item in payload.get("visual_tags") or []:
            if isinstance(item, dict):
                tags.append(VisualTag.from_dict(item))
            elif isinstance(item, str):
                tags.append(VisualTag(tag=item, confidence=1.0, source="vl"))
        event = cls(
            trace_id=payload["trace_id"],
            user_id=payload["user_id"],
            timestamp=payload["timestamp"],
            source=payload["source"],
            modality=payload.get("modality", "none"),
            text=payload.get("text"),
            visual_tags=tags,
            gesture=payload.get("gesture"),
            session_id=payload.get("session_id"),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
            raw=payload.get("raw", {}) or {},
        )
        if event.schema_version != SCHEMA_VERSION:
            return fail(
                ErrorCode.SCHEMA_VERSION_MISMATCH,
                f"schema_version={event.schema_version} 与当前 {SCHEMA_VERSION} 不兼容",
                field="schema_version",
            )
        return ok(event)


# ============================================================
# 2. RecommendationRequest
# ============================================================
@dataclass
class Constraint:
    """约束：kind=hard 时先过滤，不允许被排序分数覆盖。"""

    field: str
    op: str
    value: Any
    kind: str = "hard"
    source: str = "intent"

    def to_dict(self) -> dict:
        return {"field": self.field, "op": self.op, "value": self.value, "kind": self.kind, "source": self.source}

    @classmethod
    def from_dict(cls, payload: dict) -> Constraint:
        return cls(
            field=str(payload.get("field", "")),
            op=str(payload.get("op", "eq")),
            value=payload.get("value"),
            kind=str(payload.get("kind", "hard")),
            source=str(payload.get("source", "intent")),
        )


@dataclass
class Slot:
    """槽位：记录证据来源与置信度（阶段2 要求）。"""

    name: str
    value: Any
    confidence: float = 0.0
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: dict) -> Slot:
        return cls(
            name=str(payload.get("name", "")),
            value=payload.get("value"),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            evidence=list(payload.get("evidence", []) or []),
        )


@dataclass
class RecommendationRequest:
    """结构化推荐请求，由意图理解层产出，交给推荐适配器。"""

    trace_id: str
    user_id: str
    timestamp: str
    source: str
    intent: str = "discover"
    slots: dict[str, Slot] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    top_k: int = 8
    session_id: str | None = None
    exclude_item_ids: list[str] = field(default_factory=list)
    mode: str = "offline"
    schema_version: str = SCHEMA_VERSION
    meta: dict = field(default_factory=dict)

    # ---------- 便捷访问 ----------
    def slot_value(self, name: str, default: Any = None) -> Any:
        s = self.slots.get(name)
        return default if s is None else s.value

    def hard_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if c.kind == "hard"]

    def soft_constraints(self) -> list[Constraint]:
        return [c for c in self.constraints if c.kind == "soft"]

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "intent": self.intent,
            "slots": {k: v.to_dict() for k, v in self.slots.items()},
            "constraints": [c.to_dict() for c in self.constraints],
            "top_k": self.top_k,
            "session_id": self.session_id,
            "exclude_item_ids": list(self.exclude_item_ids),
            "mode": self.mode,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Result:
        def build(v: _Validator) -> Result | None:
            v.envelope()
            v.optional("intent", types=(str,))
            v.optional("slots", types=(dict,))
            v.optional("constraints", types=(list,))
            v.optional("top_k", types=(int,))
            v.optional("session_id", types=(str,))
            v.optional("exclude_item_ids", types=(list,))
            v.optional("mode", types=(str,))
            v.enum("intent", VALID_INTENTS)
            top_k = payload.get("top_k")
            if top_k is not None and (not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1):
                v.problems.append(
                    {"code": ErrorCode.INVALID_INPUT.value, "field": "top_k", "message": "top_k 必须是 >=1 的整数"}
                )
            excl = payload.get("exclude_item_ids")
            if excl is not None and not all(isinstance(x, str) for x in excl):
                v.problems.append(
                    {
                        "code": ErrorCode.TYPE_MISMATCH.value,
                        "field": "exclude_item_ids",
                        "message": "exclude_item_ids 必须是字符串数组",
                    }
                )
            return None

        validated = _result_of("RecommendationRequest", payload, build)
        if not validated.ok:
            return validated
        slots = {k: Slot.from_dict(v) for k, v in (payload.get("slots") or {}).items() if isinstance(v, dict)}
        constraints = [Constraint.from_dict(c) for c in (payload.get("constraints") or []) if isinstance(c, dict)]
        req = cls(
            trace_id=payload["trace_id"],
            user_id=payload["user_id"],
            timestamp=payload["timestamp"],
            source=payload["source"],
            intent=payload.get("intent", "discover"),
            slots=slots,
            constraints=constraints,
            top_k=int(payload.get("top_k", 8)),
            session_id=payload.get("session_id"),
            exclude_item_ids=list(payload.get("exclude_item_ids") or []),
            mode=payload.get("mode", "offline"),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
        )
        return ok(req)


# ============================================================
# 3. RecommendationItem
# ============================================================
@dataclass
class RecommendationItem:
    """推荐结果条目：来源可追溯（sources）、分数、证据、可读理由。"""

    trace_id: str
    user_id: str
    timestamp: str
    source: str
    item_id: str
    title: str = ""
    category: str = ""
    price: float | None = None
    currency: str = ""
    score: float = 0.0
    rank: int = 0
    sources: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    reason: str = ""
    schema_version: str = SCHEMA_VERSION
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "timestamp": self.timestamp,
            "source": self.source,
            "item_id": self.item_id,
            "title": self.title,
            "category": self.category,
            "price": self.price,
            "currency": self.currency,
            "score": round(float(self.score), 6),
            "rank": self.rank,
            "sources": list(self.sources),
            "evidence": self.evidence,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> Result:
        def build(v: _Validator) -> Result | None:
            v.envelope()
            v.require("item_id", types=(str,))
            v.optional("title", types=(str,))
            v.optional("category", types=(str,))
            v.optional("price", types=(int, float))
            v.optional("currency", types=(str,))
            v.optional("score", types=(int, float))
            v.optional("rank", types=(int,))
            v.optional("sources", types=(list,))
            v.optional("evidence", types=(dict,))
            v.optional("reason", types=(str,))
            return None

        validated = _result_of("RecommendationItem", payload, build)
        if not validated.ok:
            return validated
        item = cls(
            trace_id=payload["trace_id"],
            user_id=payload["user_id"],
            timestamp=payload["timestamp"],
            source=payload["source"],
            item_id=str(payload["item_id"]),
            title=payload.get("title", ""),
            category=payload.get("category", ""),
            price=float(payload["price"]) if is_number(payload.get("price")) else None,
            currency=payload.get("currency", ""),
            score=float(payload.get("score", 0.0) or 0.0),
            rank=int(payload.get("rank", 0) or 0),
            sources=list(payload.get("sources") or []),
            evidence=payload.get("evidence", {}) or {},
            reason=payload.get("reason", ""),
            schema_version=payload.get("schema_version", SCHEMA_VERSION),
        )
        return ok(item)


# ============================================================
# 4. EvidencePack（阶段4）
# ============================================================
@dataclass
class EvidencePack:
    """商品证据包：解释器只能从这里取事实。"""

    trace_id: str
    item_id: str
    product_facts: dict = field(default_factory=dict)  # 来自商品目录/价格表
    candidate_sources: list[str] = field(default_factory=list)  # 候选来源
    matched_constraints: list[dict] = field(default_factory=list)  # 命中的约束
    visual_tags: list[VisualTag] = field(default_factory=list)  # 视觉标签（含置信度）
    evidence_snippets: list[dict] = field(default_factory=list)  # RAG 检索片段
    session_signals: list[dict] = field(default_factory=list)  # 会话偏好信号
    consistency: dict = field(default_factory=dict)  # 一致性核验结果
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "item_id": self.item_id,
            "product_facts": self.product_facts,
            "candidate_sources": list(self.candidate_sources),
            "matched_constraints": self.matched_constraints,
            "visual_tags": [t.to_dict() for t in self.visual_tags],
            "evidence_snippets": self.evidence_snippets,
            "session_signals": self.session_signals,
            "consistency": self.consistency,
        }

    def evidence_count(self) -> int:
        """可核验证据条数（约束/事实/视觉/片段分别计数）。"""
        n = 0
        n += len(self.matched_constraints)
        n += len(self.evidence_snippets)
        n += len([t for t in self.visual_tags if t.confidence > 0])
        n += len(self.session_signals)
        if self.product_facts.get("price") is not None:
            n += 1
        if self.product_facts.get("category"):
            n += 1
        return n


# ============================================================
# 事件构造辅助
# ============================================================
def make_event(
    *,
    user_id: str,
    source: str,
    modality: str = "none",
    text: str | None = None,
    visual_tags: list[dict] | None = None,
    gesture: dict | None = None,
    session_id: str | None = None,
    timestamp: str | None = None,
    trace_hint: str | None = None,
) -> InteractionEvent:
    """按需构造合法事件（脚本/测试用），保证信封字段齐全。"""
    from .utils import new_trace_id, now_iso

    tags = [VisualTag.from_dict(t) for t in (visual_tags or [])]
    return InteractionEvent(
        trace_id=new_trace_id(trace_hint),
        user_id=str(user_id),
        timestamp=timestamp or now_iso(),
        source=source,
        modality=modality,
        text=text,
        visual_tags=tags,
        gesture=gesture,
        session_id=session_id,
    )
