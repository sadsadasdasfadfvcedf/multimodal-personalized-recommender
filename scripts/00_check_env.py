"""
scripts/00_check_env.py
=======================

运行环境检查（演示第一步）。

检查项：
    1. 配置文件解析结果（外部系统根目录、运行模式）
    2. 离线快照是否就绪（商品数 / 用户数 / 类目数）
    3. 两个适配器的健康状态与降级情况
    4. 知识库索引条数
    5. 关键依赖可用性（numpy / jieba / requests / edge-tts / torch）
    6. 离线语音自检（Windows SAPI / SAPI COM，演示前必看）

输出：outputs/env_check.json
"""

from __future__ import annotations

import importlib.util

from _bootstrap import PROJECT_ROOT, banner  # noqa: F401

from config import settings
from src.adapters import MultimodalAdapter, RecommenderAdapter
from src.catalog import Catalog
from src.presentation import probe_offline_tts
from src.rag import KnowledgeBase
from src.utils import now_iso, write_json

DEPS = ["numpy", "jieba", "requests", "httpx", "pandas", "edge_tts", "torch", "mediapipe", "PyQt5"]


def main() -> int:
    banner("运行环境检查 · 多模态个性化推荐助手")

    report: dict = {"checked_at": now_iso(), "config": settings.describe()}

    # 1) 目录
    settings.ensure_dirs()
    report["paths"] = {k: str(v) for k, v in settings.PATHS.items()}

    # 2) 离线快照
    catalog = Catalog.from_snapshot()
    report["catalog"] = catalog.describe()

    # 3) 适配器
    multimodal = MultimodalAdapter()
    recommender = RecommenderAdapter()
    report["adapters"] = {
        "multimodal": multimodal.health().data,
        "recommender": recommender.health().data,
    }

    # 4) 知识库
    kb = KnowledgeBase().load()
    report["knowledge_base"] = {"chunks": kb.size(), "documents": kb.docs()}

    # 5) 依赖
    report["dependencies"] = {
        name: ("available" if importlib.util.find_spec(name) is not None else "missing") for name in DEPS
    }

    # 6) 离线语音自检：不联网，只测本地 SAPI。纯文本降级仍然是可用状态，
    #    但演示前应当知道「到底有没有声音」。
    report["presentation"] = probe_offline_tts()

    print(f"项目根目录      : {settings.PROJECT_ROOT}")
    print(f"运行模式        : {settings.RUNTIME['mode']}（allow_real_models={settings.RUNTIME['allow_real_models']}）")
    print(f"推荐系统根目录  : {settings.RECOMMENDER_ROOT}")
    print(f"多模态助手根目录: {settings.MULTIMODAL_ROOT}")
    print(
        f"离线快照        : 商品 {catalog.size()} 件 / 用户 {len(catalog.scores)} 个 / 类目 {len(catalog.categories())} 个"
    )
    print(f"推荐适配器      : {recommender.active_mode}（降级 {len(recommender.degradations)} 次）")
    print(f"多模态适配器    : {multimodal.active_mode}（降级 {len(multimodal.degradations)} 次）")
    print(f"知识库          : {kb.size()} 个片段 / {len(kb.docs())} 篇文档")
    print("依赖状态        : " + ", ".join(f"{k}={v}" for k, v in report["dependencies"].items()))
    voice = report["presentation"]
    print(
        "离线语音        : "
        + (f"可用（{voice['engine']}）" if voice["available"] else "不可用 → 将回退纯文本（详情见 env_check.json）")
    )

    path = write_json(settings.PATHS["outputs"] / "env_check.json", report)
    print(f"\n[00_check_env] 环境检查报告已写入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
