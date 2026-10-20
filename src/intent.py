"""
src/intent.py
=============

多模态意图理解与槽位抽取（阶段2 核心）。

输入：InteractionEvent（ASR 文本 / 视觉标签 / 手势）
输出：RecommendationRequest（六类意图 + 槽位 + 硬/软约束），或明确的澄清请求

六类意图（与 config.INTENT_CONFIG 对齐）:
    discover  发现/随便看看        （可选：类目、场景偏好，均为软约束）
    similar   找相似               （必选：target 参照商品）
    filter    带硬约束筛选         （必选：至少一个硬约束）
    compare   对比                 （必选：targets ≥ 2）
    feedback  反馈                 （必选：polarity）
    explain   求解释               （必选：target）

硬性原则：
    1. 必选槽位缺失时 *不臆测*，返回 E_MISSING_SLOT 与澄清问题；
    2. thumbs_up / thumbs_down 明确映射为正/负反馈；
    3. 约束冲突（如预算上下限颠倒、互斥颜色过多）可检测并返回 E_CONSTRAINT_CONFLICT；
    4. 槽位记录证据来源与置信度。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from config import settings

from .contracts import Constraint, InteractionEvent, RecommendationRequest, Slot
from .errors import ErrorCode, Result, fail, ok
from .utils import contains_any

CFG = settings.INTENT_CONFIG
LEX = settings.SLOT_LEXICON
CONFLICT_RULES = settings.CONFLICT_RULES

_NUM_RE = re.compile(r"(\d+(?:[.,]\d+)?)")

_CN_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "壹": 1,
    "二": 2,
    "两": 2,
    "贰": 2,
    "三": 3,
    "叁": 3,
    "四": 4,
    "肆": 4,
    "五": 5,
    "伍": 5,
    "六": 6,
    "陆": 6,
    "七": 7,
    "柒": 7,
    "八": 8,
    "捌": 8,
    "九": 9,
    "玖": 9,
}
_CN_UNITS = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}
_CN_NUM_RE = re.compile("[" + settings.CN_NUM_CHARS + "]{1,12}")


def _cn_to_int(text: str) -> int | None:
    """中文数字串转整数：三百→300、一百五→150、十五→15、一千二百三十→1230。"""
    if not text:
        return None
    section = number = 0
    last_unit = 0
    for ch in text:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            section += (number or 1) * unit
            last_unit = unit
            number = 0
        else:
            return None
    if number and last_unit >= 10:
        # 口语尾随数字按上一单位的十分之一进位：「一百五」=150、「一千二」=1200
        section += number * (last_unit // 10)
        number = 0
    return (section + number) or None


# ============================================================
# 结果结构
# ============================================================
@dataclass
class IntentResult:
    intent: str
    confidence: float
    intent_scores: dict[str, float] = field(default_factory=dict)
    slots: dict[str, Slot] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)
    missing_slots: list[str] = field(default_factory=list)
    conflicts: list[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent,
            "intent_confidence": round(self.confidence, 4),
            "intent_scores": {k: round(v, 4) for k, v in self.intent_scores.items()},
            "slots": {k: v.to_dict() for k, v in self.slots.items()},
            "constraints": [c.to_dict() for c in self.constraints],
            "missing_slots": list(self.missing_slots),
            "conflicts": list(self.conflicts),
            "evidence": self.evidence,
        }


# ============================================================
# 词表匹配工具
# ============================================================
def _match_categories(text: str, catalog_categories: list[str]) -> list[tuple[str, str]]:
    """返回 [(category, 命中词)]，同时匹配显式类目名与同义词。"""
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for cat in sorted(catalog_categories, key=len, reverse=True):
        if cat and cat in text and cat not in seen:
            hits.append((cat, cat))
            seen.add(cat)
    for word, cat in sorted(LEX["category_synonyms"].items(), key=lambda kv: -len(kv[0])):
        if word in text:
            # 同义词表可能指向上游 catalog 尚未收录的类目。此时退到「其他」，
            # 而不是把无效类目写进硬约束 —— 那会让过滤后候选为空，只能靠降级兜底。
            # 「其他」与 catalog 对缺失类目的默认取值一致（见 src/catalog.py）。
            target = cat if cat in catalog_categories else "其他"
            if target not in seen:
                hits.append((target, word))
                seen.add(target)
    return hits


def _match_colors(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    lowered = text.lower()
    for color, words in LEX["colors"].items():
        for word in sorted(words, key=len, reverse=True):
            if (word.lower() in lowered) and color not in seen:
                hits.append((color, word))
                seen.add(color)
                break
    return hits


def _match_scenes(text: str) -> list[tuple[str, str]]:
    hits: list[tuple[str, str]] = []
    seen: set[str] = set()
    for word, scene in sorted(LEX["scenes"].items(), key=lambda kv: -len(kv[0])):
        if word in text and scene not in seen:
            hits.append((scene, word))
            seen.add(scene)
    return hits


def _match_budget(text: str) -> dict | None:
    """解析预算表达，支持「1000以上500以内」这类多数字表达。

    返回 {min,max,target,direction,raw,evidence}；无法解析返回 None。
    """
    cfg = LEX["budget"]
    lowered = text.lower()
    has_unit = any(w in lowered for w in cfg["unit_words"]) or any(
        w in text for w in ["预算", "价格", "花费", "价位", "budget", "price"]
    )

    def _direction(window: str) -> tuple[str | None, str | None]:
        lowered = window.lower()
        for group, label in (
            (cfg["under_words"], "under"),
            (cfg["over_words"], "over"),
            (cfg["around_words"], "around"),
        ):
            for word in group:
                if word in lowered or word in window:
                    return label, word
        return None, None

    matches: list[tuple[int, int, float, str]] = []
    for m in _NUM_RE.finditer(text):
        value = float(m.group(1).replace(",", ""))
        # 0 不是有意义的预算；顺带挡住「3 0 0」被逐位拆开后产出 price<=0 这种不可能约束
        if value > 0:
            matches.append((m.start(), m.end(), value, m.group(0)))
    # 中文数字（三百 / 五百 / 一百五）：必须是「数量词紧邻方向词或计量单位」的形态才采纳。
    # 用紧邻 3 字判断而不是 ±6 字窗口 —— 后者会让「推荐一些两百块以内」里的
    # 「一」（来自「一些」）借到远处窗口中的「以内」，产出 price<=1 这种错误约束。
    markers = [*cfg["under_words"], *cfg["over_words"], *cfg["around_words"], *cfg["unit_words"]]

    def _adjacent_marker(idx_from: int, idx_to: int) -> bool:
        before = text[max(0, idx_from - 3) : idx_from]
        after = text[idx_to : idx_to + 3]
        return any(w in before or w in after for w in markers)

    for m in _CN_NUM_RE.finditer(text):
        cn_value = _cn_to_int(m.group(0))
        if not cn_value:
            continue
        if not _adjacent_marker(m.start(), m.end()):
            continue
        matches.append((m.start(), m.end(), float(cn_value), m.group(0)))
    matches.sort(key=lambda item: item[0])

    pairs: list[tuple[float, str | None, str | None, str]] = []
    for start, end, value, raw in matches:
        window = text[max(0, start - 6) : min(len(text), end + 6)]
        direction, marker = _direction(window)
        pairs.append((value, direction, marker, raw))
    if not pairs:
        return None

    unders = [v for v, d, _, _ in pairs if d == "under"]
    overs = [v for v, d, _, _ in pairs if d == "over"]
    arounds = [v for v, d, _, _ in pairs if d == "around"]

    if not unders and not overs and not arounds:
        if has_unit and any(w in text for w in ["预算", "价位", "budget"]):
            unders = [pairs[0][0]]
        else:
            return None

    near = cfg["near"]
    lo = hi = target = None
    direction = "range"
    if arounds and not unders and not overs:
        target = arounds[0]
        lo, hi = round(target * (1 - near), 4), round(target * (1 + near), 4)
        direction = "around"
    else:
        if overs:
            lo = max(overs)
        if unders:
            hi = min(unders)
        if lo is None and hi is not None:
            target, direction = hi, "under"
        elif hi is None and lo is not None:
            target, direction = lo, "over"
        elif lo is not None and hi is not None:
            target = hi
            direction = "range"

    spans = "，".join(f"「{marker or ''}{num}」" for num, _, marker, _ in pairs if marker)
    evidence = f"文本预算表达 {spans}" if spans else f"文本预算数字「{pairs[0][3]}」"
    return {"min": lo, "max": hi, "target": target, "direction": direction, "raw": spans, "evidence": evidence}


def _match_feedback_polarity(text: str, gesture: dict | None) -> tuple[str | None, str]:
    """返回 (polarity, evidence)。手势优先，其次文本。"""
    if gesture and gesture.get("polarity"):
        return gesture["polarity"], f"手势 {gesture.get('name')}({gesture.get('confidence')})"
    neg = contains_any(text, LEX["feedback_words"]["negative"])
    pos = contains_any(text, LEX["feedback_words"]["positive"])
    if neg:
        return "negative", f"文本否定词「{neg}」"
    if pos:
        return "positive", f"文本肯定词「{pos}」"
    return None, ""


# ============================================================
# 意图分类
# ============================================================
def classify(
    event: InteractionEvent, slots: dict[str, Slot], catalog_categories: list[str]
) -> tuple[str, float, dict[str, float]]:
    text = (event.text or "").strip()
    gesture = event.gesture or {}
    scores: dict[str, float] = dict.fromkeys(CFG["intents"], 0.0)

    # 反馈
    if gesture.get("is_feedback"):
        scores["feedback"] += 1.0
    if slots.get("polarity") and slots["polarity"].value:
        scores["feedback"] += 0.6
    if contains_any(text, LEX["feedback_words"]["positive"] + LEX["feedback_words"]["negative"]):
        scores["feedback"] += 0.2

    # 相似
    if contains_any(text, LEX["similar_markers"]):
        scores["similar"] += 0.9
    # 对比
    if contains_any(text, LEX["compare_markers"]):
        scores["compare"] += 0.9
    # 解释
    if contains_any(text, LEX["explain_markers"]):
        scores["explain"] += 0.9

    # 硬约束筛选
    hard_fields = [c.name for c in slots.values() if c.name in ("budget_min", "budget_max", "colors")]
    if "budget_min" in hard_fields or "budget_max" in hard_fields:
        scores["filter"] += 0.8
    if "colors" in hard_fields:
        scores["filter"] += 0.5
    if contains_any(text, ["只看", "只要", "筛选", "必须", "限定", "不超过", "以内", "以下", "少于", "only"]):
        scores["filter"] += 0.5

    # 发现
    if contains_any(
        text,
        ["推荐", "看看", "有什么", "随便", "逛逛", "发现", "来点", "给我", "帮我找", "找点", "recommend", "browse"],
    ):
        scores["discover"] += 0.6
    if slots.get("category") or slots.get("scene"):
        scores["discover"] += 0.3

    # 目标实体指向 → 更可能是 similar/explain/compare
    if slots.get("target"):
        scores["similar"] += 0.25
        scores["explain"] += 0.15
    if slots.get("targets"):
        scores["compare"] += 0.5

    # 空文本：只能依靠手势/视觉
    if not text:
        if gesture.get("is_feedback"):
            scores["feedback"] += 0.5
        elif event.visual_tags:
            scores["discover"] += 0.2

    # 取最高分（按优先级打破平局）
    best, best_score = "discover", scores["discover"]
    for intent in CFG["priority"]:
        value = scores.get(intent, 0.0)
        if value > best_score + 1e-9:
            best, best_score = intent, value
    confidence = min(1.0, round(best_score, 4))
    if confidence < CFG["min_intent_confidence"]:
        return "discover", confidence, scores
    return best, confidence, scores


# ============================================================
# 槽位抽取
# ============================================================
def extract_slots(
    event: InteractionEvent,
    *,
    catalog_categories: list[str],
    focus_item_id: str | None = None,
    session_items: list[str] | None = None,
    title_lookup=None,
) -> dict[str, Slot]:
    text = (event.text or "").strip()
    gesture = event.gesture or {}
    slots: dict[str, Slot] = {}

    # --- 类目 ---
    cat_hits = _match_categories(text, catalog_categories)
    if cat_hits:
        cat, word = cat_hits[0]
        slots["category"] = Slot(
            "category", cat, confidence=0.85 if word == cat else 0.75, evidence=[f"文本命中类目词「{word}」"]
        )
        if len(cat_hits) > 1:
            slots["category_alt"] = Slot(
                "category_alt", [c for c, _ in cat_hits[1:]], confidence=0.5, evidence=["文本命中多个类目词"]
            )

    # 举物品识图：没有文字时，VL 描述本身也应能提供品类线索。
    if "category" not in slots and event.visual_tags:
        visual_categories: list[tuple[str, float, str, str]] = []
        for tag in event.visual_tags:
            for cat, word in _match_categories(tag.tag, catalog_categories):
                eff = float(tag.raw.get("effective_confidence", tag.confidence))
                visual_categories.append((cat, eff, tag.tag, word))
        if visual_categories:
            visual_categories.sort(key=lambda x: (-x[1], -len(x[3])))
            cat, eff, raw_tag, word = visual_categories[0]
            slots["category"] = Slot(
                "category",
                cat,
                confidence=round(eff, 4),
                evidence=[f"视觉标签「{raw_tag}」识别为「{cat}」（命中「{word}」）"],
            )

    # --- 颜色 ---
    color_hits = _match_colors(text)
    if color_hits:
        slots["colors"] = Slot(
            "colors",
            [c for c, _ in color_hits],
            confidence=0.8,
            evidence=[f"文本命中颜色词「{w}」" for _, w in color_hits],
        )
    # 视觉标签也可提供颜色线索（低置信度已降权）
    if event.visual_tags:
        vcolors = []
        for tag in event.visual_tags:
            for color, words in LEX["colors"].items():
                if any(w in tag.tag for w in words):
                    eff = float(tag.raw.get("effective_confidence", tag.confidence))
                    vcolors.append((color, eff, tag.tag))
        if vcolors:
            vcolors.sort(key=lambda x: -x[1])
            best_color, eff, raw_tag = vcolors[0]
            if "colors" not in slots:
                slots["colors"] = Slot(
                    "colors",
                    [best_color],
                    confidence=round(eff, 4),
                    evidence=[f"视觉标签「{raw_tag}」(有效置信度 {eff:.2f})"],
                )
            else:
                slots["colors"].evidence.append(f"视觉标签亦指向「{raw_tag}」")

    # --- 场景 ---
    scene_hits = _match_scenes(text)
    if scene_hits:
        scene, word = scene_hits[0]
        slots["scene"] = Slot("scene", scene, confidence=0.7, evidence=[f"文本命中场景词「{word}」"])

    # --- 预算 ---
    budget = _match_budget(text)
    if budget:
        if budget["max"] is not None:
            slots["budget_max"] = Slot(
                "budget_max", budget["max"], confidence=0.85, evidence=[budget["evidence"] + f" → 上限 {budget['max']}"]
            )
        if budget["min"] is not None:
            slots["budget_min"] = Slot(
                "budget_min", budget["min"], confidence=0.85, evidence=[budget["evidence"] + f" → 下限 {budget['min']}"]
            )
        slots["budget_raw"] = Slot("budget_raw", budget, confidence=0.7, evidence=[budget["evidence"]])

    # --- 对象指向（target / targets） ---
    explicit = _resolve_explicit_targets(text, title_lookup)
    session_items = session_items or []
    if explicit:
        slots["targets"] = Slot(
            "targets", explicit, confidence=0.8, evidence=[f"文本显式提到商品：{', '.join(explicit)}"]
        )
        slots["target"] = Slot("target", explicit[0], confidence=0.8, evidence=["取第一个显式商品作为参照"])
    else:
        ordinal_target = _resolve_ordinal_target(text, session_items)
        if ordinal_target:
            slots["target"] = Slot(
                "target", ordinal_target, confidence=0.75, evidence=["文本序号指代 + 上一轮推荐结果"]
            )
            slots["targets"] = Slot("targets", [ordinal_target], confidence=0.7, evidence=["文本序号指代"])
        referential = contains_any(text, LEX.get("referential_markers", ["这个", "这款", "它"]))
        if not ordinal_target and referential and (focus_item_id or session_items):
            target = focus_item_id or session_items[0]
            slots["target"] = Slot("target", target, confidence=0.7, evidence=["文本指代词 + 会话焦点商品"])
            slots["targets"] = Slot("targets", [target], confidence=0.6, evidence=["会话焦点商品"])
        elif event.visual_tags and focus_item_id:
            slots["target"] = Slot("target", focus_item_id, confidence=0.6, evidence=["视觉焦点商品"])

    # --- 反馈极性 ---
    polarity, evidence = _match_feedback_polarity(text, gesture)
    if polarity:
        slots["polarity"] = Slot(
            "polarity", polarity, confidence=0.9 if gesture.get("is_feedback") else 0.7, evidence=[evidence]
        )
        target_slot = slots.get("target")
        if target_slot:
            slots["feedback_target"] = Slot(
                "feedback_target",
                target_slot.value,
                confidence=target_slot.confidence,
                evidence=["反馈指向会话焦点商品"],
            )
        elif focus_item_id:
            slots["feedback_target"] = Slot(
                "feedback_target", focus_item_id, confidence=0.7, evidence=["反馈指向当前焦点商品"]
            )

    return slots


_ORDINAL_RE = re.compile(r"第\s*([0-9]+|[一二两三四五六七八九十]+)\s*(?:个|件|款|项|条|号)")


def _resolve_ordinal_target(text: str, session_items: list[str] | None) -> str | None:
    """把「第一个 / 第2款」解析为上一轮推荐结果里的具体商品。"""
    if not text or not session_items:
        return None
    match = _ORDINAL_RE.search(text)
    if not match:
        return None
    raw = match.group(1)
    # 复用完整中文数字解析：第十=10、第十五=15、第二十=20、第二十一=21。
    # 旧实现只处理「十X」，且把单独的「十」算成 20。
    index: int | None = int(raw) if raw.isdigit() else _cn_to_int(raw)
    if not index or index < 1 or index > len(session_items):
        return None
    return session_items[index - 1]


def _resolve_explicit_targets(text: str, title_lookup) -> list[str]:
    """在文本里识别「显式提到的商品」。离线模式下按标题包含关系匹配。"""
    if not text or title_lookup is None:
        return []
    found: list[str] = []
    for name in LEX["similar_markers"] + LEX["compare_markers"] + LEX["explain_markers"]:
        text = text.replace(name, " ")
    for item_id, title in title_lookup():
        if title and len(title) >= 3 and title in text and item_id not in found:
            found.append(item_id)
    return found[:3]


# ============================================================
# 约束构造与冲突检测
# ============================================================
FILTER_MARKERS = ["只看", "只要", "筛选", "必须", "限定", "only", "不超过", "以内", "以下"]


def build_constraints(slots: dict[str, Slot], text: str, intent: str | None = None) -> list[Constraint]:
    """把槽位转成约束。

    注意：**反馈意图下不得把 target 转成 similar_to 软约束**。
    反馈里的 target 是「被评价的商品」，不是「相似参照物」；
    若转成 similar_to，会与该商品相似的其他商品获得约束匹配加分，
    反而把被点赞的商品本身挤下去（实测：点赞 #1 后 #1 掉到 #2，属真实缺陷）。
    """
    constraints: list[Constraint] = []
    explicit_filter = contains_any(text, FILTER_MARKERS) is not None

    if "budget_max" in slots:
        constraints.append(Constraint("price", "lte", slots["budget_max"].value, kind="hard", source="slot:budget_max"))
    if "budget_min" in slots:
        constraints.append(Constraint("price", "gte", slots["budget_min"].value, kind="hard", source="slot:budget_min"))
    if "colors" in slots:
        constraints.append(Constraint("color", "in", slots["colors"].value, kind="hard", source="slot:colors"))
    if "category" in slots:
        kind = "hard" if explicit_filter else "soft"
        constraints.append(Constraint("category", "eq", slots["category"].value, kind=kind, source="slot:category"))
    if "scene" in slots:
        constraints.append(Constraint("scene", "eq", slots["scene"].value, kind="soft", source="slot:scene"))
    if "target" in slots and intent != "feedback":
        constraints.append(
            Constraint("item_id", "similar_to", slots["target"].value, kind="soft", source="slot:target")
        )
    if "targets" in slots and len(slots["targets"].value) >= 2:
        constraints.append(
            Constraint("item_ids", "compare", slots["targets"].value, kind="soft", source="slot:targets")
        )
    return constraints


def detect_conflicts(slots: dict[str, Slot]) -> list[dict]:
    conflicts: list[dict] = []
    bmin = slots.get("budget_min")
    bmax = slots.get("budget_max")
    if bmin and bmax and float(bmin.value) > float(bmax.value):
        conflicts.append(
            {
                "id": "budget_range",
                "desc": "预算下限高于上限",
                "detail": {"min": bmin.value, "max": bmax.value},
                "hint": f"你给的预算下限 {bmin.value} 高于上限 {bmax.value}，请确认。",
            }
        )
    colors = slots.get("colors")
    # 按 id 查规则而不是按下标 —— CONFLICT_RULES 顺序调整时这里不该悄悄失效
    color_rule = next((r for r in CONFLICT_RULES if r["id"] == "color_exclusive"), None)
    if colors and color_rule and len(colors.value) > color_rule["max_ok"]:
        conflicts.append(
            {
                "id": "color_exclusive",
                "desc": "同时指定了过多互斥颜色",
                "detail": {"colors": colors.value},
                "hint": f"一次最多保留 {color_rule['max_ok']} 种颜色，请删减。",
            }
        )
    cat = slots.get("category")
    scene = slots.get("scene")
    if cat and scene is not None and scene.value in ("厨房",) and cat.value in ("装饰品", "购物袋"):
        conflicts.append(
            {
                "id": "category_scene",
                "desc": "类目与场景可能冲突",
                "detail": {"category": cat.value, "scene": scene.value},
                "hint": f"「{cat.value}」通常不属于「{scene.value}」场景，请确认。",
            }
        )
    return conflicts


# ============================================================
# 主入口
# ============================================================
def understand(
    event: InteractionEvent,
    *,
    catalog_categories: list[str],
    focus_item_id: str | None = None,
    session_items: list[str] | None = None,
    title_lookup=None,
    top_k: int = 8,
    session_id: str | None = None,
    mode: str = "offline",
) -> Result:
    """把一次交互事件解析为推荐请求；必要槽位缺失或约束冲突时返回明确错误。"""
    slots = extract_slots(
        event,
        catalog_categories=catalog_categories,
        focus_item_id=focus_item_id,
        session_items=session_items,
        title_lookup=title_lookup,
    )
    intent, confidence, scores = classify(event, slots, catalog_categories)
    constraints = build_constraints(slots, event.text or "", intent)
    conflicts = detect_conflicts(slots)

    result = IntentResult(
        intent=intent,
        confidence=confidence,
        intent_scores=scores,
        slots=slots,
        constraints=constraints,
        conflicts=conflicts,
        evidence={
            "modality": event.modality,
            "text": event.text,
            "visual_tag_count": len(event.visual_tags),
            "gesture": event.gesture,
        },
    )

    if conflicts:
        return fail(
            ErrorCode.CONSTRAINT_CONFLICT,
            conflicts[0]["hint"],
            field=conflicts[0]["id"],
            conflicts=conflicts,
            intent_result=result.to_dict(),
            clarification=conflicts[0]["hint"],
        )

    required = CFG["required_slots"].get(intent, [])
    missing: list[str] = []
    for slot_name in required:
        if slot_name == "constraint_any":
            if not any(c.kind == "hard" for c in constraints):
                missing.append("至少一个硬约束（预算/颜色/品类筛选）")
        elif slot_name == "targets":
            targets = slots.get("targets")
            if not targets or len(targets.value) < 2:
                missing.append("两个及以上待对比商品")
        elif slot_name not in slots or slots[slot_name].value in (None, "", []):
            missing.append(slot_name)

    if missing:
        question = _clarification_question(intent, missing)
        return fail(
            ErrorCode.MISSING_SLOT,
            question,
            field=",".join(missing),
            intent=intent,
            missing_slots=missing,
            intent_result=result.to_dict(),
            clarification=question,
        )

    request = RecommendationRequest(
        trace_id=event.trace_id,
        user_id=event.user_id,
        timestamp=event.timestamp,
        source=event.source,
        intent=intent,
        slots=slots,
        constraints=constraints,
        top_k=top_k,
        session_id=session_id,
        mode=mode,
    )
    return ok({"request": request, "intent_result": result.to_dict(), "clarification": None})


def _clarification_question(intent: str, missing: list[str]) -> str:
    joined = "、".join(missing)
    if intent == "similar":
        return f"想找哪件商品的相似款呢？请补充参照商品（缺少：{joined}）。"
    if intent == "compare":
        return f"想对比哪两件商品？请补充（缺少：{joined}）。"
    if intent == "explain":
        return f"想让我解释哪件商品的推荐理由？请补充（缺少：{joined}）。"
    if intent == "feedback":
        return f"你想表达喜欢还是不喜欢？请补充（缺少：{joined}）。"
    if intent == "filter":
        return f"请补充筛选条件（缺少：{joined}）。"
    return f"还需要补充：{joined}。"
