# 阶段4 · 异常解释记录

> 阶段4 提交物 · 由 scripts/08_gen_docs.py 从证据文件自动生成，请勿手工编辑

- 生成时间：2026-10-15T06:01:40+00:00
- 数据来源：outputs/stage04_explanation_cases.jsonl、outputs/stage04_explanations.json
- 重新生成：先执行对应阶段脚本，再执行 python scripts/08_gen_docs.py

## 1. 总体结果

- 用例：**7/7 通过**
- 知识库：18 个片段 / 6 篇文档

## 2. 异常解释记录（预期内降级，非缺陷）

| 用例 | 类型 | 状态 | 错误码 | 解释文本 |
|---|---|---|---|---|
| E-02 | insufficient | no_evidence | E_NO_EVIDENCE | 暂无可核验依据，因此不做推荐解释。 |

说明：阶段4 的「异常」多为**预期内的降级**——
- 证据不足时返回 E_NO_EVIDENCE 并输出「暂无可核验依据，因此不做推荐解释」，这是不编造的正确行为；
- 商品字段缺失时自动降级为其余可用证据；
- 视觉标签置信度不足时降权后忽略，不进入解释证据。

## 3. 解释 ↔ 证据对照表

| 用例 | 商品 | 状态 | 证据数 | 证据类型 | 结论摘录 |
|---|---|---|---|---|---|
| E-01 | 85123A | ok | 9 | product_fact、product_fact、matched_constraint、matched_constraint、candidate_source、visual_tag、kb_snippet、kb_snippet、kb_snippet | 推荐理由：它属于「家居装饰」品类。参考价 £2.95（来源：historical |
| E-02 | EDGE-NO-FACTS | no_evidence | 0 |  | 暂无可核验依据，因此不做推荐解释。 |
| E-03 | 85123A | ok | 6 | product_fact、matched_constraint、candidate_source、kb_snippet、kb_snippet、kb_snippet | 推荐理由：它属于「家居装饰」品类。符合你的品类要求：家居装饰（要求 家居装饰）。 |
| E-04 | 85123A | ok | 6 | product_fact、product_fact、candidate_source、kb_snippet、kb_snippet、kb_snippet | 推荐理由：它属于「家居装饰」品类。参考价 £2.95（来源：historical |
| E-05 | 85123A | ok | 7 | product_fact、product_fact、candidate_source、visual_tag、kb_snippet、kb_snippet、kb_snippet | 推荐理由：它属于「家居装饰」品类。参考价 £2.95（来源：historical |
| E-06 | NOT-IN-CATALOG | item_not_found | None |  |  |
| E-07 | 85123A | ok | 8 | product_fact、product_fact、matched_constraint、matched_constraint、candidate_source、kb_snippet、kb_snippet、kb_snippet | 推荐理由：它属于「家居装饰」品类。参考价 £2.95（来源：historical |

## 4. 视觉标签置信度治理实测

| 用例 | 输入 | 处理 | 结果 |
|---|---|---|---|
| E-04 | 低置信视觉标签 0.48 | 有效置信度 0.24 < 0.55，被忽略 | visual_used 为空 |
| E-05 | 高置信视觉标签 0.88 | 作为证据引用并标注置信度与来源 | visual_used = [白色] |

## 5. 结论

每条解释都至少含一项实质性可核验证据；无证据场景明确失败而非编造；价格与目录不一致时按目录口径修正并给出提示。