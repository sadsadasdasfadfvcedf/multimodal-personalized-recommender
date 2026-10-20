# 多模态个性化推荐助手（integrated_ai_project）

把《行业工程实践》的 **推荐系统 v146** 与 **智能多模态AI助手** 两套独立系统，
整合为通过稳定接口协作的工程系统：语音/视觉/手势 → 意图理解 → 多路召回 → 可控精排 →
可核验解释 → 文本/语音呈现 → 点赞反馈闭环。

```text
语音:「我要给朋友挑一个结婚礼物，500以内」
  → InteractionEvent → 意图 filter（装饰品 / 婚礼 / ≤£500）
  → 多路召回（个性化 + 相似 + 品类 + 历史 + 热门）→ 硬约束过滤 → 上下文重排
  → EvidencePack → RAG 解释（每条解释都有可核验证据）→ 文本 + 语音播报
手势:👍 → 写入会话偏好（正负互斥）→ 重新排序 → 反馈前后 TopK 对照
文本:「为什么推荐第一个」→ 序号指代解析 → 生成带证据的解释
```

---

> **完整说明手册**：`docs/manual.md`（11 章 / 18 表）—— 项目是什么、能做什么、架构、六阶段交付、目录结构、安装运行、输入输出、实测数据、限制、降级、提交物，一册读完。本文（README.md）是面向开发的精简版。

## 0. 在 VS Code 里开发

```powershell
code "C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project"
```

`.vscode/` 已配好，开箱即用：

| 文件 | 内容 |
|---|---|
| `settings.json` | 解释器路径、`python.analysis.extraPaths`（让 `from config import ...` 可解析）、**终端注入 `PYTHONIOENCODING=utf-8`**（中文日志不乱码）、排除 `__pycache__` |
| `tasks.json` | 21 个任务：**`Ctrl+Shift+B` 一键跑完整离线套件**，以及各阶段脚本、三个入口、清理与验收 |
| `launch.json` | 7 个调试配置（F5）：入口程序、当前文件、阶段脚本、真实模式；`chat.py`/`webui.py` 用集成终端启动以支持键盘输入 |
| `extensions.json` | 推荐扩展：Python / Pylance / debugpy（不装也能跑） |
| `README.md` | 使用说明、断点建议、乱码排查 |

一键跑全套（等价于 VS Code 里的默认任务）：

```powershell
python scripts/run_all_offline.py          # 9 个脚本，约 24 秒，末尾出汇总表
```

中文 cmd / PowerShell 里可以直接运行：脚本启动时会把控制台代码页切到 UTF-8（65001），
不会再因为 `▶` / `✅` 这类字符抛 `UnicodeEncodeError`。

详细用法见 **`.vscode/README.md`**。

## 1. 快速开始

```powershell
cd C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project

python scripts/00_check_env.py      # 运行环境检查
python webui.py                     # ★ 网页对话界面 → http://127.0.0.1:8020
python chat.py                      # ★ 终端对话
python main.py                      # 一键端到端演示（含语音播报）
python main.py --no-voice           # 不合成语音
```

**默认完全离线**：不需要网络、不需要启动外部服务、不加载任何重型模型。

## 1.1 网页对话界面（`webui.py`）

```powershell
python webui.py                    # → http://127.0.0.1:8020
python webui.py --real --voice     # 真实麦克风/摄像头 + 语音播报
python webui.py --real --llm       # 再用真实 LLM 生成解释
python webui.py --port 8030        # 换端口
```

页面按**电商平台**的形态组织：顶部搜索、左侧商品货架、右侧推荐助手。

```
┌──────────────────────────────────────────────────────────────┐
│ [拾]  …………………………… 搜索框 ……………………………           [搜索]      │
│ [🎤语音][👁画面] │ [↩撤销][⟳重来][ℹ状态][LLM]                  │
├──────────────────────────────────┬───────────────────────────┤
│ 筛选结果 · 婚礼 · 装饰品 ≤£500     │ 推荐助手                   │
│ ┌────┐┌────┐┌────┐┌────┐┌────┐  │  · 对话流                  │
│ │ 图 ││ 图 ││ 图 ││ 图 ││ 图 │  │  · 状态/意图/槽位           │
│ │价格││价格││价格││价格││价格│  │  · 操作卡（进行中/成功/失败） │
│ └────┘└────┘└────┘└────┘└────┘  │  · 语音播放                │
│  点图看详情 · 赞/踩常驻在卡片上     │                           │
└──────────────────────────────────┴───────────────────────────┘
```

**主交互是「说一句，货架就更新」**：你在搜索框里说需求，助手在右侧回应，
左侧货架换成新的商品集合。

| 操作 | 说明 |
|---|---|
| 顶栏搜索框 | 输入需求 → 回车/搜索；货架即时更新，页头显示「筛选结果 · 婚礼 · 装饰品 · 预算 ≤ £500」 |
| 商品卡 | 方形大图 + 排名角标 + 价格 + 推荐分 + 命中约束/证据条数标记 |
| 商品卡右上角 | 👍/👎 常驻，不用悬停；反馈只锁当前卡片并乐观更新，失败自动回滚 |
| 点商品图/标题 | 详情弹窗：大图、中英标题、参考价、价格来源、历史成交、推荐理由、证据来源、喜欢/不喜欢、加入对比、追问此商品 |
| 助手侧栏 | 对话流：状态徽标 + 意图 + 槽位 + 语音播放 + 耗时 |
| 🎤 语音 / 📷 识图推荐 | 真实设备输入（需 `--real`）；举起物品即可识别主要品类/颜色并更新商品推荐；手势只在商品详情内自动开启 |
| ↩ 撤销 / ⟳ 重来 / ℹ 状态 / LLM 解释 | 会话与解释控制 |

首次打开即展示「猜你喜欢」（用**独立会话**跑默认推荐，不污染对话历史）。

当前页面只做阶段 1~6 的导购闭环：**推荐 → 详情/证据 → 对比 → 点赞/倒赞 → 追问 → 反馈前后对照**。
不提供购物车、结算、支付、订单、物流、地址、会员、优惠券或独立收藏夹；收藏用点赞表达。
对比需要先选 2~4 件商品，不足 2 件时页面会给出可见提示。

| 操作 | 说明 |
|---|---|
| 输入框打字 → 发送 | 文本对话 |
| 🎤 语音 | 录 4 秒真实麦克风 → 常驻 ASR → 对话（需 `--real`；模型启动时后台预加载） |
| 📷 识图推荐 | 抓帧识别画面中央的主要物品，结合品类/颜色直接更新货架（需 `--real`） |
| ↩ 撤销 / ⟳ 重来 / ℹ 状态 | 会话控制 |
| LLM 解释 按钮 | 一键切换真实 LLM（需 `--real`） |

**手势评价**（需 `--real`）：主页面不提供手势按钮。打开商品详情会自动开启手势直播，
摄像头保持打开并在弹窗内实时预览，只识别 👍/👎 并作用于**当前打开的商品**；
其他手势会被忽略。再次比同一手势会取消该商品的点赞/倒赞。
倒赞后商品会立即从货架移除，取消倒赞后重新出现。
倒赞后商品卡片消失时，可在助手消息里点「撤销倒赞」恢复并取消反馈。
点「停止」或关闭弹窗会释放摄像头；直播不可用时可在详情内点「改用单次识别」。

**开放问答（普通对话）**（需 `--real`；配置在线 Key 后离线模式也可对话）：

- 概念解释、写作、翻译、计算等非购物问句走**常驻 Qwen3-4B**，不返回商品；
  购物需求仍走推荐链路，寒暄 / 身份问答仍是 0 ms 固定文案；
- **在线模型优先（可选）**：设置下面任意一个 Key 后，对话与 LLM 解释会优先走在线
  OpenAI 兼容接口，失败自动回退本地 Qwen；不设置 Key 时行为与原来完全一致。

  ```powershell
  # 方式一：厂商专用 Key（自动识别）
  $env:DASHSCOPE_API_KEY = "sk-..."          # DashScope / 通义千问
  # 或 $env:DEEPSEEK_API_KEY = "sk-..."      # DeepSeek
  # 或 $env:OPENAI_API_KEY = "sk-..."        # OpenAI

  # 方式二：任意 OpenAI 兼容网关
  $env:INTEGRATED_ONLINE_LLM_API_KEY = "sk-..."
  $env:INTEGRATED_ONLINE_LLM_BASE_URL = "https://your-gateway/v1"
  $env:INTEGRATED_ONLINE_LLM_MODEL = "qwen-plus"

  # 强制本地 / 在线严格模式（默认 auto：在线失败回退本地）
  $env:INTEGRATED_LLM_PROVIDER = "auto"      # auto | online | local
  $env:INTEGRATED_ONLINE_LLM_STRICT = "0"    # 1 = 在线失败不再回退本地

  python scripts/19_online_llm_check.py      # 实测联网、模型与延迟
  ```

  也可以直接编辑项目根目录的 `local.env`（已被 `.gitignore` 忽略）：

  ```dotenv
  INTEGRATED_LLM_PROVIDER=deepseek
  DEEPSEEK_API_KEY=sk-...
  ```

  项目启动时会自动读取 `local.env`；`local.env.example` 是可提交的模板。DeepSeek 默认使用
  `https://api.deepseek.com/v1` 与 `deepseek-flash`（DeepSeek-V4.1-Flash）。
  官方 2026-10-17 的 `V4.1-Flash-1017` 是新版本名，但 API 可选择 ID 仍是
  `deepseek-flash`，不要把 `V4.1-Flash-1017` 直接填进 `INTEGRATED_ONLINE_LLM_MODEL`。

  **DeepSeek Search / Fetch 已接入**：带「最新 / 今天 / 搜索 / 版本 / 文档 / URL」等时效性
  问题会自动先调用官方搜索接口，把标题、URL、日期和摘要注入模型，回答中会附来源。
  搜索按成功请求计费（每 1000 次 5 元），购物推荐不会触发搜索；不需要时可关闭：

  ```dotenv
  INTEGRATED_WEB_SEARCH=1          # 0 = 关闭自动搜索
  INTEGRATED_WEB_SEARCH_COUNT=5    # 每次返回 1~50 条
  INTEGRATED_WEB_SEARCH_CONTENT=0  # 1 = 同时抓取网页正文
  INTEGRATED_WEB_SEARCH_TIMEOUT_S=30
  ```

  模型选择、接口地址和超时集中在 `config/settings.py` 的 `CHAT_CONFIG["online"]`；
  Key 只从环境变量读取，不写入仓库、日志或证据文件。页面 ⚙ 状态面板会显示
  「对话后端：在线模型 / 本地 Qwen / 未启用」与「联网搜索：开 / 关」。
- 模型只在第一次对话时加载（约 25~40 秒），之后同一进程复用，多轮上下文保留
  （实测第二轮 4.2 秒）；
- 点击「⟳ 重来」或切换右上角用户会清空对话历史；
- 模型不可用时该轮 `status=degraded`，页面显示固定提示，推荐功能不受影响；
- 超时用 `INTEGRATED_CHAT_TIMEOUT_S`（默认 180 秒）调整，模型目录可用
  `MULTIMODAL_LLM_PATH` 覆盖。接口为 `POST /api/chat {"text": "..."}`。
- 语音改为常驻 ASR worker（SenseVoiceSmall）：启动时后台预加载，之后每轮只付推理时间；
  实测预加载完成后第一次 9.97 秒、第二次 4.67 秒（均含 4 秒录音）。超时用
  `INTEGRATED_ASR_TIMEOUT_S`（默认 180 秒）调整。
- 手势直播用常驻摄像头 worker：摄像头只打开一次，网页用 `/camera/stream` 看 MJPEG 预览，
  `/api/camera/state` 轮询手势事件；摄像头序号/帧率用 `INTEGRATED_CAMERA_INDEX` /
  `INTEGRATED_CAMERA_FPS` 调整。
- **性能优化**：网页推荐默认不再自动合成 TTS，先返回结果（实测 0.07s），需要时点
  「🔊 语音播报」再合成；真实 LLM 解释复用常驻 Qwen3-4B 并批量生成，
  预加载后 3 条解释约 8s（旧实现每件商品都重新加载权重）。

**页面改版**（依据《页面改版设计README》，当前网页验收 14/14）：

| 改动 | 说明 |
|---|---|
| 顶栏单行 56px | 品牌 + 搜索 + 说明 chips + 用户菜单 + ⚙；运行模式移入 ⚙ 面板，不再占用首屏 |
| **横向溢出根治** | 1180 断点内收起品牌文字与 chips；搜索框 `min-width:0` 可收缩。实测 8 档视口（496~1440px）**零溢出**（原版 674px 溢出 115px、742px 溢出 47px） |
| 结果条 ResultBar | 筛选条件变成**可单独移除**的 chip（场景·婚礼 ✕），加排序下拉与网格/列表切换 |
| 商品卡 | `TOP N` 缎带、**👍/👎 常驻**（触屏可达）、`56% 匹配`（替代模型原始分）、标签最多 2 个 + `+N`、**加入对比**按钮 |
| 商品对比 | 底部升起对比栏 → 字段对照表，**差异单元格高亮** |
| 反馈响应 | 只锁当前商品（局部 pending）；点赞/倒赞可再次点击取消；倒赞即时移出货架 |
| 商品详情 | 左图右信息，展示推荐理由、证据、价格来源、喜欢/不喜欢、加入对比、追问此商品 |
| 助手气泡 | 状态徽标改「圆点 + 图标 + 中文」（完成/降级返回/需要补充信息）、意图中文化、**槽位点击即改条件**、运行细节默认 `▸ 展开依据` 折叠 |
| 失败有出路 | 语音/手势/画面失败时给「重试一次 / 改用打字 / 怎么用？」 |
| 窄屏换组织方式 | ≤1023px 助手变抽屉 + 悬浮按钮；≤639px 加底部 Tab 栏（货架/助手/状态） |
| 术语翻译 | `historical_mode` → 历史成交价；`filter` → 条件筛选；`分 0.5803` → `58% 匹配` |
| 加载骨架 | 首屏 4 个占位卡 + 骨架气泡，替代空白等待 |

商品展示（把原推荐系统页面的视觉能力并进对话流）：

- **推荐卡带商品图**：卡片左侧是真实商品照片，数据来自外部推荐系统的公开接口
  `/api/image/{item_id}`（该接口无鉴权），由本项目 `webui.py` 代理并**本地缓存**到
  `artifacts/images_cache/`，因此推荐系统未启动时图片依然可显示；
- **点击卡片看详情**：弹窗展示大图、中英标题、商品编号、类目、参考价、价格来源、历史成交笔数；
- **详情聚焦当前商品**：不额外加入“相似外观”下钻，避免把六阶段主链路扩成无边界浏览；详情只保留推荐理由、证据、对比和追问；
- **加载即有兜底**：类目图标垫在图片下层，图片未加载完或加载失败时显示图标，不出现空灰块；
- **显示数量可下拉**：货架右上角可选「显示 8 / 16 / 24 / 40 件」，切换后按当前需求重新取更多商品
  （首页走 `/api/home?limit=N`，对话结果走 `/api/more`），不污染对话历史；
- **滚动到底自动续载**：滑到货架底部会自动追加 12 件（8 → 20 → 32 …，上限 120 件）；
  推荐系统候选池不足 40 件之后，用商品目录热门补齐并标注「商品目录补充」；

界面设计要点（都是为了解决实际使用中暴露的问题）：

- **系统动作不伪装成用户发言**：🎤/👁 的结果渲染为独立的**操作卡**，卡内区分
  「进行中（转圈）/ 已完成 / 未识别 / 失败」四种状态，不再出现「你：录音识别中…」这种错位气泡；
- **失败给可执行的建议**：手势未识别时提示正确比法（只支持 👍 点赞 · 👎 倒赞），
  并在操作卡里直接显示**这次抓拍的画面**（点击可看大图），方便对照调整手的位置；
- **推荐理由默认折叠**：8 张卡片不再被长解释淹没，点「展开推荐理由」才显示；
- **离线模式禁用无关按钮**：未加 `--real` 时 🎤/👁 直接置灰并说明原因，避免点了才报错；
- **卡片信息密度**：排名徽标 + 价格 + 推荐分 + 来源 chips + 证据条数标记 + 状态徽标 + 耗时。

> **端口说明**：`:8020` 是**本项目**的对话界面（Python 标准库实现，零新增依赖）；
> `:8010` 是**外部推荐系统**自带的点击式推荐流，两者互不相干。

## 1.2 终端对话（`chat.py`）

不想开浏览器就用终端：

```powershell
python chat.py                      # 离线文本对话（毫秒级）
python chat.py --voice              # 开启语音播报
python chat.py --real               # 真实多模态：/voice 录音识别、/gesture 手势、/vision 画面理解
python chat.py --real --llm         # 再用真实 LLM 生成解释（每条约 +10~25 秒）
python chat.py --user 14156         # 换用户（15547 林先生 / 14156 陈小姐 / 17231 王女士）
```

输入方式：

| 输入 | 说明 |
|---|---|
| 直接打字 | 「500以内的婚礼装饰品」「和这个相似的商品」「为什么推荐第一个」 |
| `/voice [秒数]` | 录真实麦克风 → ASR 识别 → 送进对话（需 `--real`） |
| `/gesture` | 抓真实画面 → 手势识别；👍→点赞、👎→倒赞（需 `--real`） |
| `/vision [问题]` | 抓真实画面 → VL 描述 → 作为视觉标签送进对话（需 `--real`） |

会话命令：`/undo` 撤销上一轮 · `/reset` 新开会话 · `/state` 看当前槽位与反馈 ·
`/llm on|off` 切换真实 LLM · `/say on|off` 切换语音播报 · `/mode` · `/help` · `/quit`

> 说明：`main.py` 是**写死的三轮演示脚本**，用于验收取证；日常对话请用 `chat.py`。
> 若需图形界面，用外部 `multimodal_ai` 的 PyQt5 窗口（需可见窗口与设备授权）。

## 2. 安装

离线演示链路 **零第三方依赖**（仅标准库）。以下依赖存在时会自动增强，缺失则自动降级：

```powershell
python -m pip install -r requirements.txt
```

| 依赖 | 作用 | 缺失时的行为 |
|---|---|---|
| `numpy` | 数值处理 | 使用纯 Python 实现 |
| `jieba` | 中文分词（RAG 检索） | 退化为字符二元组 |
| `requests` | 推荐系统 HTTP 模式 | 跳过 HTTP 策略，使用本地制品/内置快照 |
| `edge-tts` | 在线语音合成 | 回退 Windows SAPI（System.Speech → SAPI COM，离线）；无语音包/无音频会话时回退纯文本（`00_check_env.py` 会预检） |
| `zhconv` | 繁体转简体（LLM 输出规范化） | 仅做检测，检出繁体即回退模板解释 |

### 2.1 真实多模态依赖（版本必须配套）

`--real` / `--mode auto` 会用到 mediapipe、torch、torchvision、transformers。这组依赖**硬耦合**，装错时错误信息不会指向根因：

| 现象 | 根因 | 正确组合 |
|---|---|---|
| `'MessageFactory' object has no attribute 'GetPrototype'` | protobuf 版本过高 | mediapipe 0.10.14 要求 `protobuf>=4.25.3,<5` |
| `operator torchvision::nms does not exist` | torchvision 与 torch 不配套 | torchvision 0.21.0 要求 `torch==2.6.0` |
| `Could not import module 'AutoProcessor'` | 上一条的连锁反应 | 先修 torchvision |

```powershell
# CUDA 12.4
python -m pip install torch==2.6.0 torchvision==0.21.0 --index-url https://download.pytorch.org/whl/cu124
python -m pip install "protobuf>=4.25.3,<5" mediapipe==0.10.14 opencv-python>=4.8 transformers>=4.57
```

演示前跑一次深检（不联网）：

```powershell
python scripts/15_deps_check.py
```

`00_check_env.py` 每次都会做**快速版**检查（只读包元数据），发现问题会直接打印，并写入 `outputs/env_check.json` 的 `dependency_compat`。

## 3. 运行

### 3.1 分阶段证据生产（推荐验收顺序）

| 命令 | 阶段 | 产物 |
|---|---|---|
| `python scripts/00_check_env.py` | 环境 | `outputs/env_check.json` |
| `python scripts/01_contract_test.py` | 1 契约与降级矩阵 | `outputs/stage01_contract_report.json` |
| `python scripts/02_context_test.py` | 2 意图与会话 | `outputs/stage02_context_cases.jsonl`、`stage02_context_metrics.json` |
| `python scripts/03_orchestrator_demo.py` | 3 编排/召回/精排 | `outputs/stage03_demo.json`、`stage03_traces.jsonl` |
| `python scripts/04_explanation_test.py` | 4 证据与解释 | `outputs/stage04_explanation_cases.jsonl`、`stage04_explanations.json` |
| `python scripts/05_feedback_ab.py` | 5 反馈 A/B | `outputs/stage05_feedback_ab.json` |
| `python scripts/06_e2e_acceptance.py` | 6 端到端与验收 | `outputs/stage06_acceptance.json`、`e2e_run_log.jsonl` |
| `python scripts/07_live_integration_check.py` | 在线联调（可选） | `outputs/live_integration.json` |
| `python scripts/08_gen_docs.py` | 文档生成（需先跑 02~05） | `docs/*.md`、`outputs/docs_manifest.json` |
| `python scripts/09_real_multimodal_check.py` | 真机多模态（需设备+权重，可选） | `outputs/real_multimodal_check.json`、`artifacts/real/` |
| `python scripts/10_real_llm_check.py` | 真实 LLM 解释链路（可选） | `outputs/real_llm_check.json` |
| `python scripts/11_llm_model_compare.py` | LLM 选型对比（可选） | `outputs/llm_model_compare.json` |
| `python scripts/12_text_norm_check.py` | 文本规范化与流畅度门控 | `outputs/text_norm_check.json` |
| `python scripts/14_asr_quality_check.py` | ASR 识别质量评测（需权重，可选） | `outputs/asr_quality_check.json`、`artifacts/asr_eval/` |
| `python scripts/15_deps_check.py` | 真实多模态依赖深检（可选，不联网） | `outputs/deps_check.json` |
| `python scripts/16_webui_click_path_check.py` | 网页点击路径回归（需 webui 在跑 + Edge） | `outputs/webui_click_path.json` |
| `python scripts/17_browser_qa.py` | 浏览器运行时 QA：console / 网络 / CWV / 无障碍（需 Edge） | `outputs/browser_qa.json`、三档截图 |
| `python scripts/18_contract_check.py` | 网页 API 契约核验（protocol §10） | `outputs/contract_check.json` |
| `python scripts/19_online_llm_check.py` | 在线 LLM 联通性与延迟（需 Key，可选） | `outputs/online_llm_check.json` |
| `python scripts/19_security_check.py` | 网页服务安全基线与输入边界 | `outputs/security_check.json` |
| `python scripts/21_deepseek_search_check.py` | DeepSeek Search/Fetch 实测（需 Key，可选） | `outputs/deepseek_search_check.json` |
| `python scripts/20_fingerprint_stability.py` | 连续两轮离线套件，验证证据指纹可重复 | 控制台结果 |
| `python scripts/13_webui_acceptance.py` | 网页改版验收（需 webui 在跑 + Edge） | `outputs/webui_acceptance.json` |
| `python scripts/clean_workspace.py` | 打包前清理 | 删除 `__pycache__` / `*.pyc` |

### 3.2 运行模式

```powershell
python main.py --mode offline   # 默认：只用内置离线快照（可离线演示）
python main.py --mode auto      # 尝试真实模型/外部服务，不可用则逐级降级
python main.py --mode online    # 强制走推荐系统 HTTP 接口
python main.py --user 14156     # 指定演示用户
```

### 3.3 与外部系统联调（可选）

```powershell
# 启动推荐系统（127.0.0.1:8010）
cd C:\Users\Administrator\Desktop\推荐系统\v146_release
.\run_frontend.ps1 -Install

# 回到本项目，启用 HTTP 模式（账号/口令只走环境变量，不写入任何文件）
$env:INTEGRATED_MODE = "auto"
$env:INTEGRATED_DEMO_USER = "<演示账号>"
$env:INTEGRATED_DEMO_PASSWORD = "<演示口令>"
python main.py
```

推荐系统根目录与多模态助手根目录会自动探测；
也可用 `INTEGRATED_RECOMMENDER_ROOT` / `INTEGRATED_MULTIMODAL_ROOT` 显式指定。
完整环境变量表见 `docs/deployment.md`。

服务起来之后，用下面这条命令验证集成层的 HTTP 适配器确实对接上了：

```powershell
python scripts/07_live_integration_check.py
# → active_mode=http，HTTP 候选 8 条，三轮主链路 ok，并给出
#   「HTTP 模式 vs 内置快照模式」同一查询的 TopK 对照
```

该脚本在服务未启动或未配置账号时会记录「跳过」并正常退出，不影响离线可复现性。

## 4. 输入与输出

### 输入

| 方式 | 格式 | 示例 |
|---|---|---|
| 语音 | `{"source": "voice", "text": "<ASR 文本>"}` | `"500以内的白色相框"` |
| 视觉 | `{"source": "vision", "text": "...", "visual_tags": [{"tag": "白色", "confidence": 0.9}]}` | 画面标签补充颜色/物体/场景 |
| 手势 | `{"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.92}}` | 👍 / 👎 → 正/负反馈 |
| 文本 | `{"source": "text", "text": "和这个相似的商品"}` | 支持序号指代「第一个」 |

### 输出

统一响应信封（关键字段）：

```json
{
  "trace_id": "tr-...", "user_id": "15547", "timestamp": "...", "source": "voice",
  "status": "ok | degraded | clarification | conflict | empty | no_evidence | error",
  "intent_result": {"intent": "filter", "slots": {...}, "missing_slots": [], "conflicts": []},
  "recommendations": [{"rank": 1, "item_id": "...", "title": "...", "price": 2.1,
                       "score": 0.723, "sources": ["..."], "explanation": "推荐理由：…"}],
  "presentation": {"text": "...", "voice_available": true, "audio_path": "..."},
  "trace": {"latency_ms": {...}, "modes": {...}, "degradations": {...}, "recall": {...}, "ranking": {...}},
  "error": null
}
```

## 5. 项目结构

```text
integrated_ai_project/
├─ main.py                    一键端到端演示
├─ config/settings.py         唯一配置入口（路径/词表/权重/降级矩阵/隐私策略）
├─ src/
│  ├─ contracts.py errors.py utils.py      契约、错误码、工具
│  ├─ adapters/                            两个外部系统的唯一接口层
│  ├─ catalog.py rag.py                    离线数据访问、BM25 检索
│  ├─ intent.py session.py                 意图与槽位、会话上下文
│  ├─ recall.py ranking.py                 多路召回、硬约束过滤与重排
│  ├─ evidence.py explain.py presentation.py  证据包、解释、文本/语音呈现
│  ├─ orchestrator.py                      主链路编排与 trace
│  ├─ feedback.py privacy.py               反馈 A/B、隐私与包体扫描
├─ .vscode/                    VS Code 配置（任务 / 调试 / 设置 / 扩展建议）
├─ scripts/                    00~13 阶段脚本 + 一键跑全套 + 快照导出 + 清理
├─ data/offline/               离线快照（商品目录/分数/相似/热门/历史）
├─ data/cases/                 阶段 2/4/5 用例数据
├─ data/kb/                    知识库（RAG 检索源）
├─ docs/                       架构、协议、部署、风险登记、项目报告
└─ outputs/                    各阶段证据文件
```

## 5.1 相邻小组复现指引

六个阶段的步骤4 都要求「相邻小组按 README 重跑一个用例并记录问题」。
完整指引（含每阶段一条最小命令、期望输出、问题记录表）见 **`docs/peer_review.md`**。

最短路径：

```powershell
python scripts/00_check_env.py     # 环境自检（应显示 offline / 3642 件商品 / 0 次降级）
python scripts/05_feedback_ab.py   # 任选一个阶段重跑
```

判据：脚本退出码 0、打印的通过数与 `docs/peer_review.md` 中的期望一致、
`outputs/` 下证据文件的修改时间为本次运行。

## 6. 文档清单（对应任务书点名的文档类提交物）

| 任务书要求 | 文件 | 内容 |
|---|---|---|
| 阶段1 证据 · 系统上下文/容器/时序/数据流 | `docs/architecture.md` | §1 上下文图、§2 容器图、§3 时序图、§4 数据流与失败出口、§5 降级矩阵、**§7 正常样例与边界样例** |
| 阶段1 证据 · 统一事件与请求响应 Schema | `docs/protocol.md` | 四类契约、六类意图与必选槽位、27 个错误码、适配器接口与边界约束 |
| 各阶段收尾 · README 五项 | `README.md` | §2 安装、§3 运行、§4 输入输出、§7 已知限制、§8 降级方式 |
| 阶段2 提交物 · 错误案例分析 | `docs/error_case_analysis.md` | 通过率、未通过明细、5 个已修复错误的根因与验证 |
| 阶段3 提交物 · 超时与降级说明 | `docs/timeout_and_degradation.md` | 四类 trace、超时/空结果/降级口径、硬约束核验、降级矩阵 |
| 阶段4 提交物 · 异常解释记录 | `docs/explanation_anomalies.md` | 异常记录、解释↔证据对照表、置信度治理实测 |
| 阶段5 提交物 · 性能与失败案例摘要 | `docs/performance_and_failures.md` | 延迟、反馈前后对照、失败/无变化案例、会话隔离复核 |
| 阶段6 提交物 · 部署与风险文档 | `docs/deployment.md`、`docs/risk_register.md` | 依赖与启动说明、风险登记表、演示用例清单、验收记录 |
| 各阶段步骤4 · 相邻小组复现 | `docs/peer_review.md` | 每阶段最小复现命令、期望输出、问题记录表 |
| 项目说明与使用手册 | `docs/manual.md` | 11 章完整手册：是什么 / 能做什么 / 架构 / 六阶段交付 / 目录结构 / 安装运行 / 输入输出 / 实测数据 / 限制 / 降级 / 提交物 |
| 阶段6 提交物 · 课程总项目报告 | `docs/project_report.md` | 需求/架构/实现/实验/问题/结论/成员贡献/参考资料 + 各阶段证据路径 |

> `error_case_analysis.md`、`timeout_and_degradation.md`、`explanation_anomalies.md`、
> `performance_and_failures.md` 以及 `architecture.md` 的样例章节，全部由
> `python scripts/08_gen_docs.py` 从 `outputs/` 证据文件自动生成 —— 证据一重跑，文档数字随之更新，
> 从机制上避免「只修改报告文字」。

## 7. 验收对照

| 阶段 | 关键验收项 | 结论 | 证据 |
|---|---|---|---|
| 1 | 接口边界 / 信封字段 / 明确错误码 / 离线可跑 | 25/25 通过 | `outputs/stage01_contract_report.json` |
| 2 | 不臆测缺失槽位 / 👍👎 映射 / 冲突检测 / 样例 ≥80% | 26/26 + 会话 12/12 | `outputs/stage02_context_metrics.json` |
| 3 | 来源可追溯 / 硬约束不被覆盖 / 空结果提示 / 可复现 | 6/6 通过 | `outputs/stage03_demo.json` |
| 4 | 每条解释有证据 / 无证据不编造 / 价格一致 / 低置信降权 | 7/7 通过 | `outputs/stage04_explanation_cases.jsonl` |
| 5 | 变化可解释 / 不跨会话污染 / 口径一致 / 含无变化案例 | 6/6 通过 | `outputs/stage05_feedback_ab.json` |
| 6 | 三类链路 / 故障降级 / 数字可追溯 / 无敏感数据 | 4/4 通过 | `outputs/stage06_acceptance.json` |

## 8. 已知限制

1. 离线快照中的个性化分数是推荐系统 v146 在样本候选集上的**离线打分**，不是在线实时推理；
2. 相似索引由商品标题/描述/类目离线生成，对同义不同名的商品召回有限；
3. 意图理解为**规则 + 词表**方案，未使用学习型分类器（阶段文档允许的进阶项）；
4. RAG 使用纯 Python BM25，未接入 multimodal_ai 的 BGE + FAISS（离线优先的取舍）；
5. 真实 LLM 解释已接入并实测达标：**Qwen3-4B-Instruct-2507 门控通过率 100%（4/4）**，
   Qwen2-1.5B 为 50%（会编造价格数字）。LLM 输出须依次通过**事实门控、格式门控、流畅度门控**三关
   才替代模板，否则回退模板解释（模板逐句有据）。
6. 流畅度已规范化：繁体自动转简、常见英文词替换、清理中文字间多余空格；未装 `zhconv` 时只做检测，
   检出繁体或残留英文即回退模板。偶发错别字（如「预算是内」）尚未处理。
7. `--mode auto` 的真实多模态链路已在 RTX 4060 + Realtek 麦克风 + 640×480 摄像头上**实测通过**
   （见 `outputs/real_multimodal_check.json`）：真实 ASR / VL / MediaPipe 手势输出与离线契约完全一致；
   但真实手势需要真人配合，本机实测靠历史手势快照回放（9× victory、1× thumbs_up；
   victory 只用于验证模型识别能力，页面已忽略，不触发任何动作）。
8. **输入侧归一化**（`config.INPUT_NORM`）与第 6 条的生成侧流畅度治理是**两套口径**：
   用户 / ASR 文本进入意图解析前只做等价改写 —— 繁转简、全角数字转半角、
   折叠被空格拆开的数字（`3 0 0` → `300`）、对紧邻数量词的方向词定向纠错（`是内` → `以内`）。
   这里**绝不删除英文词、绝不截断文本**（那是第 6 条的口径，用在用户输入上会丢掉真实信息）。
   原文保留在 `event.raw.spec.text`，处置明细写入 `event.raw.text_norm`。
   中文数字预算同样支持：`三百以内` → 300、`一百五左右` → 150±15%。
   回归证据：`outputs/text_norm_check.json` 的 T-10 ~ T-16。
9. **ASR 质量已分「采集端 / 转写端」实测**（`outputs/asr_quality_check.json`）：
   转写端在 24 条固定测试音频上 **CER=0.000**（23/24 全对，唯一例外是单字「嗯」返回空），
   术语召回 97.8%、数字召回 100%、热推理延迟 p50 124ms（冷启动约 26s）。
   但真机 4s 录音峰值只有 **-24.9 dBFS / RMS -41.5 dBFS**，比合成音频低约 19–24 dB，
   同一模型返回空文本 —— 说明瓶颈在**采集端输入电平**，不在模型。
   现场演示前应先检查麦克风增益（`scripts/00_check_env.py` 不测电平）。

## 9. 降级方式（真实模型/外部服务不可用时）

| 不可用对象 | 降级路径 | 结果表现 |
|---|---|---|
| 推荐系统 HTTP | 发布制品 → 内置快照 | 结果正常，`trace.degradations` 留痕 |
| 推荐系统制品缺失 | 内置快照 | 使用 `data/offline/` |
| 真实 ASR/VL/手势 | 离线场景桩（`data/cases`） | 契约一致，仅数据来源不同 |
| ASR 结果为空 | 明确错误，不推测 | `E_EMPTY_TRANSCRIPT` |
| RAG 无命中 | 只用结构化字段解释 | 不编造 |
| 硬约束过滤后为空 | 放宽软约束 + 热门兜底 | `status=degraded` + 提示语 |
| TTS 不可用 | 纯文本呈现 | `voice_available=false` |
| 单路召回超时 | 其余来源继续 | 该路标记 `failed`，链路不中断 |

## 10. 提交包

```powershell
python scripts/clean_workspace.py      # 清理缓存
python scripts/06_e2e_acceptance.py    # 生成验收报告 + 隐私扫描（verdict 必须 PASS）
```

包含：`config/ src/ scripts/ docs/ data/ outputs/ README.md requirements.txt main.py .gitignore`
不含：`artifacts/`（语音产物，可再生）、`__pycache__/`、`*.pyc`、模型权重、音视频、数据库、压缩包、任何密钥。
