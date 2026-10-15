# 在 VS Code 里开发这个项目

## 1. 打开

```powershell
code "C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project"
```

> 一定要打开 **integrated_ai_project** 这一层，不要打开它的上一级。
> 因为 `/workspaceFolder` 就是项目根，`config/`、`src/`、`scripts/` 都在这一层，
> Python 的 `from config import settings` 才能被正确解析。

首次打开时 VS Code 会提示安装推荐扩展（Python / Pylance / debugpy），点安装即可。
不装也能跑——本项目离线链路零第三方依赖。

## 2. 选解释器

`Ctrl+Shift+P` → **Python: Select Interpreter** → 选
`C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe`

> 已经写进 `.vscode/settings.json`，正常情况下会自动选中。
> 如果你建了虚拟环境，改成 `${workspaceFolder}/.venv/Scripts/python.exe`。

## 3. 跑任务（最常用）

`Ctrl+Shift+P` → **Tasks: Run Task**，或直接 **`Ctrl+Shift+B`** 跑默认任务。

| 任务 | 作用 |
|---|---|
| **★ 一键跑完整离线套件**（默认） | 依次跑 00~06、08、12 九个脚本，输出汇总表 |
| 环境检查 | 确认解释器 / 外部系统探测 / 离线快照 / 依赖状态 |
| 阶段1 ~ 阶段6 | 各阶段的证据生产脚本 |
| 文档生成 | 从 `outputs/` 证据重新生成 5 份文档 |
| 网页改版验收 | 10 项实测（需 webui 在跑 + Edge） |
| 在线联调 / 真机多模态 / 真实 LLM | 需外部服务或设备，见任务 detail |
| 运行 · 网页对话界面 | 启动 `webui.py` → http://127.0.0.1:8020 |
| 运行 · 终端对话 | 启动 `chat.py` |
| 清理缓存（打包前） | 删除 `__pycache__` / `*.pyc` |

## 4. 调试（F5）

`.vscode/launch.json` 里有 7 个配置，左侧「运行和调试」面板选好后按 F5：

- **① 一键端到端演示（main.py）** —— 最常用，能一路断进 `src/orchestrator.py` 看主链路
- **② 终端对话（chat.py）** —— 用集成终端启动，可以直接在里面打字对话
- **③ 网页对话界面（webui.py）** —— 启动后浏览器打开 :8020
- **④ 当前打开的文件** —— 写着哪个脚本就调哪个，改脚本时最方便
- **⑤/⑥ 阶段脚本** —— 单步跟契约校验与编排逻辑
- **⑦ 真实模式** —— 带 `INTEGRATED_ALLOW_REAL_MODELS=1`

**断点建议**（想看懂主链路时）：

| 文件 | 函数 | 看什么 |
|---|---|---|
| `src/orchestrator.py` | `handle()` | 一轮请求的完整推进顺序 |
| `src/intent.py` | `understand()` | 一句话如何变成 intent + slots |
| `src/recall.py` | `recall()` | 六路召回如何合并去重 |
| `src/ranking.py` | `hard_filter()` | 硬约束在哪里先于打分生效 |
| `src/explain.py` | `explain()` | 三道门控如何判定 |
| `src/llm_explain.py` | `evaluate_output()` | 生产链路与评测共用的统一入口 |

## 5. 中文乱码怎么办

`.vscode/settings.json` 已经在终端注入 `PYTHONIOENCODING=utf-8` 与 `PYTHONUTF8=1`，
所以**在 VS Code 集成终端里跑脚本不会乱码**。

如果你在外部 PowerShell 里跑看到 `��ģ̬` 这类乱码，先执行：

```powershell
$env:PYTHONIOENCODING = "utf-8"; $env:PYTHONUTF8 = "1"
```

## 6. 目录速览

```
integrated_ai_project/
├─ main.py / chat.py / webui.py   三个入口
├─ config/settings.py             唯一配置入口（改阈值/权重/词表都在这）
├─ src/                           领域实现：契约 → 适配器 → 意图/会话 → 召回/排序 → 证据/解释 → 编排
├─ scripts/                       00~13 证据生产与验收脚本
├─ data/                          offline 快照 / cases 用例 / kb 知识库
├─ docs/                          架构、协议、部署、风险、报告、手册、改版设计
└─ outputs/                       各阶段证据（文档里的数字都来自这里）
```

## 7. 常见问题

| 现象 | 处理 |
|---|---|
| `ModuleNotFoundError: config` | 打开的目录不对，要打开 `integrated_ai_project` 这一层 |
| 断点不生效 | 确认 `justMyCode: true` 下断点打在 `src/` 里的**自己写的**代码上 |
| 任务跑出来是乱码 | 检查是否用了外部终端；用 VS Code 集成终端 |
| `webui.py` 起不来提示端口占用 | `Get-NetTCPConnection -LocalPort 8020 -State Listen` 查占用进程 |
| 想改默认任务 | 改 `.vscode/tasks.json` 里带 `isDefault: true` 的那条 |
