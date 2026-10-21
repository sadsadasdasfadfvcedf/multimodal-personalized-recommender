"""
integrated_ai_project · 全局配置
================================

本模块是全项目 *唯一* 的配置入口（任务要求：关键路径必须从 config/settings.py 读取）。

设计原则
--------
1. 不硬编码绝对路径：外部系统根目录通过「环境变量 → 相对位置探测 → 内置离线快照」三级解析。
2. 不硬编码密钥：推荐系统 HTTP 模式的账号/口令只从环境变量读取，缺失时自动跳过该模式。
3. 不硬编码商品 ID：商品数据全部来自 data/offline/ 快照或外部系统发布的制品文件。
4. 所有阈值/权重/开关集中在此，脚本与 src 只允许 import，不允许散落常量。

用法:
    from config import settings
    settings.PATHS["outputs"]
"""

from __future__ import annotations

import os
import random
from collections.abc import MutableMapping
from pathlib import Path

# ============================================================
# 1. 项目根目录 / 随机种子
# ============================================================
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path | None = None, environ: MutableMapping[str, str] | None = None) -> None:
    """加载本地 env 文件，不覆盖已经存在的环境变量。

    只支持最简单的 ``KEY=VALUE`` 行；值为空时跳过。这样演示机可以直接把
    DeepSeek Key 放在被 .gitignore 忽略的 local.env 中，无需改代码或提交密钥。
    ``local.env`` 不匹配隐私扫描的 ``.env`` 禁止规则，也不会进入提交包。
    """
    target = environ if environ is not None else os.environ
    if path is None:
        candidate = PROJECT_ROOT / "local.env"
        env_path = candidate if candidate.is_file() else PROJECT_ROOT / ".env"
    else:
        env_path = path
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:].lstrip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            target.setdefault(key, value)


_load_dotenv()

# 换行常量（避免源码里出现反斜杠转义）
NL = chr(10)
SEED: int = 42


def set_seed(seed: int = SEED) -> None:
    """固定 Python / NumPy / (可选) Torch 随机种子，保证同输入同输出。"""
    random.seed(seed)
    os.environ["PYTHONHASHSEED"] = str(seed)
    try:
        import numpy as np

        np.random.seed(seed)
    except ImportError:
        pass
    try:
        import torch  # type: ignore

        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
    except Exception:
        pass


# ============================================================
# 2. 项目内标准路径
# ============================================================
PATHS: dict[str, Path] = {
    "root": PROJECT_ROOT,
    "config": PROJECT_ROOT / "config",
    "src": PROJECT_ROOT / "src",
    "scripts": PROJECT_ROOT / "scripts",
    "docs": PROJECT_ROOT / "docs",
    "data": PROJECT_ROOT / "data",
    "offline": PROJECT_ROOT / "data" / "offline",
    "cases": PROJECT_ROOT / "data" / "cases",
    "kb": PROJECT_ROOT / "data" / "kb",
    "outputs": PROJECT_ROOT / "outputs",
    "artifacts": PROJECT_ROOT / "artifacts",
    "images": PROJECT_ROOT / "artifacts" / "images_cache",
}

OUTPUT_FILES: dict[str, str] = {
    "stage01": "stage01_contract_report.json",
    "stage02_cases": "stage02_context_cases.jsonl",
    "stage02_metrics": "stage02_context_metrics.json",
    "stage03": "stage03_demo.json",
    "stage03_traces": "stage03_traces.jsonl",
    "stage04_cases": "stage04_explanation_cases.jsonl",
    "stage04_summary": "stage04_explanations.json",
    "stage05": "stage05_feedback_ab.json",
    "stage06": "stage06_acceptance.json",
    "e2e_log": "e2e_run_log.jsonl",
}


def output_path(key: str) -> Path:
    """取 outputs/ 下标准证据文件路径。"""
    if key not in OUTPUT_FILES:
        raise KeyError(f"未知输出键: {key}")
    return PATHS["outputs"] / OUTPUT_FILES[key]


# ============================================================
# 3. 外部系统根目录解析（禁止硬编码绝对路径）
# ============================================================
def _env_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value).expanduser() if value else None


def _first_existing(candidates) -> Path | None:
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate).resolve()
    return None


def _search_roots(names: list[str]) -> Path | None:
    """在若干常见相对位置探测兄弟项目目录（不使用任何硬编码绝对路径）。"""
    home = Path.home()
    bases = [
        PROJECT_ROOT.parent,  # 同目录并排部署
        PROJECT_ROOT.parent.parent,
        home / "Desktop",
        home / "Documents" / "trae_projects",
        home / "Documents",
        home / "projects",
    ]
    for name in names:
        found = _first_existing([base / name for base in bases])
        if found:
            return found
    return None


# 外部「推荐系统」（v146）根目录
RECOMMENDER_ROOT: Path | None = _env_path("INTEGRATED_RECOMMENDER_ROOT") or _search_roots(
    ["推荐系统", "recsys", "recommender_system"]
)
# 外部「智能多模态AI助手」根目录
MULTIMODAL_ROOT: Path | None = _env_path("INTEGRATED_MULTIMODAL_ROOT") or _search_roots(
    ["multimodal_ai", "Multimodal_AI"]
)

# 外部系统「发布制品」相对路径（只读，属于对外接口而非内部变量）
RECOMMENDER_INTEROP: dict[str, str] = {
    "products_csv": "webapp/data/products.csv",
    "app_data_json": "webapp/data/app_data.json",
    "price_map_json": "webapp/data/price_map.json",
    "user_history_json": "webapp/data/user_history.json",
    "submission_csv": "submission_v146_xgb_pairwise_deep_sample_order.csv",
    "model_version_from": "app_data_json",
}

MULTIMODAL_INTEROP: dict[str, str] = {
    "asr_module": "src/asr.py",
    "vl_module": "src/vl_model.py",
    "gesture_module": "src/gesture.py",
    "rag_module": "src/rag.py",
    "kb_dir": "data/demo_kb",
}


# ============================================================
# 4. 运行模式与降级总开关
# ============================================================
def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


RUNTIME: dict = {
    # offline   : 只用内置离线快照，绝不加载真实重型模型（默认，可离线演示）
    # auto      : 优先真实适配器，不可用则逐级降级
    # online    : 强制走外部系统 HTTP 接口
    "mode": os.getenv("INTEGRATED_MODE", "offline").strip().lower(),
    # 是否允许加载 torch/mediapipe 等重型真实模型
    "allow_real_models": _flag("INTEGRATED_ALLOW_REAL_MODELS", False),
    # 单次适配器调用的默认超时（秒）
    "default_timeout_s": float(os.getenv("INTEGRATED_TIMEOUT_S", "5.0")),
    # 是否在证据文件里写入耗时明细
    "record_latency": True,
}


# ============================================================
# 5. 推荐适配器配置（调用「推荐系统」，不读其内部变量）
# ============================================================
RECOMMENDER_ADAPTER: dict = {
    "strategy_order": ["http", "local_artifacts", "offline_snapshot"],
    "http": {
        "enabled": True,
        "base_url": os.getenv("INTEGRATED_RECSYS_URL", "http://127.0.0.1:8010"),
        # 账号/口令只来自环境变量，代码里不留任何密钥
        "username_env": "INTEGRATED_DEMO_USER",
        "password_env": "INTEGRATED_DEMO_PASSWORD",
        # 登录请求体字段映射：推荐系统 /api/login 期望 {"user_id", "password"}
        # 若上游改为 {"username", "password"}，只改这里，不动代码。
        "login_payload_keys": {"user": "user_id", "password": "password"},
        "timeout_s": float(os.getenv("INTEGRATED_RECSYS_TIMEOUT_S", "3.0")),
        "retries": 1,
        "endpoints": {
            "login": "/api/login",
            "recommendations": "/api/recommendations",
            "product": "/api/products/{item_id}",
            "history": "/api/history",
        },
    },
    "local_artifacts": {"enabled": True},
    "offline_snapshot": {"enabled": True},
    # 候选来源名称（写入 RecommendationItem.sources，保证可追溯）
    "sources": {
        "personalized": "推荐系统 v146 个性化精排",
        "similar": "相似商品召回",
        "popular": "全局热门兜底",
        "category_hot": "品类热门召回",
        "history": "历史购买相似召回",
    },
}


# ============================================================
# 6. 多模态适配器配置（调用「智能多模态AI助手」）
# ============================================================
MULTIMODAL_ADAPTER: dict = {
    "asr": {
        # 真实 ASR 采样率（与 multimodal_ai 保持一致）
        "sample_rate": 16000,
        "min_valid_duration_ms": 400,
        "min_text_len": 2,
    },
    "vision": {
        # 视觉标签置信度门限：低于 drop 直接丢弃，低于 low 降权
        "drop_confidence_threshold": 0.35,
        "low_confidence_threshold": 0.55,
        "low_confidence_weight": 0.5,
        "max_tags": 8,
    },
    # 真实模式：通过子进程调用外部项目，避免 src 包名冲突
    "real": {
        "worker": "scripts/mm_worker.py",
        "timeout_s": float(os.getenv("INTEGRATED_MM_TIMEOUT_S", "600")),
        "probe_timeout_s": 60.0,
    },
    "gesture": {
        "min_confidence": 0.60,
        # 主页面单次识别至少 3/5 帧一致才触发，避免扩展手势误判成点赞/倒赞
        "min_votes": int(os.getenv("INTEGRATED_GESTURE_MIN_VOTES", "3")),
        # 手势 → 反馈极性映射（thumbs_up/down → 正/负反馈）
        "feedback_map": {"thumbs_up": "positive", "thumbs_down": "negative"},
        # 网页端连拍：一次抓多帧，给用户留出摆姿势的时间，再按票数选最佳结果
        "capture_frames": int(os.getenv("INTEGRATED_GESTURE_FRAMES", "5")),
        "capture_interval_ms": int(os.getenv("INTEGRATED_GESTURE_INTERVAL_MS", "250")),
    },
}

# 在线视觉理解（可选）：默认使用阿里云百炼 OpenAI 兼容接口。
# 未配置 DASHSCOPE_API_KEY 时不会初始化，也不会影响离线链路。
VISION_API_CONFIG: dict = {
    "provider": os.getenv("INTEGRATED_VL_PROVIDER", "dashscope").strip().lower(),
    "api_key_env": "DASHSCOPE_API_KEY",
    "base_url": os.getenv(
        "INTEGRATED_VL_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    ).strip(),
    "model": os.getenv("INTEGRATED_VL_MODEL", "qwen3-vl-plus").strip(),
    "timeout_s": float(os.getenv("INTEGRATED_VL_TIMEOUT_S", "15")),
    "max_tokens": int(os.getenv("INTEGRATED_VL_MAX_TOKENS", "32")),
    "max_image_bytes": 4 * 1024 * 1024,
    "cache_ttl_s": float(os.getenv("INTEGRATED_VL_CACHE_TTL_S", "300")),
    "cache_max_entries": int(os.getenv("INTEGRATED_VL_CACHE_MAX", "32")),
    "rate_limit_per_minute": int(os.getenv("INTEGRATED_VL_RATE_LIMIT", "8")),
    "rate_window_s": float(os.getenv("INTEGRATED_VL_RATE_WINDOW_S", "60")),
    "fallback_to_local": os.getenv("INTEGRATED_VL_FALLBACK_LOCAL", "1").strip().lower()
    in {"1", "true", "yes", "on", "y"},
}


# ============================================================
# 6.1 商品图片与类目视觉（网页界面用）
# ============================================================
# 商品图由外部推荐系统的 image_service 按需解析（app_data.json 里没有图片 URL），
# 但其 /api/image/{item_id} 接口**不需要登录**，因此我们代理过来并做本地缓存，
# 这样推荐系统未启动时图片仍可显示（缓存是「已发布接口的响应」，不是读对方内部变量）。
IMAGE_CONFIG: dict = {
    "recache_days": 30,
    "timeout_s": 8.0,
    "max_bytes": 3 * 1024 * 1024,
    # 取不到图时的类目兜底图标
    "category_emoji": {
        "家居装饰": "🕯️",
        "装饰品": "🎁",
        "礼品": "🎁",
        "厨房用品": "🍽️",
        "生活用品": "🧺",
        "收纳用品": "📦",
        "派对用品": "🎉",
        "手工用品": "🧵",
        "文具用品": "✏️",
        "饰品": "💍",
        "购物袋": "🛍️",
        "玩具": "🧸",
        "园艺用品": "🪴",
        "花园用品": "🪴",
        "办公用品": "🗂️",
        "宠物用品": "🐾",
        "婴儿用品": "🍼",
        "家具": "🪑",
        "工具": "🔧",
        "洗浴用品": "🧴",
        "浴室用品": "🛁",
        "配饰": "🎀",
        "其他": "🛍️",
        "类别": "🛍️",
    },
    "default_emoji": "🛍️",
}

# ============================================================
# 7. 意图与槽位（阶段2）
# ============================================================
INTENT_CONFIG: dict = {
    "intents": ["discover", "similar", "filter", "compare", "feedback", "explain"],
    "intent_labels": {
        "discover": "发现/随便看看",
        "similar": "找相似",
        "filter": "带硬约束筛选",
        "compare": "对比",
        "feedback": "反馈（点赞/倒赞）",
        "explain": "求解释",
    },
    # 必选槽位缺失时不得臆测，返回 clarification_needed 与缺失槽位
    "required_slots": {
        "discover": [],
        "similar": ["target"],
        "filter": ["constraint_any"],
        "compare": ["targets"],
        "feedback": ["polarity"],
        "explain": ["target"],
    },
    # 优先级：越靠前越优先匹配
    "priority": ["feedback", "similar", "compare", "explain", "filter", "discover"],
    "min_intent_confidence": 0.30,
}

# 领域词表（词汇属于配置，不属于商品 ID）
# 演示账号（网页右上角可直接切换；没有对应账号时前端只显示当前 user_id）
DEMO_ACCOUNTS: list[dict] = [
    {
        "user_id": "15547",
        "name": "林先生",
        "persona": "家居复购型",
        "home_query": "只看家居装饰",
        "color": "#6d5dfc",
    },
    {
        "user_id": "14156",
        "name": "陈小姐",
        "persona": "厨房好物型",
        "home_query": "只看厨房用品",
        "color": "#ff7a59",
    },
    {
        "user_id": "17231",
        "name": "王女士",
        "persona": "装饰礼品型",
        "home_query": "只看礼品",
        "color": "#0aa38f",
    },
]

# 寒暄 / 身份问答：不进推荐链路。
# 用「归一化后精确匹配（允许 1-2 个语气词）」而不是 contains，
# 避免「你好，推荐一些礼物」这类正常需求被误判成寒暄。
# 常驻对话（开放问答）：只在「非推荐类问句」时启用，寒暄仍走 SMALLTALK。
CAMERA_CONFIG: dict = {
    "enabled": True,
    "worker": "scripts/camera_worker.py",
    "camera_index": int(os.getenv("INTEGRATED_CAMERA_INDEX", "0")),
    "width": 640,
    "height": 480,
    "fps": float(os.getenv("INTEGRATED_CAMERA_FPS", "12")),
    "startup_timeout_s": 30.0,
}

ASR_CONFIG: dict = {
    "enabled": True,
    "worker": "scripts/asr_worker.py",
    "timeout_s": float(os.getenv("INTEGRATED_ASR_TIMEOUT_S", "180")),
    "startup_timeout_s": 300.0,
}

CHAT_CONFIG: dict = {
    "enabled": True,
    # auto   : 配置了在线 Key 就优先在线，失败自动回退本地模型
    # online : 同上，但允许通过 INTEGRATED_ONLINE_LLM_STRICT=1 禁止回退
    # local  : 强制使用本地 Qwen
    "provider": os.getenv("INTEGRATED_LLM_PROVIDER", "auto").strip().lower(),
    "worker": "scripts/llm_chat_worker.py",
    "model_candidates": [
        "models/Qwen/Qwen3-4B-Instruct-2507",
        "models/Qwen/Qwen2-1.5B-Instruct",
    ],
    "online": {
        # 在线后端只读取环境变量中的 Key，绝不写入仓库或证据文件。
        # 支持 DashScope / DeepSeek / OpenAI / SiliconFlow / Moonshot / 智谱，
        # 也支持任意 OpenAI 兼容服务（用 INTEGRATED_ONLINE_LLM_BASE_URL 指定）。
        "enabled": _flag("INTEGRATED_ONLINE_LLM", True),
        "timeout_s": float(os.getenv("INTEGRATED_ONLINE_LLM_TIMEOUT_S", "45")),
        "max_history_messages": int(os.getenv("INTEGRATED_ONLINE_LLM_HISTORY", "12")),
        "concurrency": int(os.getenv("INTEGRATED_ONLINE_LLM_CONCURRENCY", "3")),
        # DeepSeek Search / Fetch：按成功请求计费，只在时间敏感问题中自动触发。
        "web_search": _flag("INTEGRATED_WEB_SEARCH", True),
        "search_count": int(os.getenv("INTEGRATED_WEB_SEARCH_COUNT", "5")),
        "search_content": _flag("INTEGRATED_WEB_SEARCH_CONTENT", False),
        "search_timeout_s": float(os.getenv("INTEGRATED_WEB_SEARCH_TIMEOUT_S", "30")),
    },
    "timeout_s": float(os.getenv("INTEGRATED_CHAT_TIMEOUT_S", "180")),
    "startup_timeout_s": 300.0,
    # 像问句 / 创意请求
    "question_markers": [
        "?",
        "？",
        "吗",
        "呢",
        "什么",
        "为什么",
        "怎么",
        "如何",
        "谁",
        "哪",
        "能不能",
        "可以吗",
        "是否",
        "讲",
        "写",
        "翻译",
        "计算",
        "介绍",
    ],
    # 出现这些词说明是购物/推荐需求，不能当闲聊。
    # 注意：不能把「推荐」单独列为阻断词 —— 「基于内容的推荐有什么区别？」
    # 这类技术追问会被误判成购物需求。
    "block_markers": [
        "推荐一些",
        "推荐点",
        "推荐几",
        "推荐个",
        "帮我推荐",
        "给我推荐",
        "推荐下",
        "推荐一下",
        "有什么好推荐",
        "有没有推荐",
        "为什么推荐",
        "推荐它",
        "推荐这个",
        "推荐这件",
        "推荐那件",
        "买",
        "购",
        "找",
        "看看",
        "逛逛",
        "筛选",
        "预算",
        "价格",
        "多少钱",
        "以内",
        "以下",
        "以上",
        "相似",
        "同款",
        "对比",
        "点赞",
        "倒赞",
        "商品",
        "产品",
    ],
    # 技术追问优先：命中这些词时，忽略购物阻断词与品类信号
    "tech_markers": [
        "推荐算法",
        "推荐系统",
        "推荐模型",
        "推荐引擎",
        "推荐技术",
        "推荐方法",
        "推荐策略",
        "推荐结果",
        "推荐效果",
        "推荐场景",
        "基于内容的推荐",
        "协同过滤",
        "冷启动",
        "召回",
        "精排",
        "排序模型",
        "deepseek",
        "flash",
        "v4",
        "v4.1",
        "模型版本",
        "模型发布",
        "版本发布",
    ],
    # 只有这些“时效性/检索型”问题才自动调用网页搜索，避免普通闲聊产生搜索费用。
    "web_search_markers": [
        "搜索",
        "搜一下",
        "查一下",
        "查查",
        "抓取",
        "抓一下",
        "fetch",
        "最新",
        "今天",
        "现在",
        "实时",
        "新闻",
        "近期",
        "发布",
        "版本",
        "官网",
        "链接",
        "资料",
        "网上",
        "联网",
        "文档",
        "正文",
        "search",
        "latest",
        "news",
        "today",
        "current",
        "release",
        "changelog",
        "http://",
        "https://",
    ],
    # 购物/推荐请求继续走本地推荐链路，不触发网页搜索。
    "web_search_block_markers": [
        "推荐一些",
        "推荐点",
        "帮我推荐",
        "给我推荐",
        "预算",
        "以内",
        "以下",
        "购物",
        "商品",
        "相似",
        "同款",
        "对比",
        "点赞",
        "倒赞",
    ],
}

SMALLTALK: dict = {
    "identity": ["你是谁", "你是什么", "你叫什么", "介绍一下你自己", "自我介绍", "who are you"],
    "capability": ["你能做什么", "你会什么", "你有什么功能", "你能干嘛", "怎么用", "使用说明", "help", "帮助"],
    "greeting": ["你好", "您好", "hi", "hello", "嗨"],
    "thanks": ["谢谢", "多谢", "感谢", "thanks", "thank you"],
}

SMALLTALK_REPLIES: dict = {
    "identity": (
        "我是「多模态个性化推荐助手」——把推荐系统 v146 和多模态 AI 助手整合在一起的课程项目。"
        "你可以用文字、语音、手势或画面告诉我需求，我会给出带证据的推荐、解释和对比。"
    ),
    "capability": (
        "我可以：① 按预算/品类/颜色/场景筛选；② 找相似商品；③ 对比多件商品；"
        "④ 解释推荐理由；⑤ 记录点赞/倒赞并调整排序。试试「500以内婚礼装饰品」或「对比这两件」。"
    ),
    "greeting": "你好！告诉我预算、品类或使用场景，我来帮你挑。",
    "thanks": "不客气！还想看什么，直接说就行。",
}

SLOT_LEXICON: dict = {
    "category_synonyms": {
        "厨房": "厨房用品",
        "厨具": "厨房用品",
        "餐具": "厨房用品",
        "杯": "厨房用品",
        "茶壶": "厨房用品",
        "碗": "厨房用品",
        "盘子": "厨房用品",
        "锅": "厨房用品",
        "勺子": "厨房用品",
        "家居": "家居装饰",
        "装饰": "家居装饰",
        "摆件": "家居装饰",
        "相框": "家居装饰",
        "蜡烛": "家居装饰",
        "烛台": "家居装饰",
        "灯串": "家居装饰",
        "靠垫": "家居装饰",
        "抱枕": "家居装饰",
        "收纳": "收纳用品",
        "储物": "收纳用品",
        "箱子": "收纳用品",
        "派对": "派对用品",
        "生日": "派对用品",
        "节日": "派对用品",
        "手工": "手工用品",
        "DIY": "手工用品",
        "生活": "生活用品",
        "日用": "生活用品",
        "饰品": "饰品",
        "首饰": "饰品",
        "项链": "饰品",
        "耳环": "饰品",
        "手链": "饰品",
        "戒指": "饰品",
        "眼镜": "饰品",
        "手表": "饰品",
        "帽子": "饰品",
        "文具": "文具用品",
        "笔": "文具用品",
        "书": "文具用品",
        "本子": "文具用品",
        "婴儿": "其他",
        "儿童": "其他",
        "玩具": "玩具",
        "玩偶": "玩具",
        "公仔": "玩具",
        "积木": "玩具",
        "拼图": "玩具",
        "滑板": "玩具",
        "球": "玩具",
        "园艺": "园艺用品",
        "花": "园艺用品",
        "植物": "园艺用品",
        "礼物": "装饰品",
        "礼品": "装饰品",
        "袋": "购物袋",
        "背包": "购物袋",
        "手提包": "购物袋",
        "钱包": "购物袋",
        "包装": "购物袋",
        "宠物": "宠物用品",
        "办公": "办公用品",
        "电脑": "办公用品",
        "显示器": "办公用品",
        "键盘": "办公用品",
        "鼠标": "办公用品",
        "浴室": "家居装饰",
        "洗浴": "生活用品",
        "热水袋": "生活用品",
        "围巾": "生活用品",
        "手套": "生活用品",
        "衣服": "生活用品",
        "衬衫": "生活用品",
        "外套": "生活用品",
        "鞋": "生活用品",
        "毛巾": "生活用品",
        "耳机": "生活用品",
        "手机": "生活用品",
    },
    "colors": {
        "白色": ["白", "白色", "奶白", "white"],
        "黑色": ["黑", "黑色", "black"],
        "红色": ["红", "红色", "大红", "red"],
        "粉色": ["粉", "粉色", "pink"],
        "蓝色": ["蓝", "蓝色", "blue"],
        "绿色": ["绿", "绿色", "green"],
        "黄色": ["黄", "黄色", "yellow"],
        "银色": ["银色", "银制", "银色金属", "silver"],
        "金色": ["金色", "金制", "镀金", "gold"],
        "紫色": ["紫", "紫色", "purple"],
        "橙色": ["橙", "橙色", "orange"],
        "奶油色": ["奶油色", "米白", "cream"],
    },
    "scenes": {
        "客厅": "客厅",
        "卧室": "卧室",
        "厨房": "厨房",
        "餐厅": "餐厅",
        "浴室": "浴室",
        "卫生间": "浴室",
        "花园": "花园",
        "庭院": "花园",
        "办公室": "办公室",
        "书房": "书房",
        "婚礼": "婚礼",
        "圣诞": "圣诞",
        "生日": "生日",
        "派对": "派对",
        "情人节": "情人节",
        "万圣节": "万圣节",
        "礼物": "送礼",
        "送礼": "送礼",
        "乔迁": "乔迁",
    },
    "budget": {
        # 关键词 → 修饰方向
        "under_words": ["以内", "以下", "不超过", "不超", "最多", "低于", "under", "below", "less than"],
        "over_words": ["以上", "至少", "不低于", "高于", "over", "above", "more than"],
        "around_words": ["左右", "大概", "大约", "around", "about"],
        "near": 0.15,  # “左右”允许 ±15% 的浮动
        "currency": "GBP",
        "symbol": "£",
        "unit_words": ["元", "块", "英镑", "gbp", "pound", "pounds", "£"],
    },
    "similar_markers": ["类似", "相似", "差不多", "同款", "一样的", "像这个", "和这个", "similar", "like this"],
    "compare_markers": ["对比", "比较", "比一比", "哪个好", "哪个更", "compare", "vs", "versus"],
    "explain_markers": ["为什么", "解释", "理由", "依据", "怎么推荐", "why", "explain", "reason"],
    # 注意：不使用「好」「差」这类单字，避免“好东西”“哪个好”被误判成反馈
    "feedback_words": {
        "positive": [
            "喜欢",
            "不错",
            "很好",
            "满意",
            "挺好",
            "好棒",
            "真棒",
            "点赞",
            "漂亮",
            "like",
            "great",
            "awesome",
        ],
        "negative": [
            "不喜欢",
            "不行",
            "太贵",
            "不好",
            "讨厌",
            "太差",
            "很差",
            "太丑",
            "难看",
            "不满意",
            "dislike",
            "bad",
        ],
    },
    # 指代词：用于把「这个 / 第一个 / 刚才那款」解析为会话中的具体商品
    "referential_markers": [
        "这个",
        "这款",
        "那款",
        "那个",
        "它",
        "这一个",
        "刚才那个",
        "上面那个",
        "第",
        "this one",
        "this",
    ],
    "undo_markers": ["撤销", "取消", "上一步", "算了", "undo", "revoke"],
    "reset_markers": ["重新开始", "清空", "重来", "reset", "clear"],
}

# 中文数字字符集（输入归一化与预算解析共用，避免两处各写一份）
CN_NUM_CHARS = "零〇一壹二两贰三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟"

# 输入侧归一化（ASR 文本 / 手输文本进入意图解析前的保守清洗）
#
# 原则：只做**等价改写**，以及高置信度错别字的**定向**纠正。
# 明确不做：英文词删除、复读截断、任意中文数字替换 ——
#   前两项属于生成侧流畅度治理（见 src/text_norm.normalize_fluency），
#   用在用户输入上会把「iPhone 手机壳」这类真实信息删掉。
INPUT_NORM: dict = {
    "trad_to_simp": True,
    "fullwidth_to_halfwidth": True,
    "collapse_split_digits": True,
    # 错别字规则只在「数量词紧邻方向词」的位置生效，因此「他是内行」不会被误改成「他以内行」。
    "typo_rules": [
        {"pattern": rf"是内(?=\s*[\d{CN_NUM_CHARS}])", "replace": "以内", "desc": "是内→以内（预算方向词）"},
        {"pattern": rf"已内(?=\s*[\d{CN_NUM_CHARS}])", "replace": "以内", "desc": "已内→以内（预算方向词）"},
        {"pattern": rf"(?<=[\d{CN_NUM_CHARS}])一内", "replace": "以内", "desc": "一内→以内（预算方向词）"},
        {"pattern": rf"是下(?=\s*[\d{CN_NUM_CHARS}])", "replace": "以下", "desc": "是下→以下（预算方向词）"},
        {"pattern": rf"(?<=[\d{CN_NUM_CHARS}])一上", "replace": "以上", "desc": "一上→以上（预算方向词）"},
    ],
}

# 约束冲突规则（阶段2：约束冲突可检测）
CONFLICT_RULES: list[dict] = [
    {"id": "budget_range", "desc": "预算下限高于上限", "fields": ["budget_min", "budget_max"]},
    {"id": "color_exclusive", "desc": "同时指定了互斥的多种颜色（大于 2 种）", "fields": ["colors"], "max_ok": 2},
]


# ============================================================
# 8. 召回 / 排序（阶段3）
# ============================================================
RECALL_CONFIG: dict = {
    "recall_k": 80,  # 每路召回条数
    "merge_top_k": 120,  # 合并去重后进入排序的候选数
    # session_focus：把会话焦点/刚点赞的商品强制放回候选池，
    # 保证「反馈后排序变化」可观察、可解释（否则被点过的商品可能因分路原因掉出候选池）
    "sources_enabled": ["personalized", "similar", "category_hot", "session_focus", "popular", "history"],
    "dedupe_keep": "max_score",
    "similar_seed_size": 3,  # 用 Top-N 种子商品做相似扩散
    "dedupe_case_insensitive": True,  # 商品编码大小写视为同一商品（源数据存在 84970S/84970s 这类变体）
    "popular_size": 200,  # 全局热门池大小
}

RANKING_CONFIG: dict = {
    "top_k": 8,
    "weights": {
        "base_score": 0.55,  # 推荐系统原始分数（归一化后）
        "constraint_match": 0.20,  # 命中硬约束的加权
        "source_trust": 0.10,  # 来源可信度
        "session_preference": 0.15,  # 会话偏好（点赞/倒赞/浏览）
    },
    # 非个性化候选的基础分折扣：避免热门/相似候选仅凭「归一化到 1.0」就压过个性化精排
    "non_personalized_base_discount": 0.5,
    "source_trust": {
        "推荐系统 v146 个性化精排": 1.0,
        "历史购买相似召回": 0.9,
        "相似商品召回": 0.8,
        "品类热门召回": 0.6,
        "会话关注召回": 0.85,
        "全局热门兜底": 0.4,
    },
    # 硬约束必须在打分前生效，且排序分数不得覆盖硬约束
    "hard_constraints": ["budget", "category", "color", "exclude_ids"],
    "soft_constraints": ["scene", "target"],
    "session_preference": {
        # 会话偏好权重：点赞/倒赞的差异必须可归因，故“已曝光惩罚”默认关闭。
        # 若要做多轮多样性实验，把它调成 0.05 左右即可重新启用。
        "like_category_boost": 0.12,
        "dislike_category_penalty": 0.25,
        "like_item_boost": 0.10,
        # 「继续浏览」类**弱反馈**：任务书阶段5 目标要求把「点赞、倒赞和继续浏览等反馈」
        # 一并写入会话偏好。浏览是弱正信号（强度远低于点赞），只轻微上浮。
        "view_item_boost": 0.04,
        "view_category_boost": 0.03,
        "view_max_count": 2,  # 同一商品最多累计计 2 次，防止反复打开刷分
        "seen_penalty": 0.0,
        # 单品倒赞只屏蔽该商品；同类目累计倒赞达到该次数才降权整个类目
        "dislike_category_min_count": 2,
        "max_boost": 0.30,
    },
    "empty_result_hint": "没有找到完全符合当前条件的结果，已为你降级展示同类热门商品。",
}


# ============================================================
# 9. 证据与解释（阶段4）
# ============================================================
RAG_CONFIG: dict = {
    "enabled": True,
    "kb_dir": "data/kb",
    "chunk_size": 220,
    "chunk_overlap": 40,
    "top_k": 3,
    "min_score": 0.05,
    "tokenizer": "jieba",  # jieba 不可用时自动退化为字符二元组
}

# 币种符号（价格展示用；币种取自数据，符号在此集中映射）
CURRENCY_SYMBOLS: dict = {"GBP": "£", "CNY": "¥", "USD": "$", "EUR": "€"}

EXPLANATION_CONFIG: dict = {
    # 每条解释至少要有 1 条可核验证据；否则不得编造
    "min_evidence": 1,
    "max_evidence": 4,
    "price_tolerance": 0.01,  # 价格一致性核验容差
    # 视觉标签参与解释的最低有效置信度（低于此值降权后直接忽略）
    "visual_min_effective_confidence": 0.55,
    "no_evidence_text": "暂无可核验依据，因此不做推荐解释。",
    "templates": {
        "constraint": "符合你提出的「{value}」要求（依据：商品属性 {field}={actual}）。",
        "category": "它属于「{category}」品类，与你当前需求一致（依据：商品目录 category={actual}）。",
        "price": "价格 £{price}，在预算 £{budget} 之内（依据：price_map 历史成交价）。",
        "similar": "它与「{seed_title}」相似度为 {score:.2f}（依据：推荐系统相似索引）。",
        "visual": "画面识别到「{tag}」（置信度 {confidence:.2f}，来源：{source}）。",
        "session": "你在本会话中对「{category}」表达了正向偏好（依据：会话反馈记录）。",
    },
}

PRESENTATION_CONFIG: dict = {
    "channels": ["text", "voice"],
    "voice": {
        "enabled": True,
        "engine": "edge-tts",
        "voice_zh": "zh-CN-XiaoxiaoNeural",
        "voice_en": "en-US-JennyNeural",
        "output_name": "explanation.mp3",
        "timeout_s": 15,
        "max_sentences": 3,
    },
    "max_items_in_text": 5,
}


# ============================================================
# 9.1 真实 LLM 解释（可选增强）
# ============================================================
# 离线演示不使用该项；启用条件：INTEGRATED_ALLOW_REAL_MODELS=1 且适配器进入 real 模式。
# 无论是否启用，解释都必须通过证据校验 —— LLM 输出不可溯源时一律回退模板解释。
LLM_EXPLAIN_CONFIG: dict = {
    "enabled": _flag("INTEGRATED_LLM_EXPLAIN", True),
    "max_new_tokens": int(os.getenv("INTEGRATED_LLM_MAX_TOKENS", "120")),
    "temperature": float(os.getenv("INTEGRATED_LLM_TEMPERATURE", "0.2")),
    # 格式与质量门控阈值（与事实门控相互独立）
    "max_chars": 90,
    "max_lines": 1,
    "max_repeat": 2,
    "min_chars": 20,  # 截断时至少保留的字符数，避免切出残句
    "instruction": ("你是电商推荐解释助手。请把下面的【事实】改写成一句自然、口语化的中文推荐理由。"),
    "output_rules": (
        "回答中禁止出现任何阿拉伯数字（0-9 一个都不能有）；"
        + NL
        + "需要表达价格或预算时，改用「价格实惠」「在你的预算范围内」这类说法；"
        + NL
        + "类目只能照抄「事实」中给出的写法，不得替换成近义词；"
        + NL
        + "不要分点、不要列举、不要重复粘贴「事实」原文；"
        + NL
        + "只输出一句话，不超过 60 个字。"
    ),
}

# ============================================================
# 10. 反馈闭环（阶段5）
# ============================================================
FEEDBACK_CONFIG: dict = {
    # 正负反馈互斥：同一商品只能保留最后一种极性
    "mutual_exclusion": True,
    "positive": "positive",
    "negative": "negative",
    # 会话偏好作用范围：session 级，不污染其他会话
    "scope": "session",
    "ab": {
        "top_k": 8,
        "compare_metric": "topk_order",
        "min_score_delta": 1e-6,
    },
}


# ============================================================
# 11. 降级矩阵（阶段1/3/6 共用口径）
# ============================================================
DEGRADATION_MATRIX: list[dict] = [
    {
        "scenario": "推荐系统 HTTP 不可用",
        "trigger": "连接失败/超时/未登录",
        "fallback": "local_artifacts（读取发布制品）→ offline_snapshot（内置快照）",
        "level": "L1",
    },
    {"scenario": "推荐系统制品缺失", "trigger": "文件不存在", "fallback": "offline_snapshot 内置快照", "level": "L2"},
    {
        "scenario": "真实多模态模型不可用",
        "trigger": "torch/mediapipe 缺失或未授权",
        "fallback": "离线场景桩（data/cases 中的模态输入）",
        "level": "L1",
    },
    {
        "scenario": "ASR 文本为空/过短",
        "trigger": "有效时长 < 400ms 或文本长度 < 2",
        "fallback": "返回 E_EMPTY_TRANSCRIPT，不臆测意图",
        "level": "L2",
    },
    {
        "scenario": "RAG 无命中",
        "trigger": "最高相似度 < min_score",
        "fallback": "仅用结构化字段解释，不编造",
        "level": "L2",
    },
    {
        "scenario": "硬约束过滤后无结果",
        "trigger": "candidates = 0",
        "fallback": "放宽为软约束 + 热门兜底，并标记 degraded=true",
        "level": "L1",
    },
    {
        "scenario": "语音呈现不可用",
        "trigger": "TTS 合成失败/超时",
        "fallback": "仅文本呈现，voice_available=false",
        "level": "L2",
    },
]


# ============================================================
# 12. 隐私与提交包检查（阶段6）
# ============================================================
PRIVACY_CONFIG: dict = {
    # 提交包中禁止出现的文件/目录
    "forbidden_patterns": [
        "__pycache__",
        "*.pyc",
        "*.pyo",
        "*.pth",
        "*.pt",
        "*.safetensors",
        "*.bin",
        "*.onnx",
        "*.mp3",
        "*.wav",
        "*.mp4",
        "*.avi",
        "*.jpg",
        "*.png",
        ".env",
        "*.log",
        "*.zip",
        "*.sqlite",
        "*.db",
    ],
    # 允许保留的目录例外（证据目录本身）
    "allowed_dirs": ["outputs", "docs", "artifacts"],
    # 需要脱敏扫描的键名（出现即视为敏感）
    "sensitive_keys": ["password", "passwd", "secret", "token", "api_key", "apikey", "cookie"],
    # 演示账号只从环境变量读取，不写入任何文件
    # 注意：脚本类文件（.ps1 / .sh / .bat）也算「文本」，必须纳入扫描 ——
    # 曾经漏掉 .ps1，导致 scripts/start_all.ps1 里的硬编码口令与绝对路径一路躲过检查。
    "scan_text_extensions": [
        ".py",
        ".json",
        ".jsonl",
        ".md",
        ".txt",
        ".csv",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".ps1",
        ".sh",
        ".bat",
        ".cmd",
    ],
}


# ============================================================
# 13. 目录自检
# ============================================================
def ensure_dirs() -> None:
    """确保项目所需目录存在（不触碰外部系统目录）。"""
    for key in ("offline", "cases", "kb", "outputs", "artifacts", "docs"):
        PATHS[key].mkdir(parents=True, exist_ok=True)


def describe() -> dict:
    """返回一份可写入证据文件的配置摘要（不含任何密钥）。"""
    return {
        "project_root": str(PROJECT_ROOT),
        "seed": SEED,
        "runtime_mode": RUNTIME["mode"],
        "allow_real_models": RUNTIME["allow_real_models"],
        "recommender_root_found": bool(RECOMMENDER_ROOT),
        "recommender_root": str(RECOMMENDER_ROOT) if RECOMMENDER_ROOT else None,
        "multimodal_root_found": bool(MULTIMODAL_ROOT),
        "multimodal_root": str(MULTIMODAL_ROOT) if MULTIMODAL_ROOT else None,
        "recommender_strategy_order": RECOMMENDER_ADAPTER["strategy_order"],
        "recall_sources": RECALL_CONFIG["sources_enabled"],
        "top_k": RANKING_CONFIG["top_k"],
    }
