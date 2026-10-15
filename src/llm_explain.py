"""
src/llm_explain.py
==================

用真实 LLM 生成推荐解释，并对输出做 **后验事实校验**。

为什么必须校验
--------------
实测 Qwen2-1.5B-Instruct 在「价格=2.1、预算≤500」的证据下会输出
「参考价 2.09」「预算不超过 200」「参考价格 2.30」—— 凭空编造数字。
若无校验直接展示，就违背了阶段4 的验收项「无证据时不编造」。

因此本模块采用三段式：
    1. 构造 **强约束提示词**：要求只用给定事实、不得新增数字；
    2. 真实 LLM 生成（通过 MultimodalAdapter 的子进程后端调用）；
    3. **后验校验**：任何无法在证据包中溯源的数字/类目/商品名出现即判定失败，
       失败则丢弃 LLM 文本，回退到模板解释（模板解释由 src/explain.py 保证逐句有据）。

校验失败不是异常，而是正常分支：`grounded=False` + `fallback_reason` 会一并落盘，
便于统计真实模型的「可用解释率」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from config import settings

from .contracts import EvidencePack
from .errors import Result, ok
from .text_norm import normalize_fluency, verify_fluency

NL = chr(10)
CFG = settings.LLM_EXPLAIN_CONFIG

_NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
# 允许出现的“非事实类”数字（序号、年份等通用表达不做限制，这里保持严格：一律要在证据里）
_CN_NUM = "零一二三四五六七八九十百千两"


_LEAD_RE = re.compile(r"^[\d.、)\-*•·\s]+")


def normalize_output(text: str) -> str:
    """把 LLM 原始输出规范成「一句话」候选文本。

    小模型常无视「只输出一句话」的约束，输出多行、带序号、自我重复。
    这里做**机械规范化**（取首个非空行 → 去列表符 → 按句边界截断），
    随后仍要经过事实门控与格式门控 —— 展示给用户的一定是校验过的文本。
    """
    cfg = CFG
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    first = _LEAD_RE.sub("", _LIST_RE.sub("", lines[0])).strip()
    if len(first) > cfg["max_chars"]:
        cut = max(first.rfind(ch) for ch in "。！？!?.")
        first = first[: cut + 1] if cut >= cfg.get("min_chars", 20) else first[: cfg["max_chars"]]
    return first.strip()


_LIST_RE = re.compile(r"(^|[" + NL + r"])\s*(\d+[.、)]|[-*•·])")
_WS_RE = re.compile(r"\s+")


def verify_format(text: str) -> list[dict]:
    """格式与质量门控。

    实测 Qwen2-1.5B 即使事实合规，也会输出啰嗦、自我重复、多行的文本
    （例如把同一句话换几种说法重复 6 遍）。这类文本虽然「没说错」，
    但作为面向用户的推荐解释不可接受，因此与事实门控分开独立校验。
    """
    cfg = CFG
    violations: list[dict] = []
    raw = (text or "").strip()
    if not raw:
        return [{"type": "empty_output"}]

    if len(raw) > cfg["max_chars"]:
        violations.append({"type": "too_long", "chars": len(raw), "limit": cfg["max_chars"]})

    lines = [line for line in raw.splitlines() if line.strip()]
    if len(lines) > cfg["max_lines"]:
        violations.append({"type": "multi_line", "lines": len(lines), "limit": cfg["max_lines"]})

    if _LIST_RE.search(raw):
        violations.append({"type": "list_markup"})

    # 自我重复：以 8 字滑窗统计重复
    compact = _WS_RE.sub("", raw)
    window = 8
    grams = [compact[i : i + window] for i in range(max(0, len(compact) - window + 1))]
    if grams:
        seen = {}
        worst, worst_count = "", 0
        for gram in grams:
            seen[gram] = seen.get(gram, 0) + 1
            if seen[gram] > worst_count:
                worst, worst_count = gram, seen[gram]
        if worst_count > cfg["max_repeat"]:
            violations.append(
                {"type": "repetition", "fragment": worst, "count": worst_count, "limit": cfg["max_repeat"]}
            )
    return violations


def build_grounded_prompt(item: dict, pack: EvidencePack, *, style: str | None = None) -> str:
    """构造强约束提示词：只用给定事实，不得新增任何数字。"""
    facts = pack.product_facts or {}
    lines: list[str] = []
    lines.append("商品：" + str(facts.get("title") or item.get("item_id")))
    if facts.get("category"):
        lines.append("类目：" + str(facts["category"]))
    if facts.get("price") is not None:
        lines.append("参考价：" + str(facts["price"]))
    for hit in (pack.matched_constraints or [])[:4]:
        if hit.get("field") == "price":
            op = "不超过" if hit.get("op") == "lte" else "不低于"
            lines.append("命中约束：价格" + op + str(hit.get("required")))
        elif hit.get("field") == "category":
            lines.append("命中约束：品类为" + str(hit.get("required")))
        elif hit.get("field") == "color":
            lines.append("命中约束：颜色为" + str(hit.get("required")))
    for src in (pack.candidate_sources or [])[:2]:
        lines.append("候选来源：" + str(src))
    for signal in (pack.session_signals or [])[:2]:
        lines.append("会话信号：" + str(signal.get("detail")))
    for tag in (pack.visual_tags or [])[:2]:
        lines.append("画面标签：" + str(tag.tag))

    fact_block = "\n".join("- " + line for line in lines)
    instruction = style or CFG["instruction"]
    return (
        instruction
        + "\n\n【事实】（只能使用这些内容，不得补充任何未给出的事实）\n"
        + fact_block
        + "\n\n【输出要求】\n"
        + CFG["output_rules"]
    )


@dataclass
class GroundednessReport:
    ok: bool
    violations: list[dict] = field(default_factory=list)
    allowed_numbers: list[str] = field(default_factory=list)
    found_numbers: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": self.violations,
            "allowed_numbers": self.allowed_numbers,
            "found_numbers": self.found_numbers,
        }


def _allowed_numbers(item: dict, pack: EvidencePack) -> tuple[set[float], set[str]]:
    """返回 (允许出现的数值集合, 允许逐字出现的 token 集合)。

    数值用**精确比较**（容差 1e-6），不做四舍五入近似 ——
    否则「2.09」会因为四舍五入到 1 位等于「2.1」而被误放行。
    """
    values: list[float] = []
    facts = pack.product_facts or {}
    if facts.get("price") is not None:
        values.append(float(facts["price"]))
    if item.get("price") is not None:
        values.append(float(item["price"]))
    for hit in pack.matched_constraints or []:
        for key in ("required", "actual"):
            if isinstance(hit.get(key), (int, float)):
                values.append(float(hit[key]))
    for tag in pack.visual_tags or []:
        values.append(float(tag.confidence))
        values.append(float(tag.raw.get("effective_confidence", tag.confidence)))
    for snippet in pack.evidence_snippets or []:
        if isinstance(snippet.get("score"), (int, float)):
            values.append(float(snippet["score"]))
    if isinstance(facts.get("transactions"), (int, float)):
        values.append(float(facts["transactions"]))

    # 商品编号 / 标题里出现的数字属于事实本身，按字面放行
    tokens: set[str] = set()
    for text in (str(facts.get("item_id", "")), str(facts.get("title", "")), str(facts.get("english_title", ""))):
        tokens.update(_NUM_RE.findall(text))
    return set(values), tokens


def _mentioned_categories(text: str, categories) -> set:
    """返回文本中真正出现的类目名。

    坑：中文类目之间存在**包含关系** —— 目录里同时有「装饰品」和「饰品」，
    朴素子串匹配会把「装饰品」误判成提到了「饰品」。
    这里按长度降序匹配，命中后把该片段用占位符掩掉，
    短类目就不会再匹配到长类目的内部。
    """
    working = text or ""
    found: set = set()
    for category in sorted([c for c in categories if c], key=len, reverse=True):
        start = 0
        while True:
            index = working.find(category, start)
            if index < 0:
                break
            found.add(category)
            working = working[:index] + ("\x00" * len(category)) + working[index + len(category) :]
            start = index + len(category)
    return found


def verify_groundedness(text: str, item: dict, pack: EvidencePack, catalog=None) -> GroundednessReport:
    """校验 LLM 输出是否完全可溯源。任何不可溯源项都记为一个 violation。"""
    violations: list[dict] = []
    allowed_values, allowed_tokens = _allowed_numbers(item, pack)
    found = _NUM_RE.findall(text or "")

    for token in found:
        normalized = token.replace(",", "")
        if normalized in allowed_tokens:
            continue
        try:
            as_float = float(normalized)
        except ValueError:
            violations.append(
                {"type": "fabricated_number", "token": token, "reason": "无法解析为数值且不在商品编号/标题中"}
            )
            continue
        if any(abs(as_float - value) < 1e-6 for value in allowed_values):
            continue
        violations.append(
            {"type": "fabricated_number", "token": token, "value": as_float, "reason": "该数值无法在证据包中溯源"}
        )

    # 类目一致性：输出若提到目录中的其它类目名，视为编造
    facts = pack.product_facts or {}
    true_category = str(facts.get("category") or "")
    if catalog is not None and text:
        for category in _mentioned_categories(text, catalog.categories()):
            if category != true_category:
                violations.append({"type": "wrong_category", "mentioned": category, "expected": true_category})

    # 商品名一致性：不得出现别的商品标题
    if catalog is not None and text:
        title = str(facts.get("title") or "")
        for product in list(catalog.catalog.values())[:4000]:
            other_title = product.get("title") or ""
            if other_title and other_title != title and len(other_title) >= 5 and other_title in text:
                violations.append({"type": "wrong_item", "mentioned": other_title})
                break

    return GroundednessReport(
        ok=not violations,
        violations=violations,
        allowed_numbers=sorted(f"{v:g}" for v in allowed_values)[:20],
        found_numbers=found,
    )


def evaluate_output(raw_text: str, item: dict, pack: EvidencePack, catalog=None) -> dict:
    """统一的「规范化 + 三道门控」评估入口。

    **生产链路（LlmExplainer）与评测脚本（scripts/10、scripts/11）必须共用本函数**，
    否则报出的接受率不代表用户实际拿到的东西 —— 这正是之前踩过的坑：
    评测脚本少跑一道流畅度规范化，导致「butterfly」这类残留英文在评测里"通过"了。
    """
    normalized = normalize_output(raw_text)
    normalized, fluency_actions = normalize_fluency(normalized)
    fact = verify_groundedness(normalized, item, pack, catalog)
    fmt = verify_format(normalized)
    fluency = verify_fluency(normalized)
    return {
        "text": normalized,
        "fluency_actions": fluency_actions,
        "fact": fact,
        "format": fmt,
        "fluency": fluency,
        "accepted": bool(fact.ok and not fmt and not fluency),
        "rejection": {"violations": (fact.violations + fmt + fluency)},
    }


class LlmExplainer:
    """真实 LLM 解释器：强约束提示 + 后验校验 + 失败回退。"""

    def __init__(self, adapter, catalog=None, enabled: bool | None = None) -> None:
        self.adapter = adapter
        self.catalog = catalog
        self.enabled = CFG["enabled"] if enabled is None else enabled
        # 值类型不齐（计数是 int，reject_details 是 list），显式标注为 Any，
        # 否则 mypy 推断成 dict[str, object]，后续 += 1 / .append() 全部报错。
        self.stats: dict[str, Any] = {
            "attempted": 0,
            "accepted": 0,
            "rejected": 0,
            "errors": 0,
            "reject_details": [],
        }

    def available(self) -> bool:
        return bool(self.enabled and self.adapter is not None and self.adapter.active_mode == "real")

    def explain(self, item: dict, pack: EvidencePack, *, template_text: str) -> Result:
        """返回 {text, source, grounded, ...}；LLM 不可用或校验失败时回退模板解释。"""
        if not self.available():
            return ok(
                {
                    "text": template_text,
                    "source": "template",
                    "grounded": True,
                    "fallback_reason": "LLM 未启用或适配器不在 real 模式",
                }
            )

        self.stats["attempted"] += 1
        prompt = build_grounded_prompt(item, pack)
        result = self.adapter.guard(
            "llm_explain",
            lambda: self.adapter._run_worker(
                "llm", {"prompt": prompt, "max_new_tokens": CFG["max_new_tokens"], "temperature": CFG["temperature"]}
            ),
        )
        if not result.ok:
            self.stats["errors"] += 1
            reason = result.error.message if result.error else "LLM 调用失败"
            return ok(
                {
                    "text": template_text,
                    "source": "template",
                    "grounded": True,
                    "fallback_reason": "LLM 调用失败: " + reason,
                }
            )

        raw_output = str((result.data or {}).get("text") or "").strip()
        raw = normalize_output(raw_output)
        # 流畅度规范化：繁转简 + 常见英文词替换（可选依赖，缺失时只做检测）
        raw, fluency_actions = normalize_fluency(raw)
        model = (result.data or {}).get("model")
        latency = (result.meta or {}).get("latency_ms")
        if not raw:
            self.stats["rejected"] += 1
            return ok(
                {
                    "text": template_text,
                    "source": "template",
                    "grounded": True,
                    "fallback_reason": "LLM 返回空文本",
                    "model": model,
                }
            )

        evaluated = evaluate_output(raw_output, item, pack, self.catalog)
        raw = evaluated["text"]
        report = evaluated["fact"]
        format_problems = evaluated["format"]
        fluency_problems = evaluated["fluency"]

        if not evaluated["accepted"]:
            self.stats["rejected"] += 1
            if fluency_problems:
                self.stats["fluency_rejected"] = self.stats.get("fluency_rejected", 0) + 1
                kind, message = "fluency", "LLM 输出存在繁体字或中英混排，已回退模板解释"
            elif format_problems:
                self.stats["format_rejected"] = self.stats.get("format_rejected", 0) + 1
                kind, message = "format", "LLM 输出不符合格式要求，已回退模板解释"
            else:
                kind, message = "fact", "LLM 输出含不可溯源内容，已回退模板解释"
            self.stats["reject_details"].append(
                {"item_id": pack.item_id, "type": kind, "violations": evaluated["rejection"]["violations"][:3]}
            )
            return ok(
                {
                    "text": template_text,
                    "source": "template",
                    "grounded": True,
                    "llm_rejected": True,
                    "raw_llm_text": raw_output,
                    "normalized_text": raw,
                    "fluency_actions": evaluated["fluency_actions"],
                    "rejection": evaluated["rejection"],
                    "fallback_reason": message,
                    "model": model,
                    "latency_ms": latency,
                }
            )

        self.stats["accepted"] += 1
        return ok(
            {
                "text": raw,
                "source": "llm",
                "grounded": True,
                "raw_llm_text": raw_output,
                "fluency_actions": fluency_actions,
                "groundedness": report.to_dict(),
                "model": model,
                "latency_ms": latency,
            }
        )

    def report(self) -> dict:
        attempted = self.stats["attempted"]
        return {
            **self.stats,
            "accept_rate": round(self.stats["accepted"] / attempted, 4) if attempted else None,
            "reject_rate": round(self.stats["rejected"] / attempted, 4) if attempted else None,
        }
