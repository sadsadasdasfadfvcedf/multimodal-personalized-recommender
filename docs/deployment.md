# 部署与启动说明

> 阶段6 交付物 · 依赖、启动、端口、打包与迁移

## 1. 运行环境

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Windows 10/11 | 语音呈现依赖 Windows SAPI（`System.Speech`） |
| Python | 3.10+（实测 3.12） | 全部离线链路只用标准库 + 可选依赖 |
| 内存 | ≥ 4 GB | 离线演示实际占用 < 300 MB |
| 磁盘 | ≥ 50 MB | 含离线快照（约 2.3 MB）与知识库 |
| 网络 | 非必需 | 仅 `edge-tts`（在线语音）与推荐系统 HTTP 模式需要 |

## 2. 依赖

**离线演示所需（最小集）**：无第三方依赖，标准库即可完成全部六个阶段脚本。

**推荐的增强依赖**（存在则自动启用，缺失自动降级）：

```
numpy          # 数值处理
jieba          # 中文分词（缺失时退化为字符二元组）
requests       # 推荐系统 HTTP 模式
edge-tts       # 在线语音合成（音质更好）
```

可选（真实多模态模式，`--mode auto` 且 `INTEGRATED_ALLOW_REAL_MODELS=1`）：

```
torch mediapipe opencv-python PyQt5 funasr
```

安装：

```powershell
cd C:\Users\Administrator\Desktop\多模态个性化推荐助手\integrated_ai_project
python -m pip install -r requirements.txt
```

## 3. 启动

```powershell
# 0) 运行环境检查
python scripts/00_check_env.py

# 1) 网页对话界面（推荐）
python webui.py                          # → http://127.0.0.1:8020
python webui.py --real --voice --llm     # 真实多模态 + 语音 + 真实 LLM

# 1b) 终端对话
python chat.py
python chat.py --real --voice --llm      # 真实多模态 + 语音播报 + 真实 LLM 解释

# 2) 一键端到端演示（写死的三轮脚本，用于验收取证；含语音播报）
python main.py

# 2) 逐个阶段的证据生产
python scripts/01_contract_test.py        # 接口契约与降级矩阵
python scripts/02_context_test.py         # 意图理解与会话上下文
python scripts/03_orchestrator_demo.py    # 编排 / 多路召回 / 精排 / 降级
python scripts/04_explanation_test.py     # 证据包与 RAG 解释
python scripts/05_feedback_ab.py          # 反馈闭环 A/B
python scripts/06_e2e_acceptance.py       # 端到端 / 故障降级 / 隐私扫描
python scripts/07_live_integration_check.py  # 在线联调（需推荐系统在跑，可选）
python scripts/09_real_multimodal_check.py   # 真机多模态（需设备+权重，可选）
python scripts/10_real_llm_check.py          # 真实 LLM 解释链路（可选）
python scripts/11_llm_model_compare.py       # LLM 选型对比（可选）
python scripts/12_text_norm_check.py         # 文本规范化与流畅度门控

# 3) 打包前清理
python scripts/clean_workspace.py
```

## 4. 运行模式与环境变量

| 环境变量 | 取值 | 默认 | 作用 |
|---|---|---|---|
| `INTEGRATED_MODE` | `offline` / `auto` / `online` | `offline` | 运行模式 |
| `INTEGRATED_ALLOW_REAL_MODELS` | `0` / `1` | `0` | 是否允许加载真实重型模型 |
| `INTEGRATED_RECOMMENDER_ROOT` | 路径 | 自动探测 | 推荐系统根目录 |
| `INTEGRATED_MULTIMODAL_ROOT` | 路径 | 自动探测 | 多模态助手根目录 |
| `INTEGRATED_RECSYS_URL` | URL | `http://127.0.0.1:8010` | 推荐系统 HTTP 地址 |
| `INTEGRATED_DEMO_USER` | 账号 | 未设置 | HTTP 模式演示账号（不在代码中出现） |
| `INTEGRATED_DEMO_PASSWORD` | 口令 | 未设置 | HTTP 模式演示口令（不在代码中出现） |
| `INTEGRATED_TIMEOUT_S` | 秒 | `5.0` | 适配器默认超时 |
| `INTEGRATED_CHAT_TIMEOUT_S` | 秒 | `180` | 单轮开放问答生成超时 |
| `INTEGRATED_GESTURE_FRAMES` | 帧 | `5` | 网页手势连拍帧数 |
| `INTEGRATED_GESTURE_INTERVAL_MS` | 毫秒 | `250` | 网页手势连拍间隔 |

示例（启用推荐系统 HTTP 模式）：

```powershell
$env:INTEGRATED_MODE = "auto"
$env:INTEGRATED_RECSYS_URL = "http://127.0.0.1:8010"
$env:INTEGRATED_DEMO_USER = "<演示账号>"
$env:INTEGRATED_DEMO_PASSWORD = "<演示口令>"
python main.py
```

## 5. 外部系统对接

### 5.1 推荐系统（可选）

```powershell
cd C:\Users\Administrator\Desktop\推荐系统\v146_release
.\run_frontend.ps1 -Install          # 启动后监听 127.0.0.1:8010
```

集成层在该服务不可用时自动降级，不影响离线演示。

### 5.2 在线联调验证（推荐系统在跑时）

```powershell
$env:INTEGRATED_MODE = "auto"
$env:INTEGRATED_DEMO_USER = "<演示账号>"
$env:INTEGRATED_DEMO_PASSWORD = "<演示口令>"
python scripts/07_live_integration_check.py
```

会依次验证：`/api/login` 登录鉴权 → `/api/recommendations` 个性化候选 → 三轮主链路，
并把 HTTP 模式与内置快照模式做同一查询的 TopK 对照，写入 `outputs/live_integration.json`。

> 登录请求体字段映射在 `config.RECOMMENDER_ADAPTER["http"]["login_payload_keys"]`，
> 上游若改字段名只需改配置。服务未启动或未配账号时脚本记录「跳过」并正常退出。

### 5.3 重新生成离线快照（推荐系统制品更新后）

```powershell
python scripts/build_offline_snapshot.py
# 或指定路径
python scripts/build_offline_snapshot.py --recommender-root "D:/path/to/推荐系统"
```

### 5.4 真机多模态链路验证（可选）

```powershell
$env:INTEGRATED_ALLOW_REAL_MODELS = "1"
$env:INTEGRATED_MODE = "auto"
python scripts/09_real_multimodal_check.py
```

会真实调用外部 multimodal_ai 的三个模型：

| 环节 | 真实实现 | 本机实测结果 |
|---|---|---|
| 麦克风 → ASR | FunASR SenseVoiceSmall | 『你找我干嘛』（实时环境音） |
| 摄像头 → VL | Qwen2-VL-2B-Instruct | 『画面里有一个人，他坐在椅子上，穿着白色的衣服。』 |
| 手势快照 → MediaPipe | HumanDetector + GestureRecognizer | 扫描 46 张 → 检出 14 张有手 → 确认 10 个手势 |

前置条件：设备可用、模型权重**完整**（脚本会跳过不完整权重）、`PyAudio`/`mediapipe`/`funasr` 已安装。
任一条件不满足时脚本记录「跳过」并正常退出。

> 实现要点：真实后端通过**子进程**调用外部项目（`scripts/mm_worker.py`），
> 以 `cwd` 与 `PYTHONPATH` 指向外部根目录 —— 本项目与外部项目都有 `src` 包，
> 同进程 import 会命名空间冲突。子进程同时形成清晰的进程边界。

### 5.5 真实 LLM 解释链路验证（可选）

```powershell
$env:INTEGRATED_ALLOW_REAL_MODELS = "1"
$env:INTEGRATED_MODE = "auto"
python scripts/10_real_llm_check.py
```

链路：`EvidencePack` → 强约束提示词 → 真实 LLM → **事实门控 + 格式门控** → 规范化 → 展示。

| 门控 | 拦截内容 | 拦截方式 |
|---|---|---|
| 事实门控 | 编造数字、替换类目近义词、提到别的商品 | 数值必须与证据精确匹配（容差 1e-6，不做四舍五入近似）；类目/商品名必须与证据一致 |
| 格式门控 | 多行、列表、自我重复、超长 | 单行；≤90 字；8 字滑窗重复 ≤2 次 |
| 流畅度门控 | 繁体字、中英混排 | 繁转简（zhconv/opencc）+ 常见英文词替换 + 清理中文字间空格；未映射英文词即拦截 |

LLM 输出未通过任一门控即**回退模板解释**（模板逐句有据）。可用开关：

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `INTEGRATED_LLM_EXPLAIN` | `1` | 是否启用真实 LLM 改写（仅在 real 模式下生效） |
| `INTEGRATED_LLM_MAX_TOKENS` | `120` | 单条生成上限 |
| `INTEGRATED_LLM_TEMPERATURE` | `0.2` | 采样温度 |

> 性能提示：子进程每次调用都要重新加载权重，因此提供 `llm_batch` 批量接口
> （4 条解释一次加载，均摊约 13 s/条）。若不希望任何 LLM 参与，设 `INTEGRATED_LLM_EXPLAIN=0`。

**模型选型**：`python scripts/11_llm_model_compare.py` 会在同一批证据、同一套门控下对比所有可用 LLM：

| 模型 | 磁盘 | 门控通过率 | 均耗时 |
|---|---|---|---|
| **Qwen3-4B-Instruct-2507** | 7.7 GB | **100%（6/6）** | 9.6 s/条 |
| Qwen2-1.5B-Instruct | 3.0 GB | 50%（3/6） | 6.6 s/条 |

下载模型（国内走 ModelScope，避免直连 HuggingFace 不可达）：

```powershell
python -c "from modelscope import snapshot_download; snapshot_download('Qwen/Qwen3-4B-Instruct-2507', local_dir=r'<外部项目>\models\Qwen\Qwen3-4B-Instruct-2507')"
```

> 加载行为：worker 默认强制 `HF_HUB_OFFLINE=1` / `TRANSFORMERS_OFFLINE=1`，
> 因为本机直连 huggingface.co 不可达会让 transformers 长时间挂起（实测 30 分钟无进展）。
> 需要联网时设 `INTEGRATED_MM_ONLINE=1`。

### 5.6 常驻开放问答（可选）

`webui.py --real` / `chat.py --real` 下，非购物类问句（概念解释、写作、翻译、计算等）
会交给常驻的 `scripts/llm_chat_worker.py`，由 `src/llm_chat.py` 管理生命周期：

- **只加载一次**：首次对话拉起 worker 并加载 Qwen3-4B（约 25~40 秒），后续消息走同一进程；
- **多轮上下文**：worker 内复用 `TextLLM.chat` 的 6 轮历史；`/reset`、网页「⟳ 重来」、
  切换用户都会调用 `chat.reset()` 清空；
- **进程隔离**：worker 以 `cwd=multimodal_ai`、`PYTHONPATH=multimodal_ai` 启动，避免两个
  项目的 `config` / `src` 同名包冲突；协议走 stdin/stdout JSON 行；
- **降级**：worker 启动失败或超时，本轮 `status=degraded`，`answer` 为固定提示，
  推荐链路不受影响；
- **不污染默认离线模式**：只有 `multimodal.active_mode == "real"` 时才创建 ChatService，
  `python webui.py` 默认仍不加载任何重型模型。

| 环境变量 | 默认 | 作用 |
|---|---|---|
| `INTEGRATED_CHAT_TIMEOUT_S` | `180` | 单轮生成超时（秒） |
| `MULTIMODAL_LLM_PATH` | 自动探测 | 覆盖 LLM 权重目录 |

实测（本机 RTX 4060，Qwen3-4B-Instruct-2507）：冷启动 26.6 秒，第二轮 4.2 秒，
技术追问 3.5 秒；`你是谁` 仍走 0.02 秒固定文案。

### 5.7 多模态助手（可选）

真实 ASR/VL/手势需要摄像头、麦克风与重型依赖。离线演示使用 `data/cases` 中的模态输入替代，
两者的 `InteractionEvent` 契约完全一致。

网页手势流程：前端 3 秒倒计时 → `capture_burst` 一次抓 5 帧（同一摄像头进程）→
`gesture_batch` 一次加载 MediaPipe 并逐帧识别 → 按票数/置信度选最佳手势。
这解决了「点按钮时手还没举起来」导致抓到空画面的问题。六种手势的页面动作：
👍/👎 商品反馈、✌️ 截图、✋ 停止播放、☝️ 开始录音、👇 暂停播放。
网页端使用**宽松识别档**（`lenient=true`：检测阈值 0.05、绕过「手挡脸」误检过滤），
因为用户主动把手举到脸前时严格档会把手势判成人脸误检；同时要求同一手势在 5 帧中至少 2 帧一致。
操作卡会显示最佳抓拍帧（`/capture/...`，点击看大图）。

## 6. 目录与产物

```
integrated_ai_project/
├─ main.py                     一键端到端演示
├─ config/settings.py          唯一配置入口（路径/阈值/权重/降级矩阵）
├─ src/                        领域实现（契约、适配器、意图、会话、召回、排序、证据、解释、呈现、反馈、隐私）
├─ scripts/                    00~06 阶段脚本 + 清理脚本 + 快照导出脚本
├─ data/
│  ├─ offline/                 离线快照：catalog / similar / scores / popular / history / users / meta
│  ├─ cases/                   阶段2/4/5 的用例数据
│  └─ kb/                      知识库文档（RAG 检索源）
├─ docs/                       architecture / protocol / deployment / risk_register / project_report
├─ outputs/                    各阶段证据文件（JSON / JSONL）
└─ artifacts/                  语音合成产物（可再生，不进入提交包）
```

## 7. 打包与提交

```powershell
python scripts/clean_workspace.py       # 清理 __pycache__ / *.pyc
python scripts/06_e2e_acceptance.py     # 生成验收报告并再次执行隐私扫描
```

提交包内容：`config/ src/ scripts/ docs/ data/ outputs/ README.md requirements.txt main.py .gitignore`

**不进入提交包**：`artifacts/`（语音产物，可再生）、`__pycache__/`、`*.pyc`、
模型权重（`*.pt/*.pth/*.safetensors/*.bin`）、音视频（`*.wav/*.mp3/*.mp4/*.avi`）、数据库与压缩包。

## 8. 迁移到其他机器

1. 复制整个项目目录；
2. 若推荐系统不在自动探测位置，设置 `INTEGRATED_RECOMMENDER_ROOT`；
3. 运行 `python scripts/00_check_env.py` 确认快照与依赖状态；
4. 直接运行 `python main.py`（完全离线，无需网络与外部服务）。

## 9. 常见问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 报告 `recommender_root_found=false` | 未找到推荐系统 | 设置 `INTEGRATED_RECOMMENDER_ROOT`，或直接使用内置快照（功能不受影响） |
| `voice_available=false` | edge-tts 与 Windows SAPI 均不可用 | 检查 PowerShell 是否可用；不影响文本呈现 |
| 语音产物为 `.wav` | 使用了 Windows SAPI 离线引擎 | 正常；`edge-tts` 可用时会输出 `.mp3` |
| 隐私扫描 `FAIL` | 存在 `__pycache__` | 运行 `python scripts/clean_workspace.py` 后重跑 |
| 阶段3 出现 `degraded=true` | 硬约束过滤后无候选 | 属预期降级，报告会给出 `degrade_reason` 与提示 |
| 意图识别不准 | 词表未覆盖 | 在 `config.SLOT_LEXICON` 中补充领域词，并重跑 02 用例 |
