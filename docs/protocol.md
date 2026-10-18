# 接口契约（Protocol）

> 阶段1 交付物 · 统一事件与请求/响应 Schema、错误码、适配器边界

## 1. 契约版本

`SCHEMA_VERSION = "1.0.0"`，定义在 `src/contracts.py`。
版本不一致时 `from_dict()` 返回 `E_SCHEMA_VERSION_MISMATCH`。

## 2. 公共信封（所有结构共用）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `trace_id` | string | 是 | 非空字符串，全链路唯一 |
| `user_id` | string | 是 | 非空字符串 |
| `timestamp` | string | 是 | ISO8601（带时区），非法返回 `E_INVALID_TIMESTAMP` |
| `source` | string | 是 | `voice`/`vision`/`gesture`/`text`/`feedback`/`system`，或 `source:detail` |
| `schema_version` | string | 否 | 默认 `1.0.0` |

## 3. InteractionEvent（多模态交互事件）

```json
{
  "schema_version": "1.0.0",
  "trace_id": "tr-1ce83e38da02433a",
  "user_id": "15547",
  "timestamp": "2026-10-13T09:08:25+00:00",
  "source": "voice",
  "modality": "asr",
  "text": "我要给朋友挑一个结婚礼物，500以内",
  "visual_tags": [
    {"tag": "白色桌面", "confidence": 0.9, "source": "vl",
     "raw": {"low_confidence": false, "weight": 1.0, "effective_confidence": 0.9}}
  ],
  "gesture": {"name": "thumbs_up", "confidence": 0.92, "polarity": "positive", "is_feedback": true},
  "session_id": "sess-15547|2026-10-13"
}
```

`modality ∈ {asr, visual, gesture, text, feedback, none}`。

**视觉标签治理口径**（`config.MULTIMODAL_ADAPTER.vision`）：

| 原始置信度 | 处置 | `weight` | 有效置信度 |
|---|---|---|---|
| < 0.35 | 丢弃 | — | — |
| 0.35 – 0.55 | 降权保留 | 0.5 | `confidence × 0.5` |
| ≥ 0.55 | 正常使用 | 1.0 | `confidence` |

**文本治理口径**（`config.INPUT_NORM`）：`text` 字段进入意图解析前会做一次**等价改写**。

`text` 是**归一化之后**的文本；原文始终保留在 `raw.spec.text`，处置明细写在 `raw.text_norm`（未触发时为缺省，不出现该字段）。

| 处置 | 触发条件 | 举例 |
|---|---|---|
| `traditional_to_simplified` | 检出繁体字且装有 `zhconv` / `opencc` | `300以內的水杯` → `300以内的水杯` |
| `fullwidth_to_halfwidth` | 出现全角数字或拉丁字母 | `３００以内` → `300以内` |
| `typo_corrected` | 方向词紧邻数量词但写错 | `预算是内300` → `预算以内300` |
| `collapse_split_digits` | 数字被空格逐位拆开 | `3 0 0 以内` → `300 以内` |

原则：**只做等价改写** —— 不删除英文词、不截断文本。那是生成侧 `normalize_fluency` 的口径
（用于 LLM 输出），用在用户输入上会把 `iPhone 手机壳` 这类真实信息删掉。因此输入侧对
`iPhone 手机壳 300以内` 原样保留。归一化只作用于 `text`；`visual_tags` / `gesture` 不受影响。

中文数字预算不在此改写（改写任意中文数字会误伤「十分好看」）：它在 `src/intent.py` 的预算解析里按
「数量词紧邻方向词或计量单位」的形态识别，`三百以内` → 300、`一百五左右` → 150±15%。

## 4. RecommendationRequest（结构化推荐请求）

```json
{
  "trace_id": "tr-...", "user_id": "15547", "timestamp": "2026-10-13T09:08:25+00:00",
  "source": "voice",
  "intent": "filter",
  "slots": {
    "category":  {"name": "category",  "value": "装饰品", "confidence": 0.75, "evidence": ["文本命中类目词「礼物」"]},
    "budget_max":{"name": "budget_max","value": 500.0,   "confidence": 0.85, "evidence": ["文本预算表达「以内500」"]}
  },
  "constraints": [
    {"field": "price", "op": "lte", "value": 500.0, "kind": "hard", "source": "slot:budget_max"},
    {"field": "category", "op": "eq", "value": "装饰品", "kind": "soft", "source": "slot:category"}
  ],
  "top_k": 8, "session_id": "sess-...", "exclude_item_ids": [], "mode": "offline"
}
```

**六类意图与必选槽位**

| intent | 中文 | 必选槽位 | 缺失时行为 |
|---|---|---|---|
| `discover` | 发现/随便看看 | — | — |
| `similar` | 找相似 | `target` | `E_MISSING_SLOT` + 澄清问题 |
| `filter` | 带硬约束筛选 | 至少 1 个硬约束 | `E_MISSING_SLOT` |
| `compare` | 对比 | `targets` ≥ 2 | `E_MISSING_SLOT` |
| `feedback` | 反馈 | `polarity` | `E_MISSING_SLOT` |
| `explain` | 求解释 | `target` | `E_MISSING_SLOT` |

**约束语义**：`kind=hard` 的约束先过滤，**排序分数不得覆盖硬约束**；
`kind=soft` 的约束参与打分加权。

## 5. RecommendationItem（推荐结果）

```json
{
  "trace_id": "tr-...", "user_id": "15547", "timestamp": "...", "source": "voice",
  "item_id": "20829", "title": "闪粉悬挂蝴蝶串", "category": "装饰品",
  "price": 2.1, "currency": "GBP",
  "score": 0.7231, "rank": 1,
  "sources": ["推荐系统 v146 个性化精排", "会话关注召回"],
  "evidence": {
    "score_breakdown": {"base_score": 0.88, "constraint_match": 1.0, "source_trust": 1.0, "session_preference": 0.5},
    "matched_constraints": [{"field": "price", "op": "lte", "required": 500.0, "actual": 2.1, "verifiable": true}],
    "constraint_satisfied": true
  },
  "reason": ""
}
```

## 6. EvidencePack（证据包）

解释器**唯一**允许读取事实的来源。

| 字段 | 说明 |
|---|---|
| `product_facts` | 商品目录事实（item_id/title/category/price/currency/price_source/origin） |
| `candidate_sources` | 该候选来自哪些召回通道（可追溯） |
| `matched_constraints` | 命中的约束及实际值（`verifiable=true`） |
| `visual_tags` | 通过有效置信度筛选的视觉标签 |
| `evidence_snippets` | RAG 检索片段（含 chunk_id 与 score） |
| `session_signals` | 会话偏好信号 |
| `consistency` | 价格/属性一致性核验结果 |

**实质性证据类型**：`product_fact`、`matched_constraint`、`kb_snippet`、`visual_tag`、`session_signal`。
`candidate_source` 仅为出处标注，不能单独支撑一条推荐解释。

## 7. 统一响应信封

```json
{
  "schema_version": "1.0.0",
  "trace_id": "tr-...", "user_id": "15547", "timestamp": "...", "source": "voice",
  "session_id": "sess-...",
  "status": "ok | degraded | clarification | conflict | empty | no_evidence | error",
  "event": { "...InteractionEvent..." },
  "intent_result": { "intent": "filter", "intent_confidence": 1.0, "slots": {}, "missing_slots": [], "conflicts": [] },
  "request": { "...RecommendationRequest..." },
  "effective_request": { "...会话合并后的实际请求..." },
  "recommendations": [ { "...RecommendationItem..." } ],
  "error": null,
  "trace": {
    "latency_ms": {"event_ms": 0.05, "intent_ms": 0.9, "recall_ms": 2.2, "rank_ms": 0.4, "present_ms": 0.03, "total_ms": 4.6},
    "modes": {"runtime_mode": "offline", "multimodal": "offline_stub", "recommender": "offline_snapshot"},
    "degradations": {"multimodal": {...}, "recommender": {...}},
    "recall": {"sources": [...], "merged_unique": 298, "kept": 120},
    "ranking": {"input_candidates": 120, "after_hard_filter": 4, "hard_dropped": 116, "degraded": false}
  },
  "session": { "...SessionContext 快照..." }
}
```

> `answer`（可选）：寒暄 / 身份问答的直接回答。此时 `recommendations` 为空、
> `intent_result.intent="chitchat"`，前端应展示 `answer` 而不是「找到 N 件商品」。
>
> `intent_result.intent="chat"` 时同样是 `answer` + 空 `recommendations`，
> 表示**开放问答**（由常驻 LLM 生成，保留多轮上下文）。模型不可用时
> `status="degraded"`，`answer` 为固定降级提示，并附带 `chat_error`；
> 前端展示方式与 `chitchat` 相同。

## 8. 错误码表（`src/errors.py:ErrorCode`）

| 分组 | 错误码 | 含义 |
|---|---|---|
| 契约 | `E_INVALID_INPUT` | 输入不合法 |
| 契约 | `E_MISSING_FIELD` | 缺少必填字段（`context.problems` 给出全部字段） |
| 契约 | `E_TYPE_MISMATCH` | 字段类型不匹配 |
| 契约 | `E_INVALID_TIMESTAMP` | 时间戳非 ISO8601 |
| 契约 | `E_INVALID_SOURCE` | source 取值非法 |
| 契约 | `E_SCHEMA_VERSION_MISMATCH` | 契约版本不兼容 |
| 意图/会话 | `E_UNKNOWN_INTENT` / `E_MISSING_SLOT` / `E_CONSTRAINT_CONFLICT` | 意图与槽位问题 |
| 意图/会话 | `E_SESSION_NOT_FOUND` / `E_NOTHING_TO_UNDO` | 会话状态问题 |
| 多模态 | `E_EMPTY_TRANSCRIPT` / `E_MODALITY_UNAVAILABLE` / `E_LOW_CONFIDENCE_MODALITY` | 模态问题 |
| 推荐 | `E_UPSTREAM_UNAVAILABLE` / `E_TIMEOUT` / `E_NO_RESULT` / `E_HARD_CONSTRAINT_EMPTY` | 上游与结果问题 |
| 证据 | `E_NO_EVIDENCE` / `E_EVIDENCE_INCONSISTENT` / `E_ITEM_NOT_FOUND` | 证据与商品问题 |
| 反馈 | `E_INVALID_POLARITY` / `E_FEEDBACK_CONFLICT` | 反馈问题 |
| 兜底 | `E_INTERNAL_ERROR` / `E_DEGRADED` | 内部与降级 |

**约定**：任何失败都必须返回带 `code` + `message` + `field`（可能为空）的结构化错误，
禁止「捕获异常后返回空字典」。

## 9. 适配器接口

### 9.1 MultimodalAdapter（对「智能多模态AI助手」）

| 方法 | 说明 |
|---|---|
| `parse_event(spec, user_id, session_id)` | 把模态输入解析为标准 `InteractionEvent`，执行文本归一化（见 §3）与置信度治理 |
| `transcribe(audio_path)` | real 模式调用 ASR；offline 返回 `E_MODALITY_UNAVAILABLE` |
| `describe_image(image_path, question)` | real 模式调用 VL |
| `recognize_gesture(landmarks, face)` | real 模式调用手势识别 |
| `health()` / `degradation_report()` / `call_report()` | 健康状态与调用统计 |

### 9.2 RecommenderAdapter（对「推荐系统 v146」）

策略顺序（`config.RECOMMENDER_ADAPTER.strategy_order`）：`http → local_artifacts → offline_snapshot`

| 方法 | 说明 |
|---|---|
| `personalized_candidates(user_id, k)` | 个性化精排候选（v146 分数） |
| `similar_candidates(seed_ids, k, exclude)` | 相似商品召回 |
| `category_candidates(category, k)` | 品类热门召回 |
| `history_candidates(user_id, k)` | 基于历史购买的相似召回 |
| `popular_candidates(k)` | 全局热门兜底 |
| `product_detail(item_id)` | 商品详情（含相似商品） |

**边界约束**：
- HTTP 模式的演示账号/口令**只从环境变量读取**（`INTEGRATED_DEMO_USER` / `INTEGRATED_DEMO_PASSWORD`），代码与数据文件中不含任何密钥；
- `local_artifacts` 模式只读取推荐系统**对外发布的制品文件**（`webapp/data/*.json`、`products.csv`、`submission_*.csv`），
  不 import 其 Python 模块、不访问其内部变量。

## 10. 网页 API（webui.py，本地演示边界）

`webui.py` 是浏览器与 Orchestrator 之间的本地边界。它**不是**对外服务：默认只监听
`127.0.0.1`，无鉴权、无限流，仅用于课程演示。

### 10.1 通用约定

- 所有响应都是 JSON（`Content-Type: application/json; charset=utf-8`）。
- 业务成功：`{"ok": true, ...}`。
- 业务失败：`{"ok": false, "error": "<人类可读>", "code": "E_...", "field": "<字段或 null>"}`。
- 未知路径 / 未知接口：HTTP 404，但**仍返回上述结构**（`code=E_INVALID_INPUT`）。
- `code` 取值来自 `src/errors.py:ErrorCode`（见 §8）。

### 10.2 端点

| 方法 | 路径 | 请求 | 成功响应 | 失败响应 |
|---|---|---|---|---|
| GET | `/api/info` | — | `{ok, user_id, session_id, modes, account, accounts[], voice, llm}` | — |
| GET | `/api/home` | `?limit=8\|16\|24\|40`（可选，默认 8） | `{ok, title, subtitle, items[]}` | — |
| GET | `/api/img/{item_id}` | — | 图片字节 | 404 `E_ITEM_NOT_FOUND` |
| GET | `/api/product/{item_id}` | — | `{ok, product, similar[]}` | `{ok:false, code:E_ITEM_NOT_FOUND, ...}` |
| GET | `/audio/{name}` | — | 音频字节 | 404 `E_ITEM_NOT_FOUND` |
| POST | `/api/chat` | `{text, top_k?}` | `{ok, response}`（response 见 §7） | `{ok:false, code, field, error}` |
| POST | `/api/more` | `{text, top_k}`（`top_k` 上限 120） | `{ok, items[], top_k}` | 同上 |
| POST | `/api/feedback` | `{item_id, polarity}` | `{ok, response}` | 同上 |
| POST | `/api/voice` | `{seconds}` | `{ok, transcript, response}` | 同上 |
| POST | `/api/gesture` | `{item_id?}` | `{ok, gesture, response, preview_url, gesture_frames, gesture_votes}`；非反馈手势返回 `{ok, gesture, action, message, preview_url}` | 同上（失败也带 `preview_url`） |
| GET | `/capture/{path}` | — | 抓拍画面字节 | 404 `E_ITEM_NOT_FOUND` |
| GET | `/camera/stream` | — | MJPEG 实时预览（multipart/x-mixed-replace） | 503 `E_MODALITY_UNAVAILABLE` |
| GET | `/api/camera/state` | — | `{ok, state}`（running/current_gesture/event_id/fps） | — |
| POST | `/api/camera/start` | `{}` | `{ok, state}` | `{ok:false, code, error}` |
| POST | `/api/camera/stop` | `{}` | `{ok, state}` | — |
| POST | `/api/vision` | `{question}` | `{ok, description, response}` | 同上 |
| POST | `/api/undo` / `/api/reset` | `{}` | `{ok, response}` | 同上 |
| POST | `/api/products` | `{item_ids[]}` | `{ok, products[]}` | 400 `E_INVALID_INPUT` |
| POST | `/api/user` | `{user_id}` | `{ok, account, session_id}` | `{ok:false, code:E_INVALID_INPUT, field:user_id}` |
| POST | `/api/state` | `{}` | `{ok, state}` | — |
| POST | `/api/llm` | `{}` | `{ok, enabled}` | — |

### 10.3 与 §7 的关系

`/api/chat` 等返回的 `response` 就是 §7 的统一响应信封；`ok` 只表示**本次 HTTP 调用完成**，
不代表业务成功 —— 业务状态看 `response.status`（`ok/degraded/.../error`）。
前端必须同时检查 `ok` 与 `response.status`（本仓库的 `rate()` 曾因只看 `ok` 而误报「已记录」）。

### 10.4 已知边界

- 单进程、无鉴权、无限流；只监听 `127.0.0.1`，禁止直接暴露到公网。
- 同一时刻只处理一个会修改会话的请求（`App.lock`）；长耗时请求（真实 LLM）会阻塞其他会话写操作。
- 错误响应对本地演示保留异常文本；生产部署应改为通用文案 + 服务端日志。

