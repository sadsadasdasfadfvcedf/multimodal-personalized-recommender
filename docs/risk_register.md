# 风险登记表 · 演示用例清单 · 验收记录

> 阶段6 交付物

## 1. 风险登记表

| ID | 风险 | 类别 | 概率 | 影响 | 等级 | 应对措施 | 落地位置 |
|---|---|---|---|---|---|---|---|
| R-01 | 真实 ASR/VL 模型在答辩机不可用（缺 GPU/驱动/依赖） | 技术 | 高 | 高 | **P0** | 适配器三级降级；`--mode offline` 默认不加载重型模型；离线场景桩与真实链路共用同一契约 | `src/adapters/multimodal_adapter.py` |
| R-02 | 推荐系统服务未启动 / 端口被占用 | 技术 | 中 | 中 | P1 | HTTP → 发布制品 → 内置快照自动降级，并在 trace 留痕 | `src/adapters/recommender_adapter.py` |
| R-03 | 麦克风/摄像头被其他进程占用 | 环境 | 中 | 中 | P2 | 演示前运行 `00_check_env.py`；准备场景桩作为替补 | `scripts/00_check_env.py` |
| R-04 | 语音合成依赖网络（edge-tts） | 环境 | 中 | 低 | P2 | 自动回退 Windows SAPI 离线引擎；再失败则纯文本呈现 | `src/presentation.py` |
| R-05 | 硬约束过滤后无结果导致空屏 | 产品 | 中 | 高 | **P0** | 分层降级并显式标注 `degraded=true` + `constraint_satisfied=false` + 提示语 | `src/ranking.py` |
| R-06 | 解释出现幻觉（编造属性/价格） | 合规 | 中 | 高 | **P0** | 解释只读 `EvidencePack`；无实质性证据直接 `E_NO_EVIDENCE`；价格一致性核验；接入真实 LLM 后增加**事实门控 + 格式门控**，不过则回退模板 | `src/explain.py` / `src/evidence.py` / `src/llm_explain.py` |
| R-07 | 负反馈污染其他会话 | 产品 | 低 | 高 | P1 | 偏好按 `session_id` 隔离；A/B 脚本含隔离复核（会话 C） | `src/session.py` / `src/feedback.py` |
| R-08 | 提交包夹带缓存/模型/密钥 | 合规 | 中 | 中 | P1 | `clean_workspace.py` + `privacy.py` 自动扫描，verdict 必须 PASS | `src/privacy.py` |
| R-09 | 演示现场“换输入即失效”（写死商品 ID） | 质量 | 低 | 高 | P1 | 代码零商品 ID；数据全部来自快照/制品；契约测试覆盖未知用户/未知商品 | `scripts/01_contract_test.py` |
| R-10 | 上游超时拖垮整条链路 | 技术 | 中 | 中 | P1 | 适配器统一 `guard` + 超时；单路失败不影响其余来源 | `src/adapters/base.py` |
| R-11 | 意图词表覆盖不足导致误判 | 质量 | 中 | 低 | P2 | 26 条样例回归（阈值 80%）+ 错误案例根因分析 | `scripts/02_context_test.py` |
| R-12 | 演示机器未安装 Python 依赖 | 环境 | 低 | 中 | P2 | 离线链路零第三方依赖即可跑通全部六阶段脚本 | 全项目 |

**风险等级定义**：P0 必须消除或必须现场规避；P1 需有明确降级路径；P2 记录并给出手工处置办法。

## 2. 演示用例清单（10 分钟演示脚本）

| 顺序 | 命令 / 操作 | 观察点 | 对应验收项 |
|---|---|---|---|
| 1 | `python scripts/00_check_env.py` | 运行模式、外部系统探测、快照规模、依赖状态 | 运行环境检查 |
| 2 | `python scripts/01_contract_test.py` | 25/25 通过；缺失字段、非法时间戳、非法 source 的错误码 | 非法输入返回明确错误 |
| 3 | `python scripts/02_context_test.py` | 26/26 意图样例；26 条会话行为中“必选槽位缺失不臆测”“thumbs_up/down 映射” | 意图与槽位、反馈映射 |
| 4 | `python scripts/03_orchestrator_demo.py` | NORMAL/EMPTY/DEGRADED/TIMEOUT 四条 trace；硬约束核验 | 候选可追溯、硬约束不被覆盖、空结果提示 |
| 5 | `python scripts/04_explanation_test.py` | 解释 ↔ 证据对照表；`E_NO_EVIDENCE` 案例 | 每条解释至少一项可核验证据 |
| 6 | `python scripts/05_feedback_ab.py` | 反馈前后 TopK 对照；无变化案例；会话隔离复核 | 排序变化可解释、无跨会话污染 |
| 7 | `python scripts/06_e2e_acceptance.py` | 三类输入链路 + 4 个故障注入 + 隐私扫描 PASS | 三类链路可运行、故障可降级 |
| 8 | `python scripts/07_live_integration_check.py` | 适配器 `active_mode=http`；HTTP 候选与快照候选对照 | 接口边界（只走对外接口） |
| 9 | `python main.py` | 「语音要礼物 → 推荐 → 点赞反馈 → 追问解释」完整对话 | 端到端演示 |
| 10 | 讲解失败案例 | DEGRADED 场景硬约束放宽的取舍；`E_NO_EVIDENCE` 不编造 | 失败/边界案例说明 |

## 3. 故障注入清单

| 编号 | 注入方式 | 期望行为 | 证据 |
|---|---|---|---|
| FAULT-01 | HTTP 模式但未提供演示账号 | 记录降级事件，回落到发布制品/内置快照 | `stage06_acceptance.json:fault_injections[0]` |
| FAULT-02 | `personalized_candidates` 返回 `E_TIMEOUT` | 该路标记 failed，其余来源继续，结果仍可返回 | `...fault_injections[1].failed_sources` |
| FAULT-03 | 屏蔽 edge-tts 与 Windows SAPI | `voice_available=false` + L2 降级事件，文本正常 | `...fault_injections[2].presentation` |
| FAULT-04 | ASR 文本为空白 | `status=error`，`code=E_EMPTY_TRANSCRIPT`，不推测意图 | `...fault_injections[3].error` |

## 4. 验收记录

| 验收项 | 判定方法 | 结果 | 证据文件 |
|---|---|---|---|
| 两套系统不直接读取对方内部变量 | 适配器只走 HTTP/发布制品；代码审查 | 通过 | `docs/protocol.md §9`、`src/adapters/*` |
| 字段包含 trace_id/user_id/timestamp/source | 契约测试 C-01/C-02 | 通过 | `outputs/stage01_contract_report.json` |
| 非法输入返回明确错误 | 契约测试 C-02~C-06、C-08~C-12 | 通过 | 同上 |
| 离线演示模式可运行 | `active_mode=offline_snapshot`，全脚本离线跑通 | 通过 | `outputs/env_check.json` |
| 必选槽位缺失时不臆测 | 用例 S2-14/S2-15/S2-18 | 通过 | `outputs/stage02_context_cases.jsonl` |
| thumbs_up/down 映射为正负反馈 | 契约 D-05/D-06 + 用例 S2-19/S2-20 | 通过 | `stage01` / `stage02` |
| 约束冲突可检测 | 用例 S2-23（预算上下限颠倒） | 通过 | `outputs/stage02_context_metrics.json` |
| 20 条样例通过率 ≥ 80% | 26 条样例实测 100% | 通过 | 同上 |
| 候选来源可追溯 | 每条候选带 `sources`；多路命中合并 | 通过 | `outputs/stage03_demo.json` |
| 硬约束不被排序分数覆盖 | `hard_constraint_verification` 零违规 | 通过 | 同上 |
| 空结果有清晰提示 | EMPTY 场景返回 `E_NO_RESULT` + 提示语 | 通过 | 同上 |
| 同输入和配置可复现 | 固定 seed 与排序键；重复运行结果一致 | 通过 | `docs/architecture.md §7` |
| 每条解释至少一项可核验证据 | 04 脚本对每条解释统计 `evidence_count ≥ 1` | 通过 | `outputs/stage04_explanation_cases.jsonl` |
| 无证据时不编造 | 用例 E-02 返回 `E_NO_EVIDENCE` | 通过 | 同上 |
| 商品价格与属性一致 | 用例 E-07 一致性核验 + 修正说明 | 通过 | 同上 |
| 视觉标签低置信度降权或忽略 | 契约 D-03 + 用例 E-04 | 通过 | `stage01` / `stage04` |
| 反馈后排序变化可解释 | 06 场景均输出变化原因 | 通过 | `outputs/stage05_feedback_ab.json` |
| 负反馈不污染其他会话 | 每个场景均含隔离复核（会话 C 与反馈前一致） | 通过 | 同上 |
| 至少一个无变化/失败案例 | F-03（无变化）、F-05（失败） | 通过 | 同上 |
| 三类输入链路可运行 | E2E-01/02/03 | 通过 | `outputs/stage06_acceptance.json` |
| 至少一种故障可降级 | FAULT-01~04 | 通过 | 同上 |
| 报告数字可追溯 | 每个场景带 `trace_id` 与耗时明细 | 通过 | 同上 |
| 无缓存/模型/敏感数据进入提交包 | `privacy.scan_tree()` verdict=PASS | 通过 | 同上 |
| HTTP 适配器对接真实推荐系统 | 服务在跑时 `active_mode=http`、无降级事件、三轮链路 ok | 通过 | `outputs/live_integration.json` |
| 真实多模态输出与离线契约一致 | `active_mode=real`、8 次调用成功率 100%、3 类事件信封完整 | 通过 | `outputs/real_multimodal_check.json` |
| 真实手势驱动反馈闭环 | 快照 `thumbs_up`(0.65) → polarity=positive → status=ok | 通过 | 同上 |
| 真实 LLM 解释不编造 | Qwen3-4B 6/6 通过门控；1.5B 3/6（编造数字被拦并回退模板）；零失败文本外泄 | 通过 | `outputs/real_llm_check.json`、`outputs/llm_model_compare.json` |
| 文本规范化与流畅度门控 | 繁体检测/转换、英文词替换与删除、循环复读截断 10/10 | 通过 | `outputs/text_norm_check.json` |

## 5. 已知限制

1. 离线快照的个性化分数是推荐系统 v146 在样本候选集上的**离线打分**，不是在线实时推理结果；
2. 相似索引以商品标题/描述/类目离线生成，对同义不同名的商品召回有限；
3. 意图理解目前是**规则 + 词表**方案，未使用学习型分类器（阶段文档允许的进阶项）；
4. 语音呈现依赖 Windows SAPI 或 edge-tts，未接入 multimodal_ai 的完整 TTS 音色体系；
5. 真实多模态模式已**实测通过**（RTX 4060 / Realtek 麦克风 / 640×480 摄像头）；
   限制在于：真实手势依赖真人配合，本机验证使用历史手势快照回放；
   真实 LLM 解释已接入：Qwen3-4B-Instruct-2507 可用率 100%（4/4），Qwen2-1.5B 为 50%；
   繁体与中英混排已由 text_norm 模块规范化，未映射英文即回退模板；
   偶发错别字（如「预算是内」）尚未处理；
   真实 VL 使用 Qwen2-VL-2B（`Qwen2.5-VL-3B` 权重目录为空、`Qwen3-4B` 权重不完整，已在适配层跳过）。
6. ASR 已分「采集端 / 转写端」实测（`outputs/asr_quality_check.json`）：
   转写端在 24 条固定测试音频上 CER=0.000、术语召回 97.8%、数字召回 100%、热推理 p50 124ms；
   但真机 4s 录音峰值仅 -24.9 dBFS（RMS -41.5 dBFS，比合成音频低 19–24 dB），
   同一模型返回空文本 —— 瓶颈在麦克风输入电平，不在模型。演示前须检查增益。
