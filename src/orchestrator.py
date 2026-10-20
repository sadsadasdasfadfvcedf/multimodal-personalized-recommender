"""
src/orchestrator.py
===================

主链路编排器（阶段3 核心）。

链路（与阶段1 时序图一致）::

    多模态事件 → 意图理解 → 结构化请求 → 多路召回 → 硬约束过滤 → 上下文重排
        → 证据包 → 检索增强解释 → 文本/语音呈现 → 会话状态更新

设计要点：
    - 编排器只依赖适配器接口，不直接访问两个外部系统的内部实现；
    - 每个阶段都记录耗时、模式与降级事件，写入 trace；
    - 同一输入 + 同一配置可复现（无随机、排序稳定）。
"""

from __future__ import annotations

import re
from typing import Any

from config import settings

from .adapters import MultimodalAdapter, RecommenderAdapter
from .asr_service import AsrService
from .camera_service import CameraService
from .catalog import Catalog
from .contracts import RecommendationItem, RecommendationRequest
from .errors import ErrorCode, Result, ok
from .evidence import build_pack
from .explain import explain
from .intent import understand
from .llm_chat import ChatService
from .llm_explain import LlmExplainer
from .presentation import present as present_items
from .rag import KnowledgeBase
from .ranking import rank
from .recall import recall
from .session import SessionContext, SessionStore, Turn
from .utils import Timer, contains_any, new_trace_id, now_iso, stable_hash_int, stable_key

LEX = settings.SLOT_LEXICON
EXPLAIN_TOP_N = 3

CLARIFY_NO_TARGET = "想让我解释哪件商品的推荐理由？请指明商品名称，或先说「这个」。"
CLARIFY_COMPARE = "想对比的两件商品我没有都识别到，请补充商品名称，或先在推荐结果里指一下它们。"
CLARIFY_FEEDBACK = "想对哪件商品点赞或倒赞？请先搜索出商品或点开商品详情，再比 👍/👎。"


class Orchestrator:
    """端到端编排器。"""

    def __init__(
        self,
        *,
        mode: str | None = None,
        allow_real: bool | None = None,
        with_voice: bool = True,
        dry_run_voice: bool = False,
        kb: KnowledgeBase | None = None,
        use_llm: bool | None = None,
        chat_service: Any | None = None,
    ) -> None:
        self.mode = (mode or settings.RUNTIME["mode"]).lower()
        self.multimodal = MultimodalAdapter(mode=self.mode, allow_real=allow_real)
        self.recommender = RecommenderAdapter(mode=self.mode)
        self.catalog: Catalog = self.recommender.catalog or Catalog.from_snapshot()
        self.store = SessionStore()
        self.kb = kb if kb is not None else KnowledgeBase().load()
        self.with_voice = with_voice
        self.dry_run_voice = dry_run_voice
        # 真实 LLM 解释（可选）：仅在适配器进入 real 模式且显式启用时生效；
        # 无论是否启用，解释都必须通过证据校验，不可溯源则回退模板。
        self.llm_explainer = LlmExplainer(self.multimodal, self.catalog, enabled=use_llm)
        # 开放问答（常驻 LLM）：懒启动，不阻塞 Orchestrator 构造。
        # 只在真实模型链路可用时启用，保持 `python webui.py` 默认离线、
        # 不加载重型模型的承诺；测试可注入 chat_service 绕过此门控。
        if chat_service is not None:
            self.chat: Any | None = chat_service
        elif settings.CHAT_CONFIG["enabled"] and (
            self.multimodal.active_mode == "real" or ChatService.online_configured()
        ):
            self.chat = ChatService()
        else:
            self.chat = None
        # LLM 解释复用同一个常驻聊天 worker，避免每件商品重复加载 4B 权重。
        self.llm_explainer.chat = self.chat
        # 常驻 ASR：同样只在 real 模式启用，避免默认离线加载重型模型
        self.asr: Any | None = (
            AsrService() if settings.ASR_CONFIG["enabled"] and self.multimodal.active_mode == "real" else None
        )
        # 常驻摄像头直播：懒启动，只有网页手势直播才会打开设备
        self.camera: Any | None = (
            CameraService() if settings.CAMERA_CONFIG["enabled"] and self.multimodal.active_mode == "real" else None
        )

    # ============================================================
    # 主入口
    # ============================================================
    def handle(
        self,
        spec: dict,
        *,
        user_id: str,
        session_id: str | None = None,
        trace_hint: str | None = None,
        top_k: int | None = None,
    ) -> Result:
        timer = Timer()
        session_id = session_id or self.store.default_session_id(user_id)
        session = self.store.get_or_create(session_id, user_id)

        # 1) 多模态事件解析
        with timer.track("event_ms"):
            event_result = self.multimodal.parse_event(
                spec,
                user_id=user_id,
                session_id=session_id,
                trace_id=new_trace_id(trace_hint) if trace_hint else None,
            )
        if not event_result.ok:
            return self._error_response(event_result, session, timer, source=spec.get("source", "system"))
        event = event_result.data

        session.begin_turn()

        # 2) 会话控制（撤销 / 重新开始）
        control = self._handle_control(event.text or "", session, session_id, user_id)
        if control is not None:
            return ok(
                self._envelope(
                    event, session, timer, status="ok", control=control, note=f"会话控制：{control['action']}"
                )
            )

        # 2.6) 寒暄 / 身份问答：直接回答，不进入推荐链路
        answer = self._smalltalk(event.text)
        if answer is not None:
            response = self._envelope(
                event,
                session,
                timer,
                status="ok",
                intent_result={
                    "intent": "chitchat",
                    "intent_confidence": 1.0,
                    "slots": {},
                    "missing_slots": [],
                    "conflicts": [],
                },
                request=None,
                recommendations=[],
                answer=answer,
            )
            session.record_turn(
                Turn(
                    index=len(session.turns) + 1,
                    trace_id=event.trace_id,
                    user_id=event.user_id,
                    timestamp=event.timestamp,
                    source=event.source,
                    text=event.text,
                    intent="chitchat",
                    slots={},
                    constraints=[],
                    feedback=None,
                    item_ids=[],
                    status="ok",
                    note=answer,
                )
            )
            return ok(response)

        # 2.7) 开放问答：非推荐类问句交给常驻 LLM（加载一次、多轮复用）。
        # 只处理文字/语音；视觉与手势事件必须走各自的多模态链路。
        if self.chat is not None and event.source in ("text", "voice") and self._should_chat(event.text):
            chat_result = self.chat.chat(event.text or "")
            if chat_result.ok:
                chat_data = chat_result.data
                if isinstance(chat_data, dict):
                    answer = str(chat_data.get("text") or "").strip()
                else:
                    answer = str(chat_data or "").strip()
                status = "ok"
                extra: dict[str, Any] = {}
                if isinstance(chat_data, dict) and chat_data.get("web_search") is not None:
                    extra["web_search"] = chat_data["web_search"]
            else:
                answer = "抱歉，对话模型暂时不可用；推荐功能仍然可用，你可以直接说需求。"
                status = "degraded"
                extra = {"chat_error": str(chat_result.error)}
            response = self._envelope(
                event,
                session,
                timer,
                status=status,
                intent_result={
                    "intent": "chat",
                    "intent_confidence": 1.0,
                    "slots": {},
                    "missing_slots": [],
                    "conflicts": [],
                },
                request=None,
                recommendations=[],
                answer=answer,
                **extra,
            )
            session.record_turn(
                Turn(
                    index=len(session.turns) + 1,
                    trace_id=event.trace_id,
                    user_id=event.user_id,
                    timestamp=event.timestamp,
                    source=event.source,
                    text=event.text,
                    intent="chat",
                    slots={},
                    constraints=[],
                    feedback=None,
                    item_ids=[],
                    status=status,
                    note=answer,
                )
            )
            return ok(response)

        # 2.5) 「继续浏览」弱反馈：不走意图理解，直接记入会话偏好并重排
        if event.source == "browse":
            payload = self._handle_browse(event, session, timer)
            status = payload.pop("status", "ok")
            response = self._envelope(
                event,
                session,
                timer,
                status=status,
                intent_result={
                    "intent": "browse",
                    "intent_confidence": 1.0,
                    "slots": {},
                    "missing_slots": [],
                    "conflicts": [],
                },
                request=None,
                **payload,
            )
            session.record_turn(
                Turn(
                    index=len(session.turns) + 1,
                    trace_id=event.trace_id,
                    user_id=event.user_id,
                    timestamp=event.timestamp,
                    source=event.source,
                    text=None,
                    intent="browse",
                    slots={},
                    constraints=[],
                    feedback=None,
                    item_ids=[i["item_id"] for i in response.get("recommendations", [])],
                    status=status,
                    note=response.get("note", ""),
                )
            )
            return ok(response)

        # 3) 意图理解
        with timer.track("intent_ms"):
            intent_result = understand(
                event,
                catalog_categories=self.catalog.categories(),
                focus_item_id=session.focus_item_id,
                session_items=session.last_item_ids,
                title_lookup=self._title_lookup,
                top_k=top_k or settings.RANKING_CONFIG["top_k"],
                session_id=session_id,
                mode=self.mode,
            )
        if not intent_result.ok:
            return self._clarification_response(event, session, timer, intent_result, spec)

        request: RecommendationRequest = intent_result.data["request"]
        intent_dict = intent_result.data["intent_result"]
        merge_report = session.merge_slots(request.slots, request.constraints, source=f"turn:{intent_dict['intent']}")
        session.set_clarification(None)

        # 3.5) 会话合并后的有效请求：多轮约束（如先「推荐礼物」再「500以内」）在此生效
        effective = self._effective_request(request, session)

        # 4) 按意图分派
        handlers = {
            "feedback": self._handle_feedback,
            "compare": self._handle_compare,
            "explain": self._handle_explain,
        }
        handler = handlers.get(request.intent, self._handle_recommend)
        with timer.track("dispatch_ms"):
            payload = handler(event, effective, session, timer)

        status = payload.pop("status", "ok")
        response = self._envelope(
            event,
            session,
            timer,
            status=status,
            intent_result=intent_dict,
            request=request.to_dict(),
            effective_request=effective.to_dict(),
            merge_report=merge_report,
            **payload,
        )

        # 5) 记录本轮
        session.record_turn(
            Turn(
                index=len(session.turns) + 1,
                trace_id=event.trace_id,
                user_id=event.user_id,
                timestamp=event.timestamp,
                source=event.source,
                text=event.text,
                intent=request.intent,
                slots={k: v.value for k, v in request.slots.items()},
                constraints=[c.to_dict() for c in request.constraints],
                item_ids=[i["item_id"] for i in response.get("recommendations", [])],
                feedback=payload.get("feedback"),
                status=status,
                note=response.get("note", ""),
            )
        )
        return ok(response)

    # ============================================================
    # 处理器：推荐主链路
    # ============================================================
    def _handle_recommend(self, event, request: RecommendationRequest, session: SessionContext, timer: Timer) -> dict:
        with timer.track("recall_ms"):
            recall_result = recall(request, self.recommender, self.catalog, session)
        if not recall_result.ok or not recall_result.data["candidates"]:
            error = (
                recall_result.error.to_dict()
                if recall_result.error
                else {"code": ErrorCode.NO_RESULT.value, "message": "所有召回来源均为空", "field": None, "context": {}}
            )
            return {
                "status": "empty",
                "recommendations": [],
                "recall_trace": recall_result.data["trace"] if recall_result.ok else None,
                "error": error,
                "note": "没有任何候选来源返回结果",
            }

        with timer.track("rank_ms"):
            rank_result = rank(recall_result.data["candidates"], request, self.catalog, session)
        if not rank_result.ok:
            return {
                "status": "empty",
                "recommendations": [],
                "recall_trace": recall_result.data["trace"],
                "ranking_trace": None,
                "degraded": True,
                "error": rank_result.error.to_dict() if rank_result.error else None,
                "note": rank_result.error.message if rank_result.error else "过滤后无结果",
            }

        ranking_trace = rank_result.data["trace"]
        recommendations, explanations = self._materialize(rank_result.data["ranked"], request, session, event)

        with timer.track("present_ms"):
            header = self._header(request)
            if ranking_trace.get("degraded"):
                header = "（当前条件下没有完全匹配的商品，以下为热门兜底）" + header
            presentation = present_items(
                recommendations, header=header, with_voice=self.with_voice, dry_run=self.dry_run_voice
            )

        degraded = bool(ranking_trace.get("degraded"))
        if degraded:
            for item in recommendations:
                item["degraded"] = True
                item["evidence"]["constraint_satisfied"] = False
            ranking_trace["notice"] = settings.RANKING_CONFIG["empty_result_hint"]
        else:
            for item in recommendations:
                item["evidence"]["constraint_satisfied"] = True

        session.add_recommendation(
            [r["item_id"] for r in recommendations], focus=recommendations[0]["item_id"] if recommendations else None
        )
        status = "degraded" if degraded else "ok"
        return {
            "status": status,
            "recommendations": recommendations,
            "explanations": explanations,
            "presentation": presentation,
            "recall_trace": recall_result.data["trace"],
            "ranking_trace": ranking_trace,
            "note": ranking_trace.get("degrade_reason") or "",
        }

    # ============================================================
    # 处理器：反馈
    # ============================================================
    def _handle_browse(self, event, session: SessionContext, timer: Timer) -> dict:
        """处理「继续浏览」：写入会话偏好（弱正反馈），然后重排。

        与点赞的区别：浏览不表达喜好，不改变正负反馈状态，也不屏蔽任何商品，
        只按 config 中的弱权重轻微上浮（阶段5 任务目标要求把浏览类反馈纳入会话偏好）。
        """
        item_id = str((event.raw or {}).get("item_id") or "")
        if not item_id or not self.catalog.exists(item_id):
            return {
                "status": "error",
                "recommendations": [],
                "error": _err(
                    ErrorCode.ITEM_NOT_FOUND, f"商品 {item_id or '(空)'} 不在目录中，无法记录浏览", "item_id"
                ),
            }
        category = self.catalog.category_of(item_id)
        with timer.track("browse_ms"):
            viewed = session.add_view(item_id, category=category)
        if not viewed.ok:
            assert viewed.error is not None
            return {"status": "error", "recommendations": [], "error": viewed.error.to_dict()}

        # 复用推荐主链路做重排，让「浏览」的影响可见（与点赞一致的口径）
        request = RecommendationRequest(
            trace_id=event.trace_id,
            user_id=event.user_id,
            timestamp=event.timestamp,
            source=event.source,
            intent="discover",
            slots=dict(session.slot_state),
            constraints=list(session.constraints),
            top_k=settings.RANKING_CONFIG["top_k"],
            session_id=session.session_id,
            mode=self.mode,
        )
        payload = self._handle_recommend(event, request, session, timer)
        affected = next((i for i in payload.get("recommendations", []) if i["item_id"] == item_id), None)
        payload["browse"] = {
            **viewed.data,
            "title": self.catalog.title(item_id),
            "preference": session.preference_signal(item_id, category),
            "rank_after_view": (affected or {}).get("rank"),
        }
        payload["note"] = f"已记录浏览：{self.catalog.title(item_id)}（弱反馈，仅轻微上浮）"
        return payload

    def _handle_feedback(self, event, request: RecommendationRequest, session: SessionContext, timer: Timer) -> dict:
        target = (
            request.slot_value("feedback_target")
            or request.slot_value("target")
            or session.focus_item_id
            or (session.last_item_ids[0] if session.last_item_ids else None)
        )
        polarity = request.slot_value("polarity")
        if not target:
            session.set_clarification(CLARIFY_FEEDBACK)
            return {
                "status": "clarification",
                "recommendations": [],
                "clarification": CLARIFY_FEEDBACK,
                "error": _err(ErrorCode.MISSING_SLOT, CLARIFY_FEEDBACK, "feedback_target"),
            }
        if not self.catalog.exists(str(target)):
            return {
                "status": "error",
                "recommendations": [],
                "error": _err(ErrorCode.ITEM_NOT_FOUND, f"商品 {target} 不在目录中，无法写入反馈", "feedback_target"),
            }

        before_ids = list(session.last_item_ids)
        category = self.catalog.category_of(str(target))
        with timer.track("feedback_ms"):
            feedback_result = session.add_feedback(str(target), str(polarity), category=category)
        if not feedback_result.ok:
            assert feedback_result.error is not None
            return {"status": "error", "recommendations": [], "error": feedback_result.error.to_dict()}
        session.pending_clarification = None

        payload = self._handle_recommend(event, request, session, timer)
        after_ids = [r["item_id"] for r in payload.get("recommendations", [])]
        top_n = settings.FEEDBACK_CONFIG["ab"]["top_k"]
        payload["feedback"] = {**feedback_result.data, "target_title": self.catalog.title(str(target))}
        payload["before_after"] = {
            "before_topk": before_ids[:top_n],
            "after_topk": after_ids[:top_n],
            "changed": before_ids[:top_n] != after_ids[:top_n],
        }
        payload["note"] = f"已记录{'点赞' if polarity == 'positive' else '倒赞'}：{self.catalog.title(str(target))}"
        return payload

    # ============================================================
    # 处理器：对比
    # ============================================================
    def _handle_compare(self, event, request: RecommendationRequest, session: SessionContext, timer: Timer) -> dict:
        resolved = [str(t) for t in (request.slot_value("targets") or []) if self.catalog.exists(str(t))]
        if len(resolved) < 2:
            session.set_clarification(CLARIFY_COMPARE)
            return {
                "status": "clarification",
                "recommendations": [],
                "clarification": CLARIFY_COMPARE,
                "error": _err(ErrorCode.MISSING_SLOT, CLARIFY_COMPARE, "targets", resolved=resolved),
            }
        with timer.track("compare_ms"):
            items = [self._item_payload(t, sources=["用户指定对比"], matched=[]) for t in resolved[:4]]
            fields = ("category", "price", "currency")
            differences: list[dict] = []
            for field in fields:
                values = {i["item_id"]: i.get(field) for i in items}
                if len({str(v) for v in values.values()}) > 1:
                    differences.append({"field": field, "values": values})
            for item in items:
                pack_result = build_pack(
                    item,
                    request=request,
                    catalog=self.catalog,
                    session=session,
                    visual_tags=event.visual_tags,
                    kb=self.kb,
                    sources=item["sources"],
                    matched_constraints=item["matched_constraints"],
                )
                if pack_result.ok:
                    explanation = explain(item, pack_result.data)
                    if explanation.ok:
                        item["explanation"] = explanation.data["text"]
                        item["explanation_confidence"] = explanation.data["confidence"]

        with timer.track("present_ms"):
            presentation = present_items(
                items, header="对比结果：", with_voice=self.with_voice, dry_run=self.dry_run_voice
            )
        session.add_recommendation([i["item_id"] for i in items], focus=items[0]["item_id"])
        return {
            "status": "ok",
            "recommendations": items,
            "comparison": {"targets": [i["item_id"] for i in items], "differences": differences},
            "presentation": presentation,
            "note": f"已对比 {len(items)} 件商品",
        }

    # ============================================================
    # 处理器：解释
    # ============================================================
    def _handle_explain(self, event, request: RecommendationRequest, session: SessionContext, timer: Timer) -> dict:
        target = request.slot_value("target") or session.focus_item_id
        if not target or not self.catalog.exists(str(target)):
            session.set_clarification(CLARIFY_NO_TARGET)
            return {
                "status": "clarification",
                "recommendations": [],
                "clarification": CLARIFY_NO_TARGET,
                "error": _err(ErrorCode.MISSING_SLOT, CLARIFY_NO_TARGET, "target"),
            }
        item = self._item_payload(str(target), sources=["用户指定商品"], matched=[])
        with timer.track("explain_ms"):
            pack_result = build_pack(
                item,
                request=request,
                catalog=self.catalog,
                session=session,
                visual_tags=event.visual_tags,
                kb=self.kb,
                sources=item["sources"],
                matched_constraints=item["matched_constraints"],
            )
            if not pack_result.ok:
                message = pack_result.error.message if pack_result.error else "证据构建失败"
                return {
                    "status": "no_evidence",
                    "recommendations": [],
                    "error": pack_result.error.to_dict() if pack_result.error else None,
                    "note": message,
                }
            explanation = explain(item, pack_result.data)
            if explanation.ok:
                polished = self.llm_explainer.explain(item, pack_result.data, template_text=explanation.data["text"])
                if polished.ok:
                    explanation.data["template_text"] = explanation.data["text"]
                    explanation.data["text"] = polished.data["text"]
                    explanation.data["llm"] = polished.data
        if not explanation.ok:
            message = (
                explanation.error.message if explanation.error else settings.EXPLANATION_CONFIG["no_evidence_text"]
            )
            return {
                "status": "no_evidence",
                "recommendations": [item],
                "error": explanation.error.to_dict() if explanation.error else None,
                "presentation": present_items([item], header="解释：", with_voice=False),
                "note": message,
            }
        item["explanation"] = explanation.data["text"]
        item["explanation_source"] = (explanation.data.get("llm") or {}).get("source")
        item["explanation_confidence"] = explanation.data["confidence"]
        with timer.track("present_ms"):
            presentation = present_items(
                [item], header="解释：", with_voice=self.with_voice, dry_run=self.dry_run_voice
            )
        session.add_recommendation([item["item_id"]], focus=item["item_id"])
        return {
            "status": "ok",
            "recommendations": [item],
            "explanations": [explanation.data],
            "presentation": presentation,
            "note": "已生成检索增强解释",
        }

    # ============================================================
    # 辅助
    # ============================================================
    def _materialize(self, ranked, request: RecommendationRequest, session: SessionContext, event):
        recommendations: list[dict] = []
        explanations: list[dict] = []
        # 先收集需要 LLM 改写的条目，最后一次性批量调用：
        # 旧实现是每件商品起一个 worker，8 件商品会重复加载 8 次 4B 权重。
        pending: list[tuple[dict, Any, dict]] = []
        for scored in ranked:
            product = self.catalog.product(scored.item_id) or {}
            item = RecommendationItem(
                trace_id=request.trace_id,
                user_id=request.user_id,
                timestamp=now_iso(),
                source=request.source,
                item_id=scored.item_id,
                title=product.get("title", ""),
                category=product.get("category", ""),
                price=product.get("price"),
                currency=product.get("currency", self.catalog.currency),
                score=scored.final_score,
                rank=scored.rank,
                sources=list(scored.sources),
                evidence={
                    "score_breakdown": scored.to_dict(self.catalog)["score_breakdown"],
                    "matched_constraints": scored.matched_constraints,
                    "source_detail": scored.evidence.get("source_detail", {}),
                },
            ).to_dict()

            if scored.rank <= EXPLAIN_TOP_N:
                pack_result = build_pack(
                    item,
                    request=request,
                    catalog=self.catalog,
                    session=session,
                    visual_tags=event.visual_tags,
                    kb=self.kb,
                    sources=scored.sources,
                    matched_constraints=scored.matched_constraints,
                )
                if pack_result.ok:
                    explanation = explain(item, pack_result.data)
                    if explanation.ok:
                        pending.append((item, pack_result.data, explanation.data))
                    else:
                        item["reason"] = explanation.error.message if explanation.error else ""
                else:
                    item["reason"] = pack_result.error.message if pack_result.error else ""
            recommendations.append(item)

        if pending:
            polished_results = self.llm_explainer.explain_many(
                [(item, pack, explanation_data["text"]) for item, pack, explanation_data in pending]
            )
            for (item, pack, explanation_data), polished in zip(pending, polished_results, strict=False):
                final = (
                    polished.data
                    if polished.ok
                    else {
                        "text": explanation_data["text"],
                        "source": "template",
                        "grounded": True,
                        "fallback_reason": "LLM 解释器异常",
                    }
                )
                item["explanation"] = final["text"]
                item["explanation_source"] = final.get("source")
                item["explanation_grounded"] = final.get("grounded")
                if final.get("llm_rejected"):
                    item["llm_rejected"] = True
                item["explanation_confidence"] = explanation_data["confidence"]
                item["evidence_count"] = explanation_data["evidence_count"]
                explanations.append({**explanation_data, "llm": final, "evidence_pack": pack.to_dict()})
        return recommendations, explanations

    def _effective_request(self, request: RecommendationRequest, session: SessionContext) -> RecommendationRequest:
        """把当前轮槽位与会话历史槽位合并，得到真正用于召回/排序的请求。

        多轮场景：第 1 轮「推荐一些礼物」→ 第 2 轮「500 以内」，
        第 2 轮生效的约束是「礼物 + 500 以内」，而不是只有预算。
        """
        slots = dict(session.slot_state)
        constraints = list(session.constraints)
        hard = [c for c in constraints if c.kind == "hard"]
        intent = request.intent
        if intent not in ("discover", "similar", "filter"):
            intent = "filter" if hard else "discover"
        return RecommendationRequest(
            trace_id=request.trace_id,
            user_id=request.user_id,
            timestamp=request.timestamp,
            source=request.source,
            intent=intent,
            slots=slots,
            constraints=constraints,
            top_k=request.top_k,
            session_id=request.session_id,
            exclude_item_ids=list(request.exclude_item_ids),
            mode=request.mode,
            meta={"derived_from_intent": request.intent},
        )

    def _item_payload(self, item_id: str, *, sources: list[str], matched: list[dict]) -> dict:
        product = self.catalog.product(item_id) or {}
        return {
            "item_id": item_id,
            "title": product.get("title", ""),
            "category": product.get("category", ""),
            "price": product.get("price"),
            "currency": product.get("currency", self.catalog.currency),
            "sources": list(sources),
            "matched_constraints": list(matched),
        }

    def _title_lookup(self):
        for item_id, product in self.catalog.catalog.items():
            title = product.get("title") or ""
            if title:
                yield item_id, title

    def _header(self, request: RecommendationRequest) -> str:
        if request.intent == "similar":
            target = request.slot_value("target")
            title = self.catalog.title(str(target)) if target else ""
            return f"与「{title}」相似的商品："
        category = request.slot_value("category")
        scene = request.slot_value("scene")
        parts = [p for p in (f"{scene}场景" if scene else None, category) if p]
        prefix = "".join(parts)
        if request.intent == "filter":
            return f"按你的条件（{prefix or '硬约束'}）筛选结果："
        return f"{prefix}推荐：" if prefix else "为你推荐："

    @staticmethod
    def _should_chat(text: str | None) -> bool:
        """开放问句 → 对话；购物/推荐需求 → 推荐链路。"""
        cfg = settings.CHAT_CONFIG
        if not text:
            return False
        lowered = str(text).lower()
        if not any(marker in lowered for marker in cfg["question_markers"]):
            return False
        # 技术追问优先：「基于内容的推荐有什么区别？」「推荐系统为什么需要召回？」
        # 里的「推荐」是名词，不是购物指令。
        if any(marker in lowered for marker in cfg.get("tech_markers", [])):
            return True
        if any(marker in lowered for marker in cfg["block_markers"]):
            return False
        signals = set(LEX["category_synonyms"]) | set(LEX["scenes"]) | set(LEX["colors"])
        if any(signal and str(signal).lower() in lowered for signal in signals):
            return False
        return re.search(r"\d", lowered) is None

    @staticmethod
    def _smalltalk(text: str | None) -> str | None:
        """寒暄 / 身份问答的精确匹配；正常需求（即使以「你好」开头）不受影响。"""
        if not text:
            return None
        compact = re.sub(r"[\s\W_]+", "", str(text).lower())
        if not compact:
            return None
        for kind, phrases in settings.SMALLTALK.items():
            for phrase in phrases:
                target = re.sub(r"[\s\W_]+", "", phrase.lower())
                if compact == target or (compact.startswith(target) and len(compact) <= len(target) + 2):
                    return settings.SMALLTALK_REPLIES.get(kind)
        return None

    def _handle_control(self, text: str, session: SessionContext, session_id: str, user_id: str) -> dict | None:
        if not text:
            return None
        if contains_any(text, LEX["undo_markers"]):
            result = session.undo()
            if result.ok:
                return {"action": "undo", **result.data}
            return {"action": "undo", "error": result.error.to_dict() if result.error else None}
        if contains_any(text, LEX["reset_markers"]):
            summary = session.end()
            new_id = stable_key(session_id, "reset", now_iso())
            self.store.get_or_create(new_id, user_id)
            return {"action": "reset", "ended_session": summary, "new_session_id": new_id}
        return None

    # ============================================================
    # 响应封装
    # ============================================================
    def _clarification_response(
        self, event, session: SessionContext, timer: Timer, result: Result, spec: dict
    ) -> Result:
        error = result.error
        # AppError.code 声明为 ErrorCode | str（__post_init__ 已归一，这里做兜底取值）
        code = ErrorCode.INTERNAL_ERROR.value
        context: dict = {}
        if error is not None:
            code = error.code.value if isinstance(error.code, ErrorCode) else str(error.code)
            context = error.context or {}
        status = "conflict" if code == ErrorCode.CONSTRAINT_CONFLICT.value else "clarification"
        question = context.get("clarification") or (error.message if error else "")
        session.set_clarification(question)
        response = self._envelope(
            event,
            session,
            timer,
            status=status,
            intent_result=context.get("intent_result"),
            request=None,
            recommendations=[],
            clarification=question,
            error=error.to_dict() if error else None,
            note="约束冲突，等待用户确认" if status == "conflict" else "需要用户补充信息",
        )
        session.record_turn(
            Turn(
                index=len(session.turns) + 1,
                trace_id=event.trace_id,
                user_id=event.user_id,
                timestamp=event.timestamp,
                source=event.source,
                text=event.text,
                intent=(context.get("intent_result") or {}).get("intent"),
                slots={},
                constraints=[],
                item_ids=[],
                status=status,
                note=response["note"],
            )
        )
        return ok(response)

    def _error_response(self, result: Result, session: SessionContext, timer: Timer, *, source: str) -> Result:
        error = result.error
        return ok(
            {
                "schema_version": "1.0.0",
                "trace_id": f"tr-error-{stable_hash_int(session.session_id, source):012d}",
                "user_id": session.user_id,
                "timestamp": now_iso(),
                "source": source,
                "session_id": session.session_id,
                "status": "error",
                "recommendations": [],
                "error": error.to_dict() if error else None,
                "trace": {"latency_ms": timer.as_dict(), "modes": self.modes(), "degradations": self.degradations()},
                "session": session.snapshot(),
            }
        )

    def _envelope(self, event, session: SessionContext, timer: Timer, *, status: str, **payload) -> dict:
        error = payload.pop("error", None)
        presentation = payload.get("presentation") or {}
        degradations = self.degradations()
        if presentation.get("degradations"):
            degradations["presentation"] = {
                "adapter": "presentation",
                "degraded": True,
                "events": presentation["degradations"],
            }
        trace = {
            "latency_ms": timer.as_dict(),
            "modes": self.modes(),
            "degradations": degradations,
        }
        for key, value in list(payload.items()):
            if key.endswith("_trace"):
                trace[key[:-6]] = value
        return {
            "schema_version": "1.0.0",
            "trace_id": event.trace_id,
            "user_id": event.user_id,
            "timestamp": event.timestamp,
            "source": event.source,
            "session_id": session.session_id,
            "status": status,
            "event": event.to_dict(),
            "recommendations": payload.pop("recommendations", []),
            "error": error,
            "trace": trace,
            "session": session.snapshot(),
            **payload,
        }

    def modes(self) -> dict:
        chat_report = self.chat.report() if self.chat is not None and hasattr(self.chat, "report") else {}
        return {
            "runtime_mode": self.mode,
            "multimodal": self.multimodal.active_mode,
            "recommender": self.recommender.active_mode,
            "catalog_origin": self.catalog.origin,
            "catalog_products": self.catalog.size(),
            "llm_explain": "real" if self.llm_explainer.available() else "template",
            "llm_provider": chat_report.get("last_provider") or chat_report.get("mode") or "off",
        }

    def degradations(self) -> dict:
        return {
            "multimodal": self.multimodal.degradation_report(),
            "recommender": self.recommender.degradation_report(),
        }

    def llm_report(self) -> dict:
        report = self.llm_explainer.report()
        if self.chat is not None and hasattr(self.chat, "report"):
            report["chat"] = self.chat.report()
        return report

    def adapter_report(self) -> dict:
        return {
            "multimodal": {**self.multimodal.health().data, "calls": self.multimodal.call_report()},
            "recommender": {**self.recommender.health().data, "calls": self.recommender.call_report()},
        }


def _err(code: ErrorCode, message: str, field: str | None = None, **context: Any) -> dict:
    from .errors import AppError

    return AppError(code, message, field, context).to_dict()
