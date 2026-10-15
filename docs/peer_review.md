# 相邻小组复现指引与问题记录

> 六个阶段的「步骤4」都要求：**相邻小组按 README 重跑一个用例并记录问题**。
> 本文给出一条命令的最小复现路径，并附问题记录表。

## 1. 复现前置（约 2 分钟）

```powershell
cd C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project
python scripts/00_check_env.py
```

期望输出（关键几行）：

```
运行模式        : offline（allow_real_models=False）
离线快照        : 商品 3642 件 / 用户 736 个 / 类目 23 个
推荐适配器      : offline_snapshot（降级 0 次）
多模态适配器    : offline_stub（降级 0 次）
```

> **默认完全离线**：不需要网络、不需要启动外部服务、不加载任何重型模型。
> 只要这几行符合预期，后续复现就不依赖外部环境。

## 2. 每阶段一条最小复现命令

| 阶段 | 命令 | 该看什么 | 期望 |
|---|---|---|---|
| 1 | `python scripts/01_contract_test.py` | 末尾「合计 N/N 通过」 | `25/25 通过（100%）` |
| 2 | `python scripts/02_context_test.py` | Part A 与 Part B 的通过率 | `26/26`、`10/10` |
| 3 | `python scripts/03_orchestrator_demo.py` | 四条链路与验收清单 | NORMAL/EMPTY/DEGRADED/TIMEOUT 全部 PASS |
| 4 | `python scripts/04_explanation_test.py` | 7 个用例 + 文本/语音呈现 | 全 PASS；语音为文本摘录 |
| 5 | `python scripts/05_feedback_ab.py` | 案例类型分布 | 成功/无变化/反向变化**三类齐备** |
| 6 | `python scripts/06_e2e_acceptance.py` | 按用例类型 + 验收清单 | 成功/边界/失败齐备；隐私扫描 PASS |
| 12 | `python scripts/12_text_norm_check.py` | 末尾合计 | `10/10` |

### 只挑一个用例手验（示例：阶段5 的反向变化）

```powershell
python -c "from src.orchestrator import Orchestrator; from src.feedback import run_ab; o=Orchestrator(with_voice=False); d=run_ab(o,{'id':'x','user_id':'15547','query':{'source':'text','text':'只看派对用品'},'feedback':{'polarity':'positive','target':'top1'}}).data; print(d['case_type'], d['case']['target_rank_before'], '->', d['case']['target_rank_after']); print('; '.join(d['ranking_change_explanations']))"
```

期望：`反向变化 1 -> 2`，并给出变化原因（相似商品命中场景约束反超）。

### 验证降级（阶段3/6）

```powershell
python -c "from config import settings; from src.orchestrator import Orchestrator; settings.RECALL_CONFIG['sources_enabled']=[]; r=Orchestrator(with_voice=False).handle({'source':'text','text':'推荐装饰品'},user_id='15547').data; print(r['status'], (r.get('error') or {}).get('code'))"
```

期望：`empty E_NO_RESULT`（所有召回来源不可用 → 明确空结果，不是空数组）。

## 3. 期望 vs 实际（复现判据）

复现通过的最低标准：

1. 脚本退出码为 0；
2. 打印的通过数与上表一致；
3. `outputs/` 下对应证据文件的修改时间为本次运行；
4. 缓存被业务规则排除（不出现 `__pycache__` 进包）。

任一条不符，按下面表格记录。

## 4. 问题记录表

| # | 阶段 | 复现命令 | 期望 | 实际 | 复现次数 | 定位过程 | 处理 |
|---|---|---|---|---|---|---|---|
| 1 | | | | | | | |
| 2 | | | | | | | |
| 3 | | | | | | | |

> 记录规则（对应任务书「不得只修改报告文字」）：
> 每个问题必须能在**修复后重新运行同一命令**并给出前后两次的输出差异；
> 只改报告文字、不重跑脚本的，视为未修复。

## 5. 常见差异与排查

| 现象 | 原因 | 处理 |
|---|---|---|
| 阶段3 出现 `DEGRADED` | 硬约束过滤后无候选，触发降级 | 属预期，看 `degrade_reason`；若输入含 `0.01以内` 这类不可能约束即为正常 |
| 阶段5 反向变化数量与本文不同 | 反馈链路含采样性；且候选池组成会随会话焦点变化 | 以 `case_type` 字段为准，三类齐备即可 |
| 阶段6 隐私扫描 `FAIL` | 运行脚本后产生了 `__pycache__` | `python scripts/clean_workspace.py` 后重跑 |
| 阶段4 语音为 `None` | 未安装 `edge-tts` 且 Windows SAPI 不可用 | 属 L2 降级，`presentation.degradations` 里会有记录 |
| 商品图为类目图标 | 推荐系统未启动且该商品未缓存过图片 | 属预期降级；启动 `:8010` 或预缓存即可 |
