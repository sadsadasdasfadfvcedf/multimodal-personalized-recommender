# 阶段3 · 超时与降级说明

> 阶段3 提交物 · 由 scripts/08_gen_docs.py 从证据文件自动生成，请勿手工编辑

- 生成时间：2026-10-15T05:55:21+00:00
- 数据来源：outputs/stage03_demo.json、outputs/stage03_traces.jsonl
- 重新生成：先执行对应阶段脚本，再执行 python scripts/08_gen_docs.py

## 1. 四类链路 trace

| 场景 | 状态 | 返回条数 | 端到端耗时 | 硬约束与降级 |
|---|---|---|---|---|
| NORMAL | ok | 8 | 3.729 ms | 淘汰 91 / 降级 False |
| EMPTY | empty | 0 | 0.98 ms | — |
| DEGRADED | degraded | 8 | 3.934 ms | 淘汰 120 / 降级 True |
| TIMEOUT | ok | 8 | 3.129 ms | 淘汰 0 / 降级 False |

## 2. 超时口径

适配器调用统一走 AdapterBase.guard；HTTP 层超时 → E_TIMEOUT；单路召回失败只影响该路，其余来源继续，最终结果在 trace.degradations 中留痕。

**实测（TIMEOUT 场景）**：注入 personalized_candidates → E_TIMEOUT 后，
- 失败来源：['personalized']（在 trace.recall.sources 中标记 failed）
- 其余来源继续参与合并；最终返回 8 条结果，status=ok，链路不中断。

## 3. 空结果口径

所有召回来源均空 → status=empty，返回 E_NO_RESULT，并给出清晰提示，不返回空数组了事。

**实测（EMPTY 场景）**：status=empty，error.code=E_NO_RESULT，message=所有召回来源均为空

## 4. 降级口径

硬约束（预算/颜色）过滤后为空 → 先放宽类目 → 再降级为热门兜底，degraded=true 且必须给出 empty_result_hint。

**实测（DEGRADED 场景）**：硬约束过滤淘汰 120 条候选，降级原因 = 全部硬约束过滤后为空，已降级为热门兜底；
降级结果全部标注 degraded=true 与 constraint_satisfied=false，并在呈现头部给出提示语。

## 5. 硬约束核验（对返回结果逐条比对）

| 场景 | 生效硬约束数 | 核验商品数 | 违规数 | 结论 |
|---|---|---|---|---|
| NORMAL | 2 | 8 | 0 | PASS |
| DEGRADED | 3 | 8 | 24 | FAIL |
| TIMEOUT | 0 | 8 | 0 | PASS |

未降级场景的硬约束违规数必须为 0 —— 即排序分数没有覆盖硬约束。

## 6. 全局降级矩阵

| 级别 | 场景 | 触发条件 | 降级路径 |
|---|---|---|---|
| L1 | 推荐系统 HTTP 不可用 | 连接失败/超时/未登录 | local_artifacts（读取发布制品）→ offline_snapshot（内置快照） |
| L2 | 推荐系统制品缺失 | 文件不存在 | offline_snapshot 内置快照 |
| L1 | 真实多模态模型不可用 | torch/mediapipe 缺失或未授权 | 离线场景桩（data/cases 中的模态输入） |
| L2 | ASR 文本为空/过短 | 有效时长 < 400ms 或文本长度 < 2 | 返回 E_EMPTY_TRANSCRIPT，不臆测意图 |
| L2 | RAG 无命中 | 最高相似度 < min_score | 仅用结构化字段解释，不编造 |
| L1 | 硬约束过滤后无结果 | candidates = 0 | 放宽为软约束 + 热门兜底，并标记 degraded=true |
| L2 | 语音呈现不可用 | TTS 合成失败/超时 | 仅文本呈现，voice_available=false |

> 矩阵唯一来源：config/settings.py 的 DEGRADATION_MATRIX，契约测试 01 会自检其完整性。

## 7. 复现口径

seed=42，排序键固定为 final_score desc → base_score desc → item_id asc。