"""
scripts/11_llm_model_compare.py
===============================

LLM 选型对比：在**同一批证据、同一套门控**下比较不同真实模型的可用率。

对比口径（严格一致）：
    - 同一批商品（由真实推荐链路产出，不写死商品 ID）
    - 同一套强约束提示词、同一组生成参数
    - 同一套 事实门控 + 格式门控 + 规范化
    唯一变量 = 生成模型

指标：accept_rate（通过门控的比例）、耗时、以及被拦截的具体原因。
accept_rate 直接等价于「解释里不编造」的达标率，是本项目的核心红线。

用法：
    python scripts/11_llm_model_compare.py                # 自动发现所有可用 LLM
    python scripts/11_llm_model_compare.py --models Qwen2-1.5B-Instruct Qwen3-4B-Instruct-2507
    python scripts/11_llm_model_compare.py --limit 6

输出：outputs/llm_model_compare.json
"""
from __future__ import annotations

import argparse
import os
import time

from _bootstrap import banner

from config import settings
from src.adapters import MultimodalAdapter
from src.catalog import Catalog
from src.contracts import make_event
from src.evidence import build_pack
from src.explain import explain
from src.intent import understand
from src.llm_explain import build_grounded_prompt, evaluate_output
from src.rag import KnowledgeBase
from src.utils import now_iso, write_json

QUERY = {"source": "voice", "text": "500以内的婚礼装饰品"}


def _discover_models(adapter: MultimodalAdapter) -> list[dict]:
    result = adapter._run_worker("list_llm_models", {}, timeout_s=60)
    if not result.ok:
        return []
    models = (result.data or {}).get("models", [])
    # 视觉语言模型（VL）不属于纯文本解释链路，排除
    return [m for m in models if "-VL-" not in m["name"].upper()]


def _prepare_samples(orchestrator, catalog, kb, limit: int) -> list[dict]:
    resp = orchestrator.handle(QUERY, user_id="15547", session_id="llm-compare").data
    event = make_event(user_id="15547", source="voice", text=QUERY["text"])
    intent = understand(event, catalog_categories=catalog.categories(),
                        session_id="llm-compare", mode="offline")
    request = intent.data["request"]

    samples: list[dict] = []
    for item in resp.get("recommendations", [])[:limit]:
        pack_result = build_pack(item, request=request, catalog=catalog, session=None, kb=kb,
                                 sources=item.get("sources"),
                                 matched_constraints=item.get("evidence", {}).get("matched_constraints"))
        if not pack_result.ok:
            continue
        pack = pack_result.data
        base = explain(item, pack)
        samples.append({"item": item, "pack": pack,
                        "template": base.data["text"] if base.ok else "",
                        "prompt": build_grounded_prompt(item, pack)})
    return samples


def _evaluate(adapter: MultimodalAdapter, model: dict, samples: list[dict], catalog) -> dict:
    cfg = settings.LLM_EXPLAIN_CONFIG
    record: dict = {"model": model["name"], "path": model["path"], "size_mb": model["size_mb"],
                    "samples": [], "error": None}
    started = time.perf_counter()
    batch = adapter.generate_batch([s["prompt"] for s in samples],
                                   max_new_tokens=cfg["max_new_tokens"],
                                   temperature=cfg["temperature"])
    record["seconds"] = round(time.perf_counter() - started, 2)
    if not batch.ok:
        record["error"] = batch.error.to_dict() if batch.error else {"message": "unknown"}
        record["accept_rate"] = None
        return record

    outputs = (batch.data or {}).get("outputs", [])
    record["seconds_per_explanation"] = round(record["seconds"] / max(1, len(outputs)), 2)
    accepted = 0
    reasons: list[str] = []
    for sample, out in zip(samples, outputs):
        raw_output = str(out.get("text") or "")
        evaluated = evaluate_output(raw_output, sample["item"], sample["pack"], catalog)
        ok_flag = evaluated["accepted"]
        accepted += 1 if ok_flag else 0
        if not ok_flag:
            reasons.extend(v.get("type", "?") for v in evaluated["rejection"]["violations"])
        record["samples"].append({
            "item_id": sample["item"].get("item_id"),
            "title": sample["item"].get("title"),
            "raw": raw_output[:300],
            "normalized": evaluated["text"],
            "fluency_actions": evaluated["fluency_actions"],
            "fact_violations": evaluated["fact"].violations[:3],
            "format_violations": evaluated["format"],
            "fluency_violations": evaluated["fluency"],
            "accepted": ok_flag,
            "final_text": evaluated["text"] if ok_flag else sample["template"],
        })
    record["accepted"] = accepted
    record["attempted"] = len(outputs)
    record["accept_rate"] = round(accepted / max(1, len(outputs)), 4)
    record["reject_reasons"] = sorted(set(reasons))
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 选型对比")
    parser.add_argument("--models", nargs="*", default=None, help="模型目录名；默认自动发现")
    parser.add_argument("--limit", type=int, default=4, help="样本条数")
    args = parser.parse_args()

    banner("LLM 选型对比 · 同一批证据 + 同一套门控")
    report: dict = {"checked_at": now_iso(), "query": QUERY, "models": []}

    if not settings.RUNTIME["allow_real_models"]:
        report.update({"mode": "skipped", "reason": "未启用真实模型（INTEGRATED_ALLOW_REAL_MODELS=1）"})
        write_json(settings.PATHS["outputs"] / "llm_model_compare.json", report)
        print("跳过：未启用真实模型")
        return 0

    adapter = MultimodalAdapter(mode="auto", allow_real=True)
    if adapter.active_mode != "real":
        report.update({"mode": "skipped", "reason": "适配器未进入 real 模式"})
        write_json(settings.PATHS["outputs"] / "llm_model_compare.json", report)
        print("跳过：适配器未进入 real 模式")
        return 0

    from src.orchestrator import Orchestrator

    catalog = Catalog.from_snapshot()
    kb = KnowledgeBase().load()
    # 样本只用离线链路产出，保证各模型面对的是完全相同的输入
    samples = _prepare_samples(Orchestrator(with_voice=False), catalog, kb, args.limit)
    if not samples:
        print("没有可用样本")
        return 1
    report["samples"] = [{"item_id": s["item"].get("item_id"), "title": s["item"].get("title"),
                          "price": s["item"].get("price"),
                          "facts": s["pack"].product_facts} for s in samples]
    print(f"样本 {len(samples)} 条（同一批商品与提示词）")

    discovered = _discover_models(adapter)
    report["discovered"] = discovered
    if args.models:
        wanted = set(args.models)
        models = [m for m in discovered if m["name"] in wanted]
        missing = wanted - {m["name"] for m in models}
        if missing:
            print(f"警告：以下模型当前不可用（权重缺失或未下载）：{sorted(missing)}")
    else:
        models = discovered
    if not models:
        print("没有可对比的模型")
        return 1

    original_path = os.environ.get("MULTIMODAL_LLM_PATH")
    try:
        for model in models:
            os.environ["MULTIMODAL_LLM_PATH"] = model["path"]
            print(f"→ 评测 {model['name']}（{model['size_mb']} MB）…")
            record = _evaluate(adapter, model, samples, catalog)
            report["models"].append(record)
            if record.get("error"):
                print(f"   加载/生成失败：{record['error'].get('message', '')[:80]}")
                continue
            print(f"   接受率 {record['accept_rate']:.0%}（{record['accepted']}/{record['attempted']}）"
                  f"，耗时 {record['seconds']}s（均 {record['seconds_per_explanation']}s/条）")
            if record["reject_reasons"]:
                print(f"   拦截类型：{record['reject_reasons']}")
    finally:
        if original_path is None:
            os.environ.pop("MULTIMODAL_LLM_PATH", None)
        else:
            os.environ["MULTIMODAL_LLM_PATH"] = original_path

    ok_models = [m for m in report["models"] if m.get("accept_rate") is not None]
    report["comparison"] = sorted(
        [{"model": m["model"], "size_mb": m["size_mb"], "accept_rate": m["accept_rate"],
          "accepted": m["accepted"], "attempted": m["attempted"],
          "seconds_per_explanation": m["seconds_per_explanation"],
          "reject_reasons": m["reject_reasons"]} for m in ok_models],
        key=lambda x: (-x["accept_rate"], x["seconds_per_explanation"]))
    report["best"] = report["comparison"][0]["model"] if report["comparison"] else None
    report["notes"] = [
        "accept_rate = 通过事实门控 + 格式门控的比例，等价于「解释不编造」的达标率。",
        "模型间唯一变量是生成模型；商品、提示词、采样参数、门控规则完全一致。",
        "子进程每次加载权重，因此 seconds_per_explanation 含加载摊销；批量越大摊销越低。",
    ]

    path = write_json(settings.PATHS["outputs"] / "llm_model_compare.json", report)
    print("")
    print("对比结果：")
    for row in report["comparison"]:
        print(f"  {row['model']:<28} 接受率 {row['accept_rate']:>5.0%}  "
              f"({row['accepted']}/{row['attempted']})  {row['seconds_per_explanation']}s/条  "
              f"{row['size_mb']}MB")
    print(f"\n最优：{report['best']}")
    print(f"[11_llm_model_compare] 证据已写入 {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
