"""
src/session.py
==============

会话上下文（阶段2）。

能力：
    1. 合并多轮约束（后一轮覆盖前一轮同名槽位，并保留证据链）
    2. 撤销（undo）到上一步状态
    3. 冲突提示（记录待澄清问题）
    4. 会话结束清理（end() 后拒绝继续写入）
    5. 点赞/倒赞 → 正/负反馈（互斥），且**只作用于本会话**，不污染其他会话

设计要点：反馈偏好存放在 SessionContext 内，按 session_id 隔离；
新会话从零开始 —— 直接满足「负反馈不会永久污染其他会话」。
"""
from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from config import settings
from .contracts import Constraint, Slot
from .errors import ErrorCode, Result, fail, ok
from .utils import now_iso, stable_key

FB = settings.FEEDBACK_CONFIG


@dataclass
class Turn:
    """一轮交互记录。"""

    index: int
    trace_id: str
    user_id: str
    timestamp: str
    source: str
    text: str | None = None
    intent: str | None = None
    slots: dict[str, Any] = field(default_factory=dict)
    constraints: list[dict] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    feedback: dict | None = None
    status: str = "ok"          # ok | clarification | conflict | error
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "index": self.index, "trace_id": self.trace_id, "user_id": self.user_id,
            "timestamp": self.timestamp, "source": self.source, "text": self.text,
            "intent": self.intent, "slots": self.slots, "constraints": self.constraints,
            "item_ids": list(self.item_ids), "feedback": self.feedback,
            "status": self.status, "note": self.note,
        }


class SessionContext:
    """单个会话的全部状态。"""

    def __init__(self, session_id: str, user_id: str) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.created_at = now_iso()
        self.updated_at = self.created_at
        self.ended = False
        self.turns: list[Turn] = []
        self.slot_state: dict[str, Slot] = {}
        self.constraints: list[Constraint] = []
        self.focus_item_id: str | None = None
        self.last_item_ids: list[str] = []
        self.seen_item_ids: list[str] = []
        self.likes: Counter[str] = Counter()
        self.dislikes: Counter[str] = Counter()
        # 「继续浏览」弱反馈：商品与类目的浏览计数（阶段5 任务目标要求）
        self.viewed_items: Counter[str] = Counter()
        self.viewed_categories: Counter[str] = Counter()
        self.liked_categories: Counter[str] = Counter()
        self.disliked_categories: Counter[str] = Counter()
        self.pending_clarification: str | None = None
        self._snapshots: list[dict] = []

    # ---------------- 快照 / 撤销 ----------------
    def _snapshot(self) -> dict:
        return {
            "slot_state": copy.deepcopy(self.slot_state),
            "constraints": copy.deepcopy(self.constraints),
            "focus_item_id": self.focus_item_id,
            "last_item_ids": list(self.last_item_ids),
            "seen_item_ids": list(self.seen_item_ids),
            "likes": copy.deepcopy(self.likes),
            "dislikes": copy.deepcopy(self.dislikes),
            "viewed_items": copy.deepcopy(self.viewed_items),
            "viewed_categories": copy.deepcopy(self.viewed_categories),
            "liked_categories": copy.deepcopy(self.liked_categories),
            "disliked_categories": copy.deepcopy(self.disliked_categories),
            "pending_clarification": self.pending_clarification,
        }

    def _restore(self, snap: dict) -> None:
        self.slot_state = snap["slot_state"]
        self.constraints = snap["constraints"]
        self.focus_item_id = snap["focus_item_id"]
        self.last_item_ids = snap["last_item_ids"]
        self.seen_item_ids = snap["seen_item_ids"]
        self.likes = snap["likes"]
        self.dislikes = snap["dislikes"]
        self.viewed_items = snap["viewed_items"]
        self.viewed_categories = snap["viewed_categories"]
        self.liked_categories = snap["liked_categories"]
        self.disliked_categories = snap["disliked_categories"]
        self.pending_clarification = snap["pending_clarification"]

    def undo(self) -> Result:
        """撤销上一轮：回滚槽位/约束/反馈，并弹出该轮记录。"""
        if self.ended:
            return fail(ErrorCode.SESSION_NOT_FOUND, "会话已结束，无法撤销")
        if not self._snapshots:
            return fail(ErrorCode.NOTHING_TO_UNDO, "没有可撤销的操作")
        snap = self._snapshots.pop()
        undone = self.turns.pop() if self.turns else None
        self._restore(snap)
        self.updated_at = now_iso()
        return ok({"undone_turn": undone.to_dict() if undone else None,
                   "remaining_turns": len(self.turns),
                   "slot_state": {k: v.to_dict() for k, v in self.slot_state.items()}})

    # ---------------- 写入 ----------------
    def begin_turn(self) -> None:
        self._snapshots.append(self._snapshot())

    def record_turn(self, turn: Turn) -> None:
        self.turns.append(turn)
        self.updated_at = now_iso()

    def merge_slots(self, slots: dict[str, Slot], constraints: list[Constraint],
                    *, source: str = "turn") -> dict:
        """把本轮槽位合并进会话状态：同名覆盖，并记录变更明细。"""
        changes: list[dict] = []
        for name, slot in slots.items():
            previous = self.slot_state.get(name)
            if previous is None or previous.value != slot.value:
                changes.append({
                    "slot": name,
                    "from": None if previous is None else previous.value,
                    "to": slot.value,
                    "source": source,
                    "confidence": slot.confidence,
                    "evidence": list(slot.evidence),
                })
            self.slot_state[name] = copy.deepcopy(slot)
        # 约束按 field+op 去重合并，新值覆盖旧值
        for constraint in constraints:
            self.constraints = [c for c in self.constraints
                                if not (c.field == constraint.field and c.op == constraint.op)]
            self.constraints.append(copy.deepcopy(constraint))
        self.updated_at = now_iso()
        return {"changes": changes, "slot_count": len(self.slot_state),
                "constraint_count": len(self.constraints)}

    def clear_slot(self, name: str) -> bool:
        if name in self.slot_state:
            del self.slot_state[name]
            self.constraints = [c for c in self.constraints if c.source != f"slot:{name}"]
            return True
        return False

    def set_clarification(self, question: str | None) -> None:
        self.pending_clarification = question

    def add_recommendation(self, item_ids: list[str], *, focus: str | None = None) -> None:
        self.last_item_ids = list(item_ids)
        for item_id in item_ids:
            if item_id not in self.seen_item_ids:
                self.seen_item_ids.append(item_id)
        if focus:
            self.focus_item_id = focus
        self.updated_at = now_iso()

    # ---------------- 反馈 ----------------
    def add_feedback(self, item_id: str, polarity: str, *, category: str | None = None) -> Result:
        """写入反馈。正负互斥：同一商品只会保留最后一次极性。"""
        if self.ended:
            return fail(ErrorCode.SESSION_NOT_FOUND, "会话已结束，无法写入反馈")
        if polarity not in ("positive", "negative"):
            return fail(ErrorCode.INVALID_POLARITY, f"polarity={polarity!r} 非法", field="polarity")
        item_id = str(item_id)
        conflict = False
        if FB["mutual_exclusion"]:
            if polarity == "positive" and item_id in self.dislikes:
                del self.dislikes[item_id]
                conflict = True
            if polarity == "negative" and item_id in self.likes:
                del self.likes[item_id]
                conflict = True
        if polarity == "positive":
            self.likes[item_id] += 1
        else:
            self.dislikes[item_id] += 1
        if category:
            if polarity == "positive":
                self.liked_categories[category] += 1
            else:
                self.disliked_categories[category] += 1
        if item_id not in self.seen_item_ids:
            self.seen_item_ids.append(item_id)
        self.focus_item_id = item_id
        self.updated_at = now_iso()
        return ok({
            "item_id": item_id, "polarity": polarity, "replaced_previous": conflict,
            "likes": dict(self.likes), "dislikes": dict(self.dislikes),
        })

    def add_view(self, item_id: str, *, category: str | None = None) -> Result:
        """记录一次「继续浏览」——弱正反馈，写入会话偏好但不改变正负反馈状态。

        与点赞的区别：浏览只是"看过"，不表达喜好，因此
          · 权重远低于点赞（config: view_item_boost / view_category_boost）
          · 有累计上限，防止反复打开刷分
          · 不影响 likes / dislikes，也不会屏蔽任何商品
        """
        if self.ended:
            return fail(ErrorCode.SESSION_NOT_FOUND, "会话已结束，无法写入浏览记录")
        item_id = str(item_id)
        if not item_id:
            return fail(ErrorCode.MISSING_FIELD, "缺少 item_id", field="item_id")
        self.viewed_items[item_id] += 1
        if category:
            self.viewed_categories[str(category)] += 1
        if item_id not in self.seen_item_ids:
            self.seen_item_ids.append(item_id)
        self.updated_at = now_iso()
        return ok({"item_id": item_id, "category": category,
                   "view_count": self.viewed_items[item_id],
                   "distinct_viewed": len(self.viewed_items)})

    def remove_feedback(self, item_id: str) -> Result:
        item_id = str(item_id)
        removed = False
        for bucket in (self.likes, self.dislikes):
            if item_id in bucket:
                del bucket[item_id]
                removed = True
        return ok({"item_id": item_id, "removed": removed})

    # ---------------- 偏好信号 ----------------
    def preference_signal(self, item_id: str, category: str) -> dict[str, float]:
        """返回某商品的会话偏好分数（正=加分，负=减分）与原因。"""
        cfg = settings.RANKING_CONFIG["session_preference"]
        score = 0.0
        reasons: list[str] = []
        if item_id in self.likes:
            score += cfg["like_item_boost"] * min(self.likes[item_id], 2)
            reasons.append(f"你点过赞的商品(+{cfg['like_item_boost'] * min(self.likes[item_id], 2):.2f})")
        if item_id in self.dislikes:
            score -= cfg["dislike_category_penalty"]
            reasons.append(f"你点过倒赞的商品(-{cfg['dislike_category_penalty']:.2f})")
        if category in self.liked_categories:
            boost = cfg["like_category_boost"] * min(self.liked_categories[category], 3)
            score += boost
            reasons.append(f"本会话偏好类目「{category}」(+{boost:.2f})")
        # 「继续浏览」弱正反馈（阶段5 任务目标）
        if item_id in self.viewed_items:
            boost = cfg.get("view_item_boost", 0.04) * min(self.viewed_items[item_id],
                                                           int(cfg.get("view_max_count", 2)))
            score += boost
            reasons.append(f"你浏览过该商品 {min(self.viewed_items[item_id], int(cfg.get('view_max_count', 2)))} 次(+{boost:.2f})")
        if category in self.viewed_categories:
            boost = cfg.get("view_category_boost", 0.03) * min(self.viewed_categories[category], 3)
            score += boost
            reasons.append(f"你浏览过类目「{category}」(+{boost:.2f})")

        # 类目级倒赞：只有累计次数达到阈值才生效，避免单次倒赞放大成整类降权
        min_count = int(cfg.get("dislike_category_min_count", 2))
        if self.disliked_categories.get(category, 0) >= min_count:
            penalty = cfg["dislike_category_penalty"] * min(self.disliked_categories[category], 2)
            score -= penalty
            reasons.append(f"本会话多次回避类目「{category}」(-{penalty:.2f})")
        if cfg["seen_penalty"] > 0 and item_id in self.seen_item_ids:
            score -= cfg["seen_penalty"]
            reasons.append(f"本会话已出现(-{cfg['seen_penalty']:.2f})")
        score = max(-cfg["max_boost"], min(cfg["max_boost"], score))
        return {"score": round(score, 4), "reasons": reasons}

    def blocked_item_ids(self) -> set[str]:
        """被本会话负反馈明确排除的商品（仅本会话生效）。"""
        return set(self.dislikes)

    # ---------------- 结束 ----------------
    def end(self) -> dict:
        self.ended = True
        summary = {
            "session_id": self.session_id, "user_id": self.user_id,
            "turns": len(self.turns), "likes": dict(self.likes), "dislikes": dict(self.dislikes),
            "ended_at": now_iso(),
        }
        self.slot_state.clear()
        self.constraints.clear()
        self.last_item_ids = []
        self.focus_item_id = None
        self.pending_clarification = None
        self.likes.clear()
        self.dislikes.clear()
        self.viewed_items.clear()
        self.viewed_categories.clear()
        summary["viewed_items"] = 0
        return summary

    # ---------------- 导出 ----------------
    def snapshot(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_id": self.user_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "ended": self.ended,
            "turns": len(self.turns),
            "slot_state": {k: v.to_dict() for k, v in self.slot_state.items()},
            "constraints": [c.to_dict() for c in self.constraints],
            "focus_item_id": self.focus_item_id,
            "last_item_ids": list(self.last_item_ids),
            "seen_item_ids": list(self.seen_item_ids),
            "likes": dict(self.likes),
            "dislikes": dict(self.dislikes),
            "viewed_items": dict(self.viewed_items),
            "viewed_categories": dict(self.viewed_categories),
            "liked_categories": dict(self.liked_categories),
            "disliked_categories": dict(self.disliked_categories),
            "pending_clarification": self.pending_clarification,
        }

    def turn_log(self) -> list[dict]:
        return [t.to_dict() for t in self.turns]


class SessionStore:
    """会话仓库：按 session_id 隔离；不同会话的反馈互不影响。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionContext] = {}

    def get_or_create(self, session_id: str, user_id: str) -> SessionContext:
        session = self._sessions.get(session_id)
        if session is None or session.ended or session.user_id != str(user_id):
            session = SessionContext(session_id, str(user_id))
            self._sessions[session_id] = session
        return session

    def get(self, session_id: str) -> SessionContext | None:
        return self._sessions.get(session_id)

    def end(self, session_id: str) -> dict:
        session = self._sessions.get(session_id)
        if session is None:
            return {"session_id": session_id, "ended": False, "reason": "会话不存在"}
        summary = session.end()
        return summary

    def default_session_id(self, user_id: str) -> str:
        return "sess-" + stable_key(user_id, now_iso()[:10])

    def active_count(self) -> int:
        return sum(1 for s in self._sessions.values() if not s.ended)
