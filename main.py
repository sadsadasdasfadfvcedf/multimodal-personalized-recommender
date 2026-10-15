"""
main.py —— 多模态个性化推荐助手 · 一键端到端演示
=================================================

复现阶段1 的主链路：

    语音要礼物 → 多模态意图理解 → 多路召回 → 硬约束过滤 → 上下文重排
        → 证据包 → RAG 解释 → 文本/语音呈现 → 点赞反馈 → 重新排序

用法::

    python main.py                 # 默认演示（含语音播报）
    python main.py --no-voice      # 不合成语音
    python main.py --mode auto     # 尝试真实模型，不可用则自动降级
    python main.py --user 14156    # 指定用户

产物：outputs/main_demo.json（完整对话轨迹）
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import settings  # noqa: E402
from src.orchestrator import Orchestrator  # noqa: E402
from src.utils import now_iso, write_json  # noqa: E402

SCRIPT = [
    ("voice", "我要给朋友挑一个结婚礼物，500以内", "语音请求（ASR 文本）"),
    ("gesture", None, "手势点赞（thumbs_up）"),
    ("text", "为什么推荐第一个", "追问解释"),
]


def _print_turn(idx: int, label: str, resp: dict) -> None:
    print(f"\n─── 第 {idx} 轮 · {label} ───")
    print(f"trace_id={resp.get('trace_id')}  status={resp.get('status')}  session={resp.get('session_id')}")
    if resp.get("error"):
        print(f"  错误：{resp['error']['code']} - {resp['error']['message']}")
    intent = (resp.get("intent_result") or {}).get("intent")
    if intent:
        slots = {k: v.get("value") for k, v in ((resp.get("intent_result") or {}).get("slots") or {}).items()}
        print(f"  意图：{intent}  槽位：{slots}")
    for index, item in enumerate(resp.get("recommendations", [])[:5], start=1):
        rank = item.get("rank") or index
        score = item.get("score")
        score_text = f"{score:.3f}" if isinstance(score, (int, float)) else "—"
        print(f"  #{rank} {item.get('title')}  £{item.get('price')}  score={score_text}  来源={item.get('sources')}")
        if item.get("explanation"):
            print(f"      解释：{item['explanation'][:120]}")
    if resp.get("presentation", {}).get("text"):
        print("  【呈现】" + resp["presentation"]["text"].replace("\n", "\n          "))
    if resp.get("presentation", {}).get("voice_available"):
        print(f"  【语音】已合成：{resp['presentation'].get('audio_path')}（{resp['presentation'].get('engine')}）")
    if resp.get("feedback"):
        print(
            f"  【反馈】{resp['feedback'].get('polarity')} → {resp['feedback'].get('target_title')}"
            f"（正负互斥替换={resp['feedback'].get('replaced_previous')}）"
        )
    if resp.get("before_after"):
        ba = resp["before_after"]
        print(f"  【排序变化】changed={ba.get('changed')}")
        print(f"      反馈前 top3：{ba.get('before_topk')[:3]}")
        print(f"      反馈后 top3：{ba.get('after_topk')[:3]}")
    latency = (resp.get("trace") or {}).get("latency_ms") or {}
    print(
        f"  耗时：{latency.get('total_ms')}ms  "
        f"(事件{latency.get('event_ms')} 意图{latency.get('intent_ms')} "
        f"召回{latency.get('recall_ms')} 排序{latency.get('rank_ms')} 呈现{latency.get('present_ms')})"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="多模态个性化推荐助手 · 一键演示")
    parser.add_argument("--user", default="15547", help="演示用户 ID")
    parser.add_argument("--session", default=None, help="会话 ID（默认自动生成）")
    parser.add_argument(
        "--mode", default=None, choices=["offline", "auto", "online"], help="运行模式（默认取 config.RUNTIME['mode']）"
    )
    parser.add_argument("--no-voice", action="store_true", help="不合成语音")
    args = parser.parse_args()

    settings.ensure_dirs()
    orch = Orchestrator(mode=args.mode, with_voice=not args.no_voice)
    session_id = args.session or orch.store.default_session_id(args.user)

    print("=" * 72)
    print("多模态个性化推荐助手 · 端到端演示")
    print("=" * 72)
    print(f"用户={args.user}  会话={session_id}")
    print(
        f"运行模式={orch.mode}  多模态适配器={orch.multimodal.active_mode}  推荐适配器={orch.recommender.active_mode}"
    )
    print(f"商品目录={orch.catalog.size()} 件（{orch.catalog.model_version}）")

    transcript: list[dict] = []
    for idx, (source, text, label) in enumerate(SCRIPT, start=1):
        if source == "gesture":
            spec = {"source": "gesture", "gesture": {"name": "thumbs_up", "confidence": 0.92}}
        else:
            spec = {"source": source, "text": text}
        result = orch.handle(spec, user_id=args.user, session_id=session_id)
        if not result.ok:
            print(f"第 {idx} 轮失败：{result.error}")
            return 1
        resp = result.data
        _print_turn(idx, label, resp)
        transcript.append({"turn": idx, "label": label, "input": spec, "response": resp})

    report = {
        "generated_at": now_iso(),
        "user_id": args.user,
        "session_id": session_id,
        "modes": orch.modes(),
        "catalog": orch.catalog.describe(),
        "turns": transcript,
        "adapter_report": orch.adapter_report(),
    }
    path = write_json(settings.PATHS["outputs"] / "main_demo.json", report)
    print(f"\n[main] 完整轨迹已写入 {path}")

    session = orch.store.get(session_id)
    if session:
        print(
            f"[main] 会话状态：轮次={len(session.turns)} 点赞={dict(session.likes)} "
            f"倒赞={dict(session.dislikes)} 生效槽位={list(session.slot_state)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
