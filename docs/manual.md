# 多模态个性化推荐助手 · 项目说明与使用手册

> 由「智能多模态AI助手」与「推荐系统 v146」两个项目合二为一
>
> 本文档是什么：一份可以直接用来「读懂项目 + 跑起来 + 对验收」的说明文档。第一章讲它是什么和由什么合成，第二章讲它到底能做什么，第三至五章讲架构与阶段，第六至八章讲怎么跑与实测结果，第九至十一章讲限制、降级与提交物。所有数字均取自 outputs/ 下的落盘证据。
>
> 维护说明：本文档的实测数字随脚本重跑而变；凡与 `outputs/` 不符时，**以 outputs/ 下的证据文件为准**。




| 项目代号 | integrated_ai_project |
|---|---|
| 课程 | 《行业工程实践》 · 多模态个性化推荐助手 |
| 规模 | 6 个阶段 × 8 课时 = 48 课时 |
| 合成来源 | 模块一 推荐系统 v146 ＋ 模块二 智能多模态AI助手 |
| 系统形态 | 可完全离线运行的「Web 对话界面 + 终端对话 + 一键演示」三段式入口 |
| 核心技术 | 统一事件契约 · 规则+词表意图理解 · 多路召回 · 可控精排 · 证据包 + RAG 解释 · 反馈闭环 A/B |
| 工程约束 | 单一配置入口 · 适配器接口边界 · 结构化错误码 · 每个结论都有可重跑证据 |
| 文档版本 | v1.0 · 依据 6 份阶段任务书与 outputs/ 实测证据整理 |


## 目录

- 一、这个项目是什么
- 二、能做出什么功能
- 三、系统架构
- 四、六个阶段做了什么、交付了什么
- 五、目录结构
- 六、安装与运行
- 七、输入与输出
- 八、实测验收数据
- 九、已知限制
- 十、降级方式（真实模型 / 外部服务不可用时）
- 十一、文档与提交包清单

## 一、这个项目是什么

### 1.1 一句话概括

「多模态个性化推荐助手」是一个可完全离线运行的多模态对话式推荐系统：用户用语音、摄像头画面、手势或文字说出购物需求，系统理解意图与约束、从多路候选池召回商品、在硬约束下重新排序、为每一条推荐给出可核验的证据解释，并用文字和语音回复；用户的一个 👍/👎 会立刻写回会话偏好并改变下一次排序，而且变化前后可以逐条对照。

### 1.2 由来：两个前序项目合二为一

本项目不是从零写的，而是把之前分别做过的两套独立系统，整合成「通过稳定接口协作」的一个工程系统。两套系统各自保留原有能力，集成层负责在中间做翻译、治理和兜底。

| 来源 | 原项目 | 原本能做什么 | 在本项目中的角色 | 接入方式 |
|---|---|---|---|---|
| 模块一 | 推荐系统 v146 （Desktop\推荐系统） | XGBoost rank:pairwise 精排（800 棵树）与 v97 基线按 alpha=0.10 秩融合，官方综合分 0.8025；3642 件商品目录 / 736 个用户离线打分 / 23 个类目 / GBP 计价；自带 FastAPI 前端（127.0.0.1:8010） | 外部系统 B：候选池与排序分数来源 | 只经 RecommenderAdapter HTTP → 发布制品 → 内置快照 三级降级 |
| 模块二 | 智能多模态AI助手 （Documents\trae_projects\multimodal_ai） | SenseVoiceSmall ASR + WebRTC VAD 断句、Qwen3 文本 LLM、BGE+FAISS 知识库 RAG、Qwen2.5-VL 视觉理解、edge-tts/SAPI 语音合成、MediaPipe Pose/Hands 六种手势（本项目页面仅启用 👍/👎）、PyQt5 GUI | 外部系统 A：系统的「感官」与语音出口 | 只经 MultimodalAdapter 真实设备/模型不可用时走离线场景桩 |
| 集成层 | integrated_ai_project （本项目） | 原本不存在 | 事件协议、意图理解、会话状态、多路召回、可控精排、证据解释、反馈闭环、验收与证据 | 本项目全部代码 |

表 1-1  两个前序项目的能力与接入方式

关键边界：合成之后这两套系统仍然「互不认识」。集成层不 import 对方模块、不读对方内部变量、也不碰对方 GUI 对象——所有跨系统往来都走适配器与统一契约。这条是阶段 1 的硬性验收项。

### 1.3 合成之后多出来的能力

单独任何一套系统都做不到下面这些事，它们是「合成」这件事本身的产出：

| 新增能力 | 具体说明 |
|---|---|
| 统一事件契约 | 把语音 / 视觉 / 手势 / 文本四类输入压成同一种信封（trace_id、user_id、timestamp、source、session_id、schema_version），下游只认这一种输入，模态差异全部收敛在适配器里 |
| 结构化意图与槽位 | 六类意图（discover / similar / filter / compare / feedback / explain）× 多类槽位（品类、商品类型、预算、对象、颜色、场景、风格/主题、材质/工艺），每个槽位都带证据来源与置信度；必选槽位缺失时澄清而不臆测 |
| 会话上下文 | 多轮约束合并（类目 + 预算可同时生效）、撤销、冲突检测、会话级偏好隔离与结束清理 |
| 可控精排 | 硬约束（预算 / 类目 / 颜色 / 排除项）在打分之前过滤，排序分数不得覆盖硬约束；空结果时先放宽再热门兜底，并明确标记 degraded |
| 证据包与可核验解释 | 解释器只允许读 EvidencePack；每条解释至少一条实质证据；无证据时返回 no_evidence 而不是编造 |
| 反馈闭环 A/B | 固定同一用户 / 同一查询 / 同一候选条件，记录反馈前后的 TopK 与分数变化，并包含「无变化」和「反向变化」案例 |
| 全链路 trace 与降级留痕 | 每次请求都能回答：这次用了哪个模式、命中哪几路召回、丢掉多少候选、降级了什么 |
| 可复现证据链 | 每个阶段结论都由可重跑脚本产出 JSON 落盘；文档里的数字由脚本从证据文件生成，从机制上避免「只修改报告文字」 |

表 1-2  合成带来的八项新增能力

### 1.4 贯穿全程的四条工程原则

- 单一配置入口：所有阈值、权重、开关与路径集中在 config/settings.py，禁止硬编码绝对路径、密钥和商品 ID。

- 接口边界清晰：两套外部系统只经适配器访问，跨项目不共享内存对象。

- 失败必须可区分：任何失败都返回带 code + message + field 的结构化错误，禁止「捕获异常后返回空字典」。

- 结论必须有证据：每个结论配一个可重跑脚本加一份 outputs/ 落盘证据。

## 二、能做出什么功能

### 2.1 六个典型场景

场景 1｜说一句话，货架就更新

  「我要给朋友挑一个结婚礼物，500以内」

  → 左侧货架换成「筛选结果 · 婚礼 · 装饰品 ≤ £500」，出 8 件商品，每件都有推荐理由

场景 2｜指代追问

  「为什么推荐第一个」

  → 序号指代解析到当前第 1 名 → 输出带证据编号的解释（该案例 9 条证据）

场景 3｜手势点赞即时重排

  👍 → 写回会话偏好（正负标签互斥）→ 重新召回与排序 → 给出反馈前后 TopK 与分数对照

场景 4｜看着东西问

  👁 抓一帧画面 → 视觉标签（低置信丢弃 / 中置信降权 / 高置信保留）→ 作为槽位进入推荐

场景 5｜翻看的商品会轻微上浮

  点开某件商品详情（浏览）→ 记为**弱正反馈**写入会话偏好 → 下一次排序里它轻微上浮（实测 Δ+0.0175，明显小于点赞的 Δ+0.055），且不改变点赞/倒赞状态

场景 6｜失败也要说清楚

  召不回任何东西 → status=empty + E_NO_RESULT + 明确提示语，不返回空数组了事

### 2.2 功能清单

十二个功能模块，每一行都能在代码里找到位置、在 outputs/ 里找到对应的实测证据：

| # | 功能模块 | 具体能力 | 代码位置 | 阶段 | 验收证据 / 结果 |
|---|---|---|---|---|---|
| 1 | 契约与错误治理 | 四类数据结构（InteractionEvent / RecommendationRequest / RecommendationItem / EvidencePack）+ JSON 校验；27 个错误码；统一响应信封；schema 版本校验 | src/contracts.py src/errors.py | 1 | stage01_contract_report.json 25/25 PASS |
| 2 | 多模态适配 | ASR 文本、VL 视觉标签、手势事件 → InteractionEvent；真实模型与离线桩输出同一套契约 | src/adapters/ multimodal_adapter.py scripts/mm_worker.py | 1 · 2 | real_multimodal_check.json 契约一致 contracts_all_ok |
| 3 | 推荐适配 | HTTP /api/recommendations → 发布制品 → 内置快照 三级降级；候选统一字段、去重与 TopK 截断 | src/adapters/ recommender_adapter.py | 1 · 3 | live_integration.json HTTP 候选 8 条，三轮主链路 ok |
| 4 | 意图理解 | 六类意图 + 多类槽位抽取，记录证据来源与置信度；必选槽位缺失不臆测；约束冲突可检测 | src/intent.py | 2 | stage02_context_metrics.json 26/26（阈值 ≥80%） |
| 5 | 会话上下文 | 多轮约束合并、撤销、冲突提示、点赞/倒赞正负互斥、会话偏好隔离与结束清理 | src/session.py src/privacy.py | 2 · 5 | 会话检查 12/12； 跨会话隔离复核全部一致 |
| 6 | 多路召回 | 十一路：个性化精排 / 相似商品 / 品类热门（含备用类目）/ 商品类型 / 颜色 / 材质 / 主题 / 场景 / 会话关注 / 全局热门 / 历史购买相似；合并去重保留全部 sources 与分数明细 | src/recall.py | 3 | stage03_demo.json 候选来源可追溯 |
| 7 | 可控精排 | 硬约束先过滤（预算/类目/颜色/排除项）；0.55 基础分 + 0.20 约束命中 + 0.10 来源可信 + 0.15 会话偏好重排；空结果放宽与热门兜底 | src/ranking.py | 3 | 硬约束核验 0 违规； 三类 trace：ok / empty / degraded |
| 8 | 证据包 | 商品事实、候选来源、命中约束、视觉标签、KB 片段、价格一致性核验 | src/evidence.py src/catalog.py | 4 | 解释↔证据对照表 证据条数 9 / 6 / 6 / 7 / 8 |
| 9 | RAG 解释 | 纯 Python BM25 检索 6 篇知识库文档 / 18 个片段；可选接入真实 LLM，但须连过事实、格式、流畅度三道门控 | src/explain.py src/rag.py src/llm_explain.py | 4 | stage04_explanations.json 7/7；Qwen3-4B 门控通过 4/4 |
| 10 | 文本 / 语音呈现 | 文本回复 + TTS 语音播报（edge-tts → Windows SAPI System.Speech → SAPI COM → 纯文本四级）；播报内容是文本的摘录而非新内容 | src/presentation.py | 4 | 呈现一致性检查通过 （92 字文本 → 14 字播报） |
| 11 | 反馈闭环 | 点赞 / 倒赞写入会话偏好、正负互斥、反馈前后 A/B 对照、负反馈不污染其他会话 | src/feedback.py | 5 | stage05_feedback_ab.json 8/8：成功 4 · 无变化 2 · 反向 1 · 失败 1 |
| 12 | 可观测与验收 | 全链路 trace、降级矩阵、故障注入、隐私扫描、打包清单、一键端到端验收 | src/orchestrator.py src/privacy.py | 3 · 6 | stage06_acceptance.json 隐私扫描 PASS（文件数随文档增删变动，以 stage06_acceptance.json 为准） |

表 2-1  功能清单与实测验收对照

### 2.3 三个入口

| 入口 | 命令 | 用途与特点 |
|---|---|---|
| Web 对话界面 | python webui.py → http://127.0.0.1:8020 | 按电商平台形态组织：顶部搜索框 + 左侧商品货架 + 右侧推荐助手。点图看详情（大图 / 中英标题 / 参考价 / 价格来源 / 历史成交 / 推荐理由 / 证据 / 对比 / 追问），卡片右上角 👍/👎 常驻并局部更新。零新增依赖（Python 标准库实现） |
| 终端对话 | python chat.py | 不开浏览器就用它。支持直接打字，也支持 /voice 录音识别、/gesture 手势、/vision 画面理解，以及 /undo 撤销、/reset 重开、/state 看槽位与反馈、/llm on&#124;off、/say on&#124;off |
| 一键演示 | python main.py | 写死的三轮端到端演示脚本，用于验收取证（含语音播报）。日常对话请用 chat.py 或 webui.py |

表 2-2  三个运行入口

端口说明：:8020 是本项目自己的对话界面；:8010 是外部推荐系统自带的点击式推荐流，两者互不相干。

> 开放问答：`--real` 下非购物类问句（如「什么是协同过滤？」）由常驻 Qwen3-4B 回答，
> 不返回商品；首次加载约 25~40 秒，之后多轮复用（实测 4.2 秒/轮）。
> 购物需求仍走推荐链路，`你是谁` 等寒暄仍是 0 ms 固定文案。
>
> 识图推荐：点「📷 识图推荐」后，系统会抓取当前摄像头画面，优先用在线
> `qwen3-vl-plus` 识别画面中央的主要物品（实测约 1~2 秒），失败时回退本地 Qwen2-VL。
> 识别文本会抽取品类与颜色，再进入现有召回/精排链路更新货架。
> 工具条新增「🎤 语音识图」：录音 5 秒并强制同轮抓帧，适合一边举着物品一边说
> 「这个适合送人吗」；不依赖「手上」等关键词。原有「🎤 语音」按钮仍会在听到
> 「手上 / 手里 / 拿着」等词时自动触发识图。
> 也可以直接在对话框输入「推荐我手上相关的物品」或「画面里这个东西适合送人吗」，
> 系统会自动抓帧；用户问题进入对话/意图链路，视觉模型只输出“颜色+物品名称”。
> 首次使用设备前会显示摄像头、麦克风和在线图片上传提示；识图结果按图片哈希短期缓存，
> 并限制请求频率。模型、延迟、缓存命中和回退原因会写入 `trace.vision`。
> 例如识别到「茶杯」会落到「厨房用品」并推荐茶具类商品；如果识别不到可映射的品类，
> 页面会提示把物品放近、避免遮挡后重试，不会用无关商品凑结果。
>
> 手势：主页面不提供手势按钮；手势只在商品详情内使用。进入详情会自动开启手势直播，
> 只识别 👍/👎 并写入当前商品反馈；其他手势会被忽略。再次比同一手势会取消该商品的点赞/倒赞；
> 倒赞会先确认，确认后才从货架移除；撤销后恢复到原位置。若直播不可用，可在详情内点「改用单次识别」，
> 单次识别会倒计时 3 秒、连拍 5 帧并要求至少 3/5 帧一致。
> 倒赞后商品卡片消失时，可在助手消息里点「撤销倒赞」恢复并取消反馈。
> 操作卡会显示本次抓拍的画面（点击可看大图）；货架右上角可下拉选择显示 8/16/24/40 件，
> 滚动到底会自动续载（上限 120 件）。
> 语音输入使用常驻 ASR，启动时后台预加载，后续每轮约 4~5 秒（含 4 秒录音）。
> 打开商品详情会自动开启手势直播：摄像头仅在详情打开期间实时预览 + 持续识别；
> 👍/👎 连续 3 帧稳定后自动更新当前商品，换手势不需要把手移出画面；
> 倒赞会先确认，确认后才移除商品；关闭详情会立即停止摄像头。

## 三、系统架构

### 3.1 系统上下文：三套系统如何协作

```text
[用户]
```

```text
  │
```

```text
  ├── 语音 / 画面 / 手势 ──▶ 智能多模态AI助手（外部系统 A）
```

```text
  │                          ASR · VL · 手势识别 · 知识库 RAG · TTS
```

```text
  │                                    │
```

```text
  │                          InteractionEvent（统一事件契约）
```

```text
  │                                    ▼
```

```text
  └── 浏览 / 点击 / 点赞 ──▶ 集成层 integrated_ai_project（本项目）
```

                             意图理解 · 会话上下文 · 多路召回 · 可控精排

                             证据包 · RAG 解释 · 文本/语音呈现 · 反馈闭环

```text
                                       │
```

                              RecommendationRequest / 发布制品 / HTTP

                                       ▼

                             推荐系统 v146（外部系统 B）

                             XGBoost rank:pairwise 精排 · 相似索引 · 商品目录

图 3-1  系统上下文（阶段 1 交付物 docs/architecture.md §1）

### 3.2 分层结构

| 层 | 模块 | 职责 |
|---|---|---|
| 契约层 | src/contracts.py · src/errors.py · src/utils.py | 四类数据结构与 JSON 校验、27 个错误码、统一信封、schema 版本、通用工具 |
| 适配层 | src/adapters/base.py src/adapters/multimodal_adapter.py src/adapters/recommender_adapter.py | 两个外部系统的唯一接口层。统一走 guard 包装，负责超时、重试、降级留痕与健康检查 |
| 数据层 | src/catalog.py · src/rag.py · data/offline/ · data/kb/ | 离线商品目录、个性化分数、相似索引、热门池、历史；知识库 BM25 检索 |
| 理解层 | src/intent.py · src/session.py · src/text_norm.py | 六类意图与五类槽位、证据与置信度、会话状态与偏好、文本规范化 |
| 决策层 | src/recall.py · src/ranking.py | 多路召回、合并去重与 TopK、硬约束过滤、上下文重排与降级 |
| 表达层 | src/evidence.py · src/explain.py · src/llm_explain.py · src/presentation.py | 证据包组装、模板解释、可选 LLM 解释与三重门控、文本与语音呈现 |
| 闭环层 | src/feedback.py · src/privacy.py | 反馈写入与会话偏好、反馈前后 A/B、隐私与提交包扫描 |
| 编排层 | src/orchestrator.py | 主链路编排、trace 与耗时、模式与降级记录、状态判定 |

表 3-1  八层结构与模块职责

### 3.3 主链路：从一句话到一次反馈

```text
① 输入      语音「我要给朋友挑一个结婚礼物，500以内」
```

              → InteractionEvent(source=voice, trace_id, user_id, timestamp, session_id)

```text
② 意图      intent=filter；槽位 {category=装饰品, scene=婚礼, budget_max=500}
```

              缺失必选槽位 → E_MISSING_SLOT + 澄清问题（不臆测）

```text
③ 会话      SessionContext 合并多轮约束（类目与预算同时生效）
```

              约束互相矛盾 → E_CONSTRAINT_CONFLICT

```text
④ 召回      个性化 / 相似 / 品类热门 / 商品类型 / 颜色 / 材质 / 主题 / 场景 / 会话关注 / 全局热门 / 历史 多路并行
```

              每路 recall_k=80 → 合并去重（大小写视为同一商品）→ merge_top_k=120

              单路超时只影响该路，其余继续，链路不中断

```text
⑤ 硬约束    预算 / 类目 / 颜色 / 排除项 先过滤 —— 排序分数不得覆盖硬约束
```

```text
⑥ 精排      0.55×归一化基础分 + 0.20×约束命中 + 0.10×来源可信 + 0.15×会话偏好
```

              非个性化候选基础分打 0.5 折扣 → TopK = 8

```text
⑦ 证据      EvidencePack：商品事实 / 候选来源 / 命中约束 / 视觉标签 / KB 片段 / 一致性核验
```

```text
⑧ 解释      模板解释逐句有据；接入真实 LLM 时须连过 事实门控 → 格式门控 → 流畅度门控
```

              无证据 → status=no_evidence，不编造

```text
⑨ 呈现      文本 + 语音播报；TTS 不可用 → 纯文本（voice_available=false）
           演示前先跑 00_check_env，最后一行会打印「离线语音是否可用」
```

```text
⑩ 反馈      👍 → 会话偏好（正负互斥）→ 重新召回排序 → 反馈前后 TopK 对照
```

图 3-2  主链路十步（阶段 1 时序图 + 阶段 3 编排实现）

### 3.4 契约要点

| 契约 | 内容 |
|---|---|
| 公共信封 | trace_id · user_id · timestamp(ISO8601) · source · session_id · schema_version=1.0.0 |
| InteractionEvent | 多模态交互事件：source ∈ voice / vision / gesture / text / feedback / system |
| RecommendationRequest | intent + slots（带 confidence 与 evidence）+ constraints（hard / soft）+ top_k |
| RecommendationItem | item_id · title · category · price · currency · score · rank · sources · evidence |
| EvidencePack | 解释器唯一允许读取事实的来源，七类字段，五类实质证据 |
| 六类意图 | discover（无必选槽位）/ similar(target) / filter(≥1 硬约束) / compare(targets≥2) / feedback(polarity) / explain(target) —— 缺失时返回 E_MISSING_SLOT |
| 27 个错误码 | 契约 7 · 意图与会话 5 · 多模态 3 · 推荐 5 · 证据 3 · 反馈 2 · 兜底 2 |
| 响应状态 | ok / degraded / clarification / conflict / empty / no_evidence / error |

表 3-2  四类数据契约与错误治理口径（详见 docs/protocol.md）

## 四、六个阶段做了什么、交付了什么

| 阶段 | 名称 | 任务目标 | 关键验收项 | 主要交付物 | 结果 |
|---|---|---|---|---|---|
| 1 | 需求分析、系统架构与接口契约 | 把两套系统整合为通过稳定接口协作的工程系统，形成统一事件、会话上下文和推荐结果协议 | 不读对方内部变量 · 信封四字段 · 非法输入明确错误 · 离线可跑 | docs/architecture.md docs/protocol.md outputs/stage01_contract_report.json | 25/25 |
| 2 | 多模态意图理解与用户上下文 | 把 ASR 文本、视觉标签与手势事件转换为结构化推荐意图、约束和短期偏好，支持追问、撤销与冲突处理 | 槽位缺失不臆测 · 👍👎 映射正负反馈 · 冲突可检测 · 20 条样例 ≥80% | outputs/stage02_context_cases.jsonl outputs/stage02_context_metrics.json docs/error_case_analysis.md | **26/26** 会话 12/12 |
| 3 | 对话编排、多路召回与可控精排 | 通过编排器调用推荐链路，完成候选召回、硬约束过滤、上下文重排和无结果降级 | 候选来源可追溯 · 硬约束不被分数覆盖 · 空结果有提示 · 同输入可复现 | outputs/stage03_demo.json outputs/stage03_traces.jsonl docs/timeout_and_degradation.md | **6/6** 三类 trace |
| 4 | 商品多模态理解、RAG 解释与交互呈现 | 用商品文本、属性与视觉标签形成证据包，以可核验事实生成解释，并通过文本或语音呈现 | 每条解释有证据 · 无证据不编造 · 价格属性一致 · 低置信视觉标签降权或忽略 | outputs/stage04_explanation_cases.jsonl outputs/stage04_explanations.json docs/explanation_anomalies.md | 7/7 |
| 5 | 反馈闭环、效果评估与工程优化 | 把点赞、倒赞和继续浏览等反馈写入会话偏好，比较反馈前后结果，并从效果、交互、性能和可靠性维度评估 | 排序变化可解释 · 负反馈不污染其他会话 · 对照口径一致 · 含无变化案例 | outputs/stage05_feedback_ab.json docs/performance_and_failures.md | **8/8** 四类案例齐备 |
| 6 | 系统联调、验收部署与课程总答辩 | 完成端到端联调、故障降级、隐私安全检查、部署说明、总项目报告与现场答辩 | 三类输入链路可运行 · 至少一种故障可降级 · 数字可追溯 · 无敏感数据入包 | outputs/stage06_acceptance.json docs/deployment.md docs/risk_register.md docs/project_report.md | **4/4** 隐私扫描 PASS |

表 4-1  六个阶段的交付与验收对照

验收方式（每阶段一致）：运行环境检查 → 执行本阶段脚本 → 展示结构化输出 → 解释一个成功案例与一个失败/边界案例 → 说明下一阶段如何复用。只展示界面而没有脚本、日志或结构化输出，不通过。

## 五、目录结构

integrated_ai_project/

```text
├─ main.py                     一键端到端演示（验收取证用）
```

```text
├─ chat.py                     终端对话入口
```

```text
├─ webui.py                    Web 对话界面 → http://127.0.0.1:8020
```

```text
├─ requirements.txt            可选增强依赖（离线链路零依赖）
```

```text
开发环境：项目内含 .vscode/ 配置（21 个任务 + 7 个调试配置），用 VS Code 打开 integrated_ai_project 即可开箱使用，
详见 .vscode/README.md。

├─ config/
```

```text
│  └─ settings.py              唯一配置入口：路径 / 词表 / 权重 / 降级矩阵 / 隐私策略
```

```text
├─ src/
```

```text
│  ├─ contracts.py  errors.py  utils.py        契约、27 个错误码、工具
```

```text
│  ├─ adapters/                                两个外部系统的唯一接口层
```

```text
│  ├─ catalog.py    rag.py                     离线数据访问、BM25 检索
```

```text
│  ├─ intent.py     session.py  text_norm.py   意图与槽位、会话上下文、文本规范化
```

```text
│  ├─ recall.py     ranking.py                 多路召回、硬约束过滤与上下文重排
```

```text
│  ├─ evidence.py   explain.py  llm_explain.py 证据包、模板解释、可选 LLM 解释与门控
```

```text
│  ├─ presentation.py                          文本与语音呈现
```

```text
│  ├─ orchestrator.py                          主链路编排与 trace
```

```text
│  └─ feedback.py   privacy.py                 反馈 A/B、隐私与包体扫描
```

```text
├─ scripts/
```

```text
│  ├─ 00_check_env.py ~ 06_e2e_acceptance.py   六阶段证据生产脚本
```

```text
│  ├─ 07_live_integration_check.py             与推荐系统在线联调（可选）
```

```text
│  ├─ 09_real_multimodal_check.py              真机多模态实测（可选）
```

```text
│  ├─ 10_real_llm_check.py  11_llm_model_compare.py   真实 LLM 与选型对比（可选）
```

```text
│  ├─ 12_text_norm_check.py                    文本规范化与流畅度门控
│  ├─ 14_asr_quality_check.py                  ASR 识别质量评测（可选）
│  ├─ 15_deps_check.py                         真实多模态依赖深检（可选）
│  ├─ 16_webui_click_path_check.py             网页点击路径回归（4 项，需 Edge）
│  ├─ 17_browser_qa.py                         浏览器运行时 QA（console/CWV/无障碍）
│  ├─ 18_contract_check.py                     网页 API 契约核验（14 项）
│  ├─ 19_security_check.py                     网页服务安全基线（8 项）
│  ├─ 13_webui_acceptance.py                   网页改版验收（27 项，需 Edge）
│  ├─ run_all_offline.py                       一键跑完整离线套件（汇总表）
```

```text
│  ├─ mm_worker.py                             多模态子进程 worker（避免包名冲突）
```

```text
│  └─ build_offline_snapshot.py  clean_workspace.py
```

```text
├─ data/
```

```text
│  ├─ offline/                 离线快照：商品目录 / 分数 / 相似 / 热门 / 历史 / 用户
```

```text
│  ├─ cases/                   阶段 2 / 4 / 5 用例数据（含 JSONL）
```

```text
│  └─ kb/                      知识库 6 篇（RAG 检索源）
```

```text
├─ docs/                       架构、协议、部署、风险登记、项目报告、同伴复现指引
```

```text
├─ outputs/                    各阶段证据文件（本文档数字的来源）
```

```text
└─ artifacts/                 语音与图片产物（可再生，不入提交包）
```

图 5-1  项目目录结构

## 六、安装与运行

### 6.1 环境要求

- 操作系统：Windows 10 / 11（实测环境：RTX 4060 + Realtek 麦克风 + 640×480 摄像头）

- Python：Python 3.10 ~ 3.12（实测 3.12）

- 依赖：离线演示链路零第三方依赖，仅用标准库即可完整跑通六阶段脚本。

以下依赖存在时自动增强，缺失则自动降级，不会中断链路：

| 依赖 | 作用 | 缺失时的行为 |
|---|---|---|
| numpy | 数值处理 | 使用纯 Python 实现 |
| jieba | 中文分词（RAG 检索） | 退化为字符二元组 |
| requests | 推荐系统 HTTP 模式 | 跳过 HTTP 策略，使用本地制品 / 内置快照 |
| edge-tts | 在线语音合成 | 回退 Windows SAPI（System.Speech → SAPI COM，离线）；无语音包/无音频会话时回退纯文本 |
| zhconv | 繁体转简体（LLM 输出规范化） | 仅做检测，检出繁体即回退模板解释 |

表 6-1  可选依赖与降级行为

```text
python -m pip install -r requirements.txt
```

### 6.2 快速开始

```text
cd C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project
```

```text
python scripts/00_check_env.py      # 1. 运行环境检查
```

```text
python webui.py                     # 2. ★ 网页对话界面 → http://127.0.0.1:8020
```

```text
python chat.py                      # 3. ★ 终端对话
```

```text
python main.py                      # 4. 一键端到端演示（含语音播报）
```

默认完全离线：不需要网络、不需要启动外部服务、不加载任何重型模型。

需要真实设备或真实模型时，用参数开启：

```text
python webui.py --real --voice      # 真实麦克风 / 摄像头 + 语音播报
```

```text
python webui.py --real --llm        # 再用真实 LLM 生成解释
```

```text
python webui.py --port 8030         # 换端口
```

```text
python chat.py --real --llm         # 终端 + 真实多模态 + 真实 LLM
```

```text
python chat.py --user 14156         # 换用户（15547 林先生 / 14156 陈小姐 / 17231 王女士）
```

### 6.3 分阶段证据生产（推荐验收顺序）

| 命令 | 阶段 | 产物 |
|---|---|---|
| python scripts/00_check_env.py | 环境 | outputs/env_check.json |
| python scripts/01_contract_test.py | 1 契约与降级矩阵 | outputs/stage01_contract_report.json |
| python scripts/02_context_test.py | 2 意图与会话 | outputs/stage02_context_cases.jsonl + stage02_context_metrics.json |
| python scripts/03_orchestrator_demo.py | 3 编排 / 召回 / 精排 | outputs/stage03_demo.json + stage03_traces.jsonl |
| python scripts/04_explanation_test.py | 4 证据与解释 | outputs/stage04_explanation_cases.jsonl + stage04_explanations.json |
| python scripts/05_feedback_ab.py | 5 反馈 A/B | outputs/stage05_feedback_ab.json |
| python scripts/06_e2e_acceptance.py | 6 端到端与验收 | outputs/stage06_acceptance.json + e2e_run_log.jsonl |
| python scripts/07_live_integration_check.py | 在线联调（可选） | outputs/live_integration.json |
| python scripts/08_gen_docs.py | 文档生成（需先跑 02~05） | docs/*.md + outputs/docs_manifest.json |
| python scripts/09_real_multimodal_check.py | 真机多模态（可选） | outputs/real_multimodal_check.json |
| python scripts/10_real_llm_check.py | 真实 LLM 解释链路（可选） | outputs/real_llm_check.json |
| python scripts/11_llm_model_compare.py | LLM 选型对比（可选） | outputs/llm_model_compare.json |
| python scripts/12_text_norm_check.py | 文本规范化与流畅度门控 | outputs/text_norm_check.json |
| python scripts/14_asr_quality_check.py | ASR 识别质量评测（需权重，可选） | outputs/asr_quality_check.json |
| python scripts/15_deps_check.py | 真实多模态依赖深检（不联网） | outputs/deps_check.json |
| python scripts/16_webui_click_path_check.py | 网页点击路径回归（需 webui 在跑 + Edge） | outputs/webui_click_path.json |
| python scripts/17_browser_qa.py | 浏览器运行时 QA（console/网络/CWV/无障碍） | outputs/browser_qa.json |
| python scripts/18_contract_check.py | 网页 API 契约核验（protocol §10） | outputs/contract_check.json |
| python scripts/19_online_llm_check.py | 在线 LLM 联通性与延迟（需 Key，可选） | outputs/online_llm_check.json |
| python scripts/19_security_check.py | 网页服务安全基线（8 项） | outputs/security_check.json |
| python scripts/21_deepseek_search_check.py | DeepSeek Search/Fetch 实测（需 Key，可选） | outputs/deepseek_search_check.json |
| python scripts/20_fingerprint_stability.py | 连续两轮离线套件，验证证据指纹可重复 | 控制台结果 |
| python scripts/13_webui_acceptance.py | 网页改版验收（需 webui 在跑 + Edge） | outputs/webui_acceptance.json |
| python scripts/run_all_offline.py | 一键跑完整离线套件（9 个脚本，约 24 秒） | 刷新全部 outputs/ 证据 |
| python scripts/clean_workspace.py | 打包前清理 | 删除 __pycache__ / *.pyc |

表 6-2  脚本与证据产物对应表

### 6.4 运行模式

```text
python main.py --mode offline   # 默认：只用内置离线快照（可离线演示）
```

```text
python main.py --mode auto      # 尝试真实模型 / 外部服务，不可用则逐级降级
```

```text
python main.py --mode online    # 强制走推荐系统 HTTP 接口
```

```text
python main.py --user 14156     # 指定演示用户
```

### 6.5 与外部系统联调（可选）

推荐系统根目录与多模态助手根目录会自动探测，也可用环境变量显式指定。账号与口令只走环境变量，不写入任何文件。

```text
# 启动推荐系统（127.0.0.1:8010）
```

```text
cd C:\Users\Administrator\Desktop\推荐系统\v146_release
```

.\run_frontend.ps1 -Install

```text
# 回到本项目，启用 HTTP 模式
```

```text
$env:INTEGRATED_MODE = "auto"
```

```text
$env:INTEGRATED_DEMO_USER = "<演示账号>"
```

```text
$env:INTEGRATED_DEMO_PASSWORD = "<演示口令>"
```

```text
python main.py
```

```text
# 验证集成层的 HTTP 适配器确实对接上了
```

```text
python scripts/07_live_integration_check.py
```

- INTEGRATED_RECOMMENDER_ROOT —— 显式指定推荐系统根目录

- INTEGRATED_MULTIMODAL_ROOT —— 显式指定多模态助手根目录

- INTEGRATED_MODE —— offline / auto / online

- INTEGRATED_ALLOW_REAL_MODELS —— 是否允许加载真实模型

- INTEGRATED_LLM_PROVIDER —— auto（默认，在线优先、失败回退本地）/ online / local

- DASHSCOPE_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY —— 在线模型 Key（任选其一，自动识别；DASHSCOPE 同时用于在线识图）

- INTEGRATED_ONLINE_LLM_API_KEY + INTEGRATED_ONLINE_LLM_BASE_URL + INTEGRATED_ONLINE_LLM_MODEL —— 任意 OpenAI 兼容网关

- INTEGRATED_ONLINE_LLM_STRICT —— 1 表示在线失败禁止回退本地；默认 0

- 项目根目录 `local.env` —— 启动时自动读取（不覆盖已有环境变量），适合本机填写 DeepSeek Key；该文件不进入 Git 或提交包

- INTEGRATED_WEB_SEARCH / INTEGRATED_WEB_SEARCH_COUNT —— DeepSeek Search 自动搜索开关与返回条数

- INTEGRATED_WEB_SEARCH_CONTENT / INTEGRATED_WEB_SEARCH_TIMEOUT_S —— 是否抓取正文、搜索超时（秒）

## 七、输入与输出

### 7.1 四类输入

| 方式 | 格式 | 示例 |
|---|---|---|
| 语音 | {"source": "voice", "text": "<ASR 文本>"} | 「500以内的白色相框」 |
| 视觉 | {"source": "vision", "text": "...", "visual_tags": [{"tag": "白色", "confidence": 0.9}]} | 画面标签补充颜色 / 物体 / 场景 |
| 手势 | {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.92}} | 👍 / 👎 → 正 / 负反馈 |
| 文本 | {"source": "text", "text": "和这个相似的商品"} | 支持序号指代「第一个」 |

表 7-1  四类输入的统一表示

### 7.2 统一响应信封（关键字段）

```text
{
```

  "schema_version": "1.0.0",

  "trace_id": "tr-...", "user_id": "15547", "timestamp": "...",

  "source": "voice", "session_id": "sess-15547|2026-10-13",

  "status": "ok | degraded | clarification | conflict | empty | no_evidence | error",

  "intent_result": {"intent": "filter", "slots": {...},

                    "missing_slots": [], "conflicts": []},

  "effective_request": {"...会话合并后的实际请求..."},

  "recommendations": [

```text
    {"rank": 1, "item_id": "20829", "title": "闪粉悬挂蝴蝶串", "price": 2.1,
```

     "score": 0.723, "sources": ["推荐系统 v146 个性化精排"], "explanation": "推荐理由：…"}

  ],

  "presentation": {"text": "...", "voice_available": true, "audio_path": "..."},

  "trace": {

    "latency_ms": {"intent_ms": 0.9, "recall_ms": 2.2, "rank_ms": 0.4, "total_ms": 4.6},

    "modes": {"runtime_mode": "offline", "multimodal": "offline_stub",

              "recommender": "offline_snapshot"},

    "degradations": {...},

    "recall": {"sources": [...], "merged_unique": 298, "kept": 120},

    "ranking": {"input_candidates": 120, "after_hard_filter": 4,

                "hard_dropped": 116, "degraded": false}

```text
  },
```

  "error": null

```text
}
```

### 7.3 状态取值含义

| status | 含义 |
|---|---|
| ok | 正常返回 |
| degraded | 经过降级才拿到结果（例如硬约束过滤后为空，放宽并热门兜底） |
| clarification | 必选槽位缺失，需要用户澄清 |
| conflict | 约束互相冲突，需要用户裁决 |
| empty | 所有召回来源均无结果，返回 E_NO_RESULT 并给出提示 |
| no_evidence | 有候选但无可用证据，拒绝编造解释 |
| error | 返回带 code + message + field 的结构化错误 |

表 7-2  七种响应状态

## 八、实测验收数据

### 8.1 六阶段验收结论

| 阶段 | 关键验收项 | 结论 | 证据文件 |
|---|---|---|---|
| 1 | 接口边界 / 信封字段 / 明确错误码 / 离线可跑 | 25/25 通过（成功 9 · 失败 11 · 边界 5） | outputs/stage01_contract_report.json |
| 2 | 不臆测缺失槽位 / 👍👎 映射 / 冲突检测 / 样例 ≥80% | 意图 26/26（阈值 80%）+ 会话 12/12 | outputs/stage02_context_metrics.json |
| 3 | 来源可追溯 / 硬约束不被覆盖 / 空结果提示 / 可复现 | 6/6 通过；硬约束核验 0 违规 | outputs/stage03_demo.json |
| 4 | 每条解释有证据 / 无证据不编造 / 价格一致 / 低置信降权 | 7/7 通过；证据不足案例正确返回 no_evidence | outputs/stage04_explanation_cases.jsonl |
| 5 | 变化可解释 / 不跨会话污染 / 口径一致 / 含无变化案例 | 8/8 通过（成功 4 · 无变化 2 · 反向 1 · 失败 1） | outputs/stage05_feedback_ab.json |
| 6 | 三类链路 / 故障降级 / 数字可追溯 / 无敏感数据 | 5/5 通过；隐私扫描 PASS | outputs/stage06_acceptance.json |

表 8-1  阶段验收结论（数字均由脚本重跑产出）

### 8.2 端到端与故障注入

| 用例 | 类别 | 内容 | 结果 |
|---|---|---|---|
| E2E-01 | 成功 | 语音链路：ASR 文本 → 意图 → 推荐 → 解释 → 语音播报 | ok |
| E2E-02 | 成功 | 视觉链路：画面标签 → 颜色槽位 → 推荐 → 解释 | ok |
| E2E-03 | 成功 | 手势链路：thumbs_up → 正反馈 → 会话偏好 → 重新排序 | ok |
| FAULT-01 | 边界 | 推荐系统 HTTP 不可用 → 自动降级到发布制品 / 内置快照 | 降级留痕 |
| FAULT-02 | 边界 | 个性化召回超时 → 其余召回来源继续，链路不中断 | ok |
| FAULT-03 | 边界 | 语音合成不可用 → 降级为纯文本呈现 | ok |
| FAULT-04 | 失败 | ASR 结果为空 → 返回 E_EMPTY_TRANSCRIPT，不做意图推测 | error（预期） |

表 8-2  端到端三类链路与四类故障注入

### 8.3 真实设备与真实模型实测

| 实测项 | 环境 / 模型 | 结果 |
|---|---|---|
| 真实 ASR | Realtek 麦克风 + 离线控制音频 | 控制音频识别为「开饭时间早上九点至下午五点」，与预期一致 |
| 真实视觉理解 | 摄像头 640×480 + Qwen2-VL-2B-Instruct | 输出「画面里有一个人，他穿着黑色的衣服，坐在椅子上。」 |
| 真实手势识别 | MediaPipe Pose / Hands | 批量扫描 46 帧：14 帧检出手、10 帧识别出手势（victory ×9、thumbs_up ×1；victory 仅验证识别能力，页面忽略） |
| 多模态适配器 | 8 次调用 | 成功率 100%，平均 14.5 s、最大 32.6 s |
| 真实 LLM 解释 | Qwen3-4B-Instruct-2507 | 门控通过 4/4（100%），平均 12.35 s / 条 |
| 真实 LLM 对比 | Qwen2-1.5B-Instruct | 门控通过 3/6（50%），会编造价格数字，故不采用 |
| 契约一致性 | 真实链路 vs 离线链路 | 两者输出契约完全一致（contracts_all_ok = true） |
| 在线联调 | 推荐系统 HTTP 127.0.0.1:8010 | active_mode=http，HTTP 候选 8 条，三轮主链路 ok |
| 文本规范化 | 繁体检测 / 转换 / 英文词替换 / 去多余空格 | 10/10 通过 |

表 8-3  真实设备与真实模型实测结果

门控的含义：LLM 生成的解释必须依次通过事实门控、格式门控、流畅度门控三关才能替代模板解释，否则自动回退到逐句有据的模板解释。所以「门控通过率低」说明门控在工作，而不是链路故障。

### 8.4 隐私与打包

| 检查项 | 结果 |
|---|---|
| 隐私扫描 | verdict = PASS（本次运行的文件数见 outputs/stage06_acceptance.json）；无禁用文件、无敏感字面量、无绝对路径硬编码 |
| 提交包体量 | 94 个条目 / 3.46 MB（已排除 artifacts、__pycache__、*.pyc） |
| 禁止入包 | __pycache__ · *.pyc · *.pth/pt/safetensors/bin/onnx · 音视频 · 图片 · .env · *.log · *.zip · *.db |
| 敏感键名 | password · passwd · secret · token · api_key · cookie |

表 8-4  隐私安全与提交包检查

## 九、已知限制

- 离线快照中的个性化分数是推荐系统 v146 在样本候选集上的离线打分，不是在线实时推理。

- 相似索引由商品标题 / 描述 / 类目离线生成，对同义不同名的商品召回有限。

- 意图理解采用规则 + 词表方案，未使用学习型分类器（属于任务书允许的进阶项）。

- RAG 使用纯 Python BM25，未接入多模态助手的 BGE + FAISS（离线优先的取舍）。

- 真实 LLM 解释已接入并实测达标（Qwen3-4B-Instruct-2507 门控通过率 100%，4/4）；小参数模型（Qwen2-1.5B）为 50%，会编造价格数字，故默认不启用。

- 流畅度已做规范化（繁体转简、常见英文词替换、清理中文字间多余空格）；未安装 zhconv 时只做检测，检出繁体或残留英文即回退模板。偶发错别字（如「预算是内」）尚未处理。

- 真实多模态链路已在 RTX 4060 + Realtek 麦克风 + 640×480 摄像头上实测通过；但真实手势需要真人配合，本机实测部分依赖历史手势快照回放。

## 十、降级方式（真实模型 / 外部服务不可用时）

| 不可用对象 | 降级路径 | 结果表现 |
|---|---|---|
| 推荐系统 HTTP | 发布制品（local_artifacts） → 内置快照 | 结果正常，trace.degradations 留痕 |
| 推荐系统发布制品缺失 | 内置离线快照 data/offline/ | 使用内置快照，功能不中断 |
| 真实 ASR / VL / 手势模型 | 离线场景桩（data/cases/） | 契约一致，仅数据来源不同 |
| ASR 结果为空 | 明确报错，不推测意图 | E_EMPTY_TRANSCRIPT |
| RAG 无命中 | 只用结构化字段生成解释 | 不编造 |
| 硬约束过滤后为空 | 放宽软约束 + 热门兜底 | status=degraded + 提示语 |
| TTS 不可用 | 纯文本呈现 | voice_available=false |
| 单路召回超时 | 其余召回来源继续 | 该路标记 failed，链路不中断 |
| 多模态助手整体不可用 | MultimodalAdapter 返回 offline_stub | 链路可跑，事件来源标注为场景桩 |

表 10-1  九条降级路径

## 十一、文档与提交包清单

### 11.1 交付文档清单

| 任务书要求 | 文件 | 内容 |
|---|---|---|
| 阶段 1 · 系统上下文 / 容器 / 时序 / 数据流 | docs/architecture.md | 上下文图、容器图、时序图、数据流与失败出口、降级矩阵、正常与边界样例 |
| 阶段 1 · 统一事件与请求响应 Schema | docs/protocol.md | 四类契约、六类意图与必选槽位、27 个错误码、适配器接口与边界约束 |
| 各阶段收尾 · README 五项 | README.md / 本文档 | 安装、运行、输入输出、已知限制、降级方式 |
| 阶段 2 · 错误案例分析 | docs/error_case_analysis.md | 通过率、未通过明细、已修复错误的根因与验证 |
| 阶段 3 · 超时与降级说明 | docs/timeout_and_degradation.md | 四类 trace、超时 / 空结果 / 降级口径、硬约束核验 |
| 阶段 4 · 异常解释记录 | docs/explanation_anomalies.md | 异常记录、解释↔证据对照表、置信度治理实测 |
| 阶段 5 · 性能与失败案例摘要 | docs/performance_and_failures.md | 延迟、反馈前后对照、失败与无变化案例、会话隔离复核 |
| 阶段 6 · 部署与风险文档 | docs/deployment.md · docs/risk_register.md | 依赖与启动说明、风险登记表、演示用例清单、验收记录 |
| 各阶段步骤 4 · 相邻小组复现 | docs/peer_review.md | 每阶段最小复现命令、期望输出、问题记录表 |
| 阶段 6 · 课程总项目报告 | docs/project_report.md | 需求 / 架构 / 实现 / 实验 / 问题 / 结论 / 成员贡献 / 参考资料 + 各阶段证据路径 |

表 11-1  交付文档清单

其中 error_case_analysis.md、timeout_and_degradation.md、explanation_anomalies.md、performance_and_failures.md 以及 architecture.md 的样例章节，全部由 python scripts/08_gen_docs.py 从 outputs/ 证据文件自动生成——证据一重跑，文档数字随之更新，从机制上避免「只修改报告文字」。

### 11.2 最短复现路径（给不熟悉项目的人）

```text
python scripts/00_check_env.py     # 环境自检（应显示 offline / 3642 件商品 / 0 次降级）
```

```text
python scripts/05_feedback_ab.py   # 任选一个阶段重跑
```

判据：脚本退出码 0、打印的通过数与本文档期望一致、

      outputs/ 下证据文件的修改时间为本次运行时间。

### 11.3 提交包

```text
python scripts/clean_workspace.py      # 清理缓存
```

```text
python scripts/06_e2e_acceptance.py    # 生成验收报告 + 隐私扫描（verdict 必须 PASS）
```

包含：config/ src/ scripts/ docs/ data/ outputs/ README.md requirements.txt main.py .gitignore

不含：artifacts/（语音产物，可再生）、__pycache__/、*.pyc、模型权重、音视频、数据库、压缩包、任何密钥
