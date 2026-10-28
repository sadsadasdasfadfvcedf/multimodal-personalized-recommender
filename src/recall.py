"""
src/recall.py
=============

多路召回与候选合并（阶段3）。

多路来源（config.RECALL_CONFIG["sources_enabled"]）:
    personalized  推荐系统 v146 个性化精排
    similar       相似商品召回（以会话焦点/参照商品为种子）
    category_hot  品类热门召回
    item_type     商品类型召回（按标题词命中）
    color         颜色召回（按标题里的颜色词命中）
    material      材质召回（木质 / 玻璃 / 陶瓷 …）
    theme         主题召回（复古 / 花卉 / 心形 …）
    scene         场景召回（圣诞 / 婚礼 / 生日 …）
    history       基于历史购买的相似召回
    popular       全局热门兜底

合并规则：
    同一 item 由多路命中 → 合并来源、取各来源归一化分数最大值，并保留全部证据；
    合并后按 merge_top_k 截断。每条候选都保留 sources/score/evidence，保证可追溯。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from config import settings

from .catalog import Catalog
from .contracts import RecommendationRequest
from .errors import Result, ok
from .session import SessionContext
from .utils import minmax_normalize


class _RecommenderLike(Protocol):
    """recall() 需要的推荐侧能力。

    这里用结构化类型（Protocol）而不是 AdapterBase —— 多路召回调用的是
    **推荐系统适配器**特有的方法，它们并不在基类上；把参数标成基类会让
    类型检查器认为这些调用非法，标成 Protocol 则如实表达「需要什么能力」。
    """

    def personalized_candidates(self, user_id: str, k: int = 80) -> Result: ...

    def similar_candidates(self, seed_item_ids: list[str], k: int = 40, exclude: set[str] | None = None) -> Result: ...

    def category_candidates(self, category: str, k: int = 40, exclude: set[str] | None = None) -> Result: ...

    def history_candidates(self, user_id: str, k: int = 30, exclude: set[str] | None = None) -> Result: ...

    def popular_candidates(self, k: int = 60, exclude: set[str] | None = None) -> Result: ...


CFG = settings.RECALL_CONFIG
SOURCE_LABELS = settings.RECOMMENDER_ADAPTER["sources"]
LEX = settings.SLOT_LEXICON


@dataclass
class Candidate:
    item_id: str
    sources: list[str] = field(default_factory=list)  # 来源标签（可读）
    source_keys: list[str] = field(default_factory=list)  # 来源键（机器可读）
    raw_scores: dict[str, float] = field(default_factory=dict)
    norm_scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, catalog: Catalog | None = None) -> dict:
        product = catalog.product(self.item_id) if catalog else None
        return {
            "item_id": self.item_id,
            "title": (product or {}).get("title", ""),
            "category": (product or {}).get("category", ""),
            "price": (product or {}).get("price"),
            "sources": list(self.sources),
            "source_keys": list(self.source_keys),
            "raw_scores": {k: round(v, 6) for k, v in self.raw_scores.items()},
            "norm_scores": {k: round(v, 6) for k, v in self.norm_scores.items()},
            "evidence": self.evidence,
        }


def recall(
    request: RecommendationRequest,
    adapter: _RecommenderLike,
    catalog: Catalog,
    session: SessionContext | None = None,
) -> Result:
    """执行多路召回，返回合并去重后的候选列表与召回 trace。"""
    enabled = set(CFG["sources_enabled"])
    exclude: set[str] = set(request.exclude_item_ids or [])
    target = str(request.slot_value("target") or "")
    similar_only = request.intent == "similar" and bool(target)
    if similar_only:
        # 相似推荐不能把种子商品自己再推荐给用户。
        exclude.add(target)
    # 已购商品与是否有会话无关：即使调用方没传 session，也必须从所有来源剔除。
    exclude |= catalog.purchased(request.user_id)
    if session:
        exclude |= session.blocked_item_ids()  # 本会话负反馈排除

    per_source: dict[str, list[dict]] = {}
    source_reports: list[dict] = []

    def _record(key: str, result: Result) -> None:
        if result.ok and result.data:
            per_source[key] = result.data
            source_reports.append(
                {"source": key, "label": SOURCE_LABELS.get(key, key), "status": "ok", "count": len(result.data)}
            )
        else:
            source_reports.append(
                {
                    "source": key,
                    "label": SOURCE_LABELS.get(key, key),
                    "status": "empty" if result.error is None else "failed",
                    "count": 0,
                    "error": result.error.to_dict() if result.error else None,
                }
            )

    def _term_source(key: str, words: list[str], detail: dict) -> None:
        """按标题词召回一路候选（颜色/材质/主题/场景共用）。

        这些属性都只能从标题核验（见 ranking.hard_filter），所以候选也必须
        从标题建池；否则硬约束会先淘汰掉所有候选，再被降级放宽，属性就没生效。
        词表为空时不写 source_reports，避免没有该条件的查询多出一条空来源。
        """
        cleaned = list(dict.fromkeys(str(word).strip() for word in words if str(word).strip()))
        if not cleaned:
            return
        label = SOURCE_LABELS.get(key, key)
        ids = [item_id for item_id in catalog.title_matches(cleaned, k=CFG["recall_k"] * 2) if item_id not in exclude][
            : CFG["recall_k"]
        ]
        if not ids:
            source_reports.append({"source": key, "label": label, "status": "empty", "count": 0, "error": None})
            return
        peak = max(catalog.popularity(item_id) for item_id in ids) or 1
        per_source[key] = [
            {
                "item_id": item_id,
                "score": round(catalog.popularity(item_id) / peak, 6),
                "source_key": key,
                "source_label": label,
                "detail": detail,
            }
            for item_id in ids
        ]
        source_reports.append({"source": key, "label": label, "status": "ok", "count": len(ids)})

    # 1) 个性化
    if "personalized" in enabled:
        _record("personalized", adapter.personalized_candidates(request.user_id, CFG["recall_k"]))

    # 2) 相似（种子 = 参照商品 + 会话焦点 + 个性化 Top-N）
    if "similar" in enabled:
        seeds: list[str] = []
        if target:
            seeds.append(target)
        if not similar_only:
            if session and session.focus_item_id:
                seeds.append(session.focus_item_id)
            for row in per_source.get("personalized", [])[: CFG["similar_seed_size"]]:
                seeds.append(str(row["item_id"]))
        seeds = [s for s in dict.fromkeys(seeds) if catalog.exists(s)]
        if seeds:
            _record("similar", adapter.similar_candidates(seeds, CFG["recall_k"], exclude=exclude))

    # 3) 品类热门（主类目 + 备用类目 category_alt）
    # 「生日礼物」这类说法会同时给出 派对用品 / 装饰品 两个类目，只召回主类目
    # 会让备用类目只能靠个性化/热门碰运气。
    if "category_hot" in enabled:
        category = request.slot_value("category")
        if not category:
            for constraint in request.constraints:
                if constraint.field == "category":
                    category = constraint.value
                    break
        categories: list[str] = []
        if category:
            categories.append(str(category))
        alt = request.slot_value("category_alt")
        if isinstance(alt, str):
            alt = [alt]
        for name in alt or []:
            if str(name) not in categories:
                categories.append(str(name))
        if categories:
            rows: list[dict] = []
            failures: list = []
            for name in categories:
                result = adapter.category_candidates(name, CFG["recall_k"], exclude=exclude)
                if result.ok and result.data:
                    rows.extend(result.data)
                elif not result.ok and result.error is not None:
                    failures.append(result.error)
            if rows or not failures:
                _record("category_hot", ok(rows))
            else:
                source_reports.append(
                    {
                        "source": "category_hot",
                        "label": SOURCE_LABELS.get("category_hot", "category_hot"),
                        "status": "failed",
                        "count": 0,
                        "error": failures[0].to_dict(),
                    }
                )

    # 3.2) 具体商品类型召回（茶壶 / 相框 / 贺卡等）
    if "item_type" in enabled:
        raw_terms = request.slot_value("item_type_terms") or []
        terms = [str(term) for term in raw_terms] if isinstance(raw_terms, list) else []
        if terms:
            category = request.slot_value("category")
            ids = [
                item_id
                for item_id in catalog.title_matches(
                    terms,
                    category=str(category) if category else None,
                    k=CFG["recall_k"] * 2,
                )
                if item_id not in exclude
            ][: CFG["recall_k"]]
            item_type = request.slot_value("item_type")
            type_cfg = LEX.get("product_types", {}).get(str(item_type), {}) if item_type else {}
            related_terms = [str(term) for term in type_cfg.get("related_terms", [])]
            if related_terms:
                related_ids = [
                    item_id
                    for item_id in catalog.title_matches(related_terms, category=None, k=CFG["recall_k"] * 2)
                    if item_id not in exclude and item_id not in ids
                ]
                ids = list(dict.fromkeys([*ids, *related_ids]))[: CFG["recall_k"]]
            if ids:
                peak = max(catalog.popularity(item_id) for item_id in ids) or 1
                per_source["item_type"] = [
                    {
                        "item_id": item_id,
                        "score": round(catalog.popularity(item_id) / peak, 6),
                        "source_key": "item_type",
                        "source_label": SOURCE_LABELS.get("item_type", "商品类型召回"),
                        "detail": {"terms": terms, "category": category},
                    }
                    for item_id in ids
                ]
                source_reports.append(
                    {
                        "source": "item_type",
                        "label": SOURCE_LABELS.get("item_type", "商品类型召回"),
                        "status": "ok",
                        "count": len(ids),
                    }
                )
            else:
                source_reports.append(
                    {
                        "source": "item_type",
                        "label": SOURCE_LABELS.get("item_type", "商品类型召回"),
                        "status": "empty",
                        "count": 0,
                        "error": None,
                    }
                )

    # 3.4) 属性召回：颜色 / 材质 / 主题 / 场景。
    # 这些属性在精排阶段都是「标题核验」的硬条件，没有对应的召回来源时，
    # 候选池里一旦没有命中商品，硬约束就会把候选全部淘汰，再被降级放宽，
    # 用户看到的便是与输入不符的结果（如「黑色」返回白色相框）。
    if "color" in enabled:
        colors = request.slot_value("colors")
        if not colors:
            for constraint in request.constraints:
                if constraint.field == "color":
                    colors = constraint.value
                    break
        if isinstance(colors, str):
            colors = [colors]
        color_names = [str(color) for color in (colors or []) if str(color).strip()]
        color_words: list[str] = []
        for color in color_names:
            color_words.extend(LEX["colors"].get(color, [color]))
        _term_source("color", color_words, {"colors": color_names, "words": color_words})

    if "material" in enabled:
        material = request.slot_value("material")
        if material:
            material_words = list(LEX.get("materials", {}).get(str(material), [str(material)]))
            _term_source("material", material_words, {"material": str(material), "words": material_words})

    if "theme" in enabled:
        theme = request.slot_value("theme")
        if theme:
            theme_words = list(LEX.get("themes", {}).get(str(theme), [str(theme)]))
            _term_source("theme", theme_words, {"theme": str(theme), "words": theme_words})

    if "scene" in enabled:
        scene = request.slot_value("scene")
        if scene:
            # scenes 是「同义词 → 规范场景」的映射，标题里可能命中任一写法。
            scene_words = [word for word, name in LEX.get("scenes", {}).items() if name == str(scene)]
            scene_words.append(str(scene))
            _term_source("scene", scene_words, {"scene": str(scene), "words": scene_words})

    # 3.5) 会话关注（焦点商品 + 刚点赞商品），保证反馈对象留在候选池内
    if "session_focus" in enabled and session is not None:
        focus_ids: list[str] = []
        if session.focus_item_id:
            focus_ids.append(session.focus_item_id)
        focus_ids.extend(list(session.likes.keys())[:3])
        focus_ids = [i for i in dict.fromkeys(focus_ids) if catalog.exists(i) and i not in exclude]
        if focus_ids:
            per_source["session_focus"] = [
                {
                    "item_id": i,
                    "score": 1.0,
                    "source_key": "session_focus",
                    "source_label": SOURCE_LABELS.get("session_focus", "会话关注召回"),
                    "detail": {"in_session_focus": True, "liked": i in session.likes},
                }
                for i in focus_ids
            ]
            source_reports.append(
                {
                    "source": "session_focus",
                    "label": SOURCE_LABELS.get("session_focus", "会话关注召回"),
                    "status": "ok",
                    "count": len(focus_ids),
                }
            )

    # 4) 历史相似
    if "history" in enabled:
        _record("history", adapter.history_candidates(request.user_id, CFG["recall_k"], exclude=exclude))

    # 5) 全局热门兜底
    if "popular" in enabled:
        _record("popular", adapter.popular_candidates(CFG["recall_k"], exclude=exclude))

    # ---------------- 合并去重 ----------------
    merged: dict[str, Candidate] = {}
    dropped_unknown = 0
    dropped_excluded = 0
    case_insensitive = CFG.get("dedupe_case_insensitive", True)
    alias: dict[str, str] = {}  # 归一化键 → 实际 item_id

    def _norm(item_id: str) -> str:
        return item_id.upper() if case_insensitive else item_id

    for key, rows in per_source.items():
        scores = {_norm(str(r["item_id"])): float(r.get("score") or 0.0) for r in rows}
        norm = minmax_normalize(scores)
        for row in rows:
            item_id = str(row["item_id"])
            if not catalog.exists(item_id):
                dropped_unknown += 1
                continue
            # 排除项（已购/本会话负反馈/请求显式排除）对所有来源一视同仁。
            # 曾豁免 personalized：离线快照的个性化分数里包含已购商品，
            # 导致「已购商品不再推荐」的承诺被打破（如用户 14156 的已购
            # 22699 分数 0.97，曾出现在 TopK 里）。
            if item_id in exclude:
                dropped_excluded += 1
                continue
            nkey = _norm(item_id)
            if nkey in alias:
                item_id = alias[nkey]
            else:
                alias[nkey] = item_id
            cand = merged.get(item_id)
            if cand is None:
                cand = Candidate(item_id=item_id)
                merged[item_id] = cand
            label = row.get("source_label") or SOURCE_LABELS.get(key, key)
            if label not in cand.sources:
                cand.sources.append(label)
            if key not in cand.source_keys:
                cand.source_keys.append(key)
            cand.raw_scores[key] = scores.get(nkey, 0.0)
            cand.norm_scores[key] = norm.get(nkey, 0.0)
            cand.evidence.setdefault("source_detail", {})[key] = row.get("detail", {})

    # 「找相似」优先只使用该商品的相似召回；只有相似源为空时才使用其他来源兜底。
    if similar_only and per_source.get("similar"):
        merged = {item_id: cand for item_id, cand in merged.items() if "similar" in cand.source_keys}

    def _source_priority(cand: Candidate) -> int:
        """属性来源优先进入排序候选池，避免被 merge_top_k 截断。"""
        if "item_type" in cand.source_keys:
            return 3
        if any(key in cand.source_keys for key in ("color", "material", "theme", "scene")):
            return 2
        if "session_focus" in cand.source_keys:
            return 1
        return 0

    ranked = sorted(
        merged.values(),
        key=lambda c: (
            -_source_priority(c),
            -max(c.norm_scores.values() or [0.0]),
            -len(c.source_keys),
            c.item_id,
        ),
    )
    top = ranked[: CFG["merge_top_k"]]

    trace = {
        "sources": source_reports,
        "merged_unique": len(merged),
        "kept": len(top),
        "dropped_unknown_item": dropped_unknown,
        "dropped_excluded": dropped_excluded,
        "dedupe_policy": CFG["dedupe_keep"],
        "exclude_count": len(exclude),
        "multi_source_items": sum(1 for c in merged.values() if len(c.source_keys) > 1),
        "recall_config": {"recall_k": CFG["recall_k"], "merge_top_k": CFG["merge_top_k"]},
    }
    return ok({"candidates": top, "trace": trace})
