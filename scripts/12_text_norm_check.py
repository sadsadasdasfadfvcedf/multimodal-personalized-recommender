"""
scripts/12_text_norm_check.py
=============================

文本规范化与流畅度门控验证（对应问题 P-16 / P-17）。

验证三件事：
    1. 繁体字检测与繁转简（内置 4056 字表来自 zhconv 权威词表推导）；
    2. 中英混排的英文词替换；
    3. 生成式模型的**循环复读截断**（VL 输出退化）。

设计口径：可选依赖 + 安全降级
    - 有 zhconv/opencc：转换后通过门控；
    - 无转换器：检测到繁体/残留英文即判不合格，由上层回退模板解释。

口径：宁可不展示，也不展示不规范文本。

输出：outputs/text_norm_check.json
"""

from __future__ import annotations

import importlib.util

from _bootstrap import banner

from config import settings
from src.text_norm import (
    TRADITIONAL_MARKERS,
    describe,
    find_traditional,
    normalize_fluency,
    normalize_input,
    verify_fluency,
)
from src.utils import now_iso, write_json

# 直接加载 worker，复用其中的循环截断实现（避免重复实现两份逻辑）
_SPEC = importlib.util.spec_from_file_location(
    "mm_worker_for_test", str(settings.PROJECT_ROOT / "scripts" / "mm_worker.py")
)
assert _SPEC is not None and _SPEC.loader is not None, "mm_worker 模块加载失败"
_WORKER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_WORKER)

RESULTS: list[dict] = []


def check(cid: str, desc: str, passed: bool, detail: dict) -> None:
    RESULTS.append({"case_id": cid, "description": desc, "passed": bool(passed), "detail": detail})


def main() -> int:
    banner("文本规范化与流畅度门控验证")
    report: dict = {"checked_at": now_iso(), "text_norm": describe(), "cases": []}

    # ---------- 1) 繁体检测 ----------
    simplified = "这款装饰品颜值高，价格实惠，属于你的预算范围内的选择。"
    traditional = "這款裝飾品顏值高，價格實惠，屬於你的預算範圍內的選擇。"
    check(
        "T-01", "简体文本不应被误判为含繁体", not find_traditional(simplified), {"hits": find_traditional(simplified)}
    )
    hits = find_traditional(traditional)
    check("T-02", "繁体文本应被检出", len(hits) >= 6, {"hits": hits})

    # ---------- 2) 繁转简 + 门控 ----------
    converted, actions = normalize_fluency(traditional)
    check(
        "T-03",
        "繁体文本应被转为简体",
        converted == simplified and not verify_fluency(converted),
        {"converted": converted, "actions": actions, "violations": verify_fluency(converted)},
    )

    # ---------- 3) 中英混排 ----------
    mixed = "这10個复古彩ball聖誕燈，價格實惠，推薦給你！"
    normalized, actions = normalize_fluency(mixed)
    check(
        "T-04",
        "英文词应被替换为中文",
        "ball" not in normalized and "球" in normalized,
        {"normalized": normalized, "actions": actions},
    )
    check(
        "T-05", "规范化后应通过流畅度门控", not verify_fluency(normalized), {"violations": verify_fluency(normalized)}
    )

    # ---------- 4) 未映射英文词应被拦截 ----------
    # ---------- 5) 无转换器时的降级行为 ----------

    # ---------- 4b) 未映射英文词：删除而非保留 ----------
    unmapped_text = "这款悬挂蝴蝶 tea 烛台是装饰品，价格实惠。"
    cleaned, actions = normalize_fluency(unmapped_text)
    check(
        "T-06b",
        "未映射英文词应被删除（不引入新内容）",
        "tea" not in cleaned and not verify_fluency(cleaned),
        {"cleaned": cleaned, "actions": actions},
    )

    # ---------- 4c) 英文占比过高应判不合格 ----------
    english_heavy = "This product is very nice and the price is affordable for you."
    cleaned_heavy, actions_heavy = normalize_fluency(english_heavy)
    violations_heavy = verify_fluency(cleaned_heavy)
    check(
        "T-06c",
        "英文占比过高时应判不合格（防止删空后误判合规）",
        any(v["type"] == "insufficient_chinese" for v in violations_heavy),
        {"cleaned": cleaned_heavy, "violations": violations_heavy, "actions": actions_heavy},
    )

    # ---------- 5) 无转换器时的降级行为 ----------
    check(
        "T-07",
        "无转换器时繁体字仍会被门控拦下（安全降级）",
        set(find_traditional(traditional)) >= {"這", "裝", "飾", "價"},
        {"marker_table": len(TRADITIONAL_MARKERS), "note": "内置字表保证即使没有 zhconv 也能拦截不规范文本"},
    )

    # ---------- 6) 循环复读截断 ----------
    loop = (
        "一个戴眼镜的男生，坐在电脑前，桌子上放着一些物品，桌子后面有一面墙，"
        "墙上挂着一些物品，桌子后面有一面墙，墙上挂着一些物品，桌子后面有一面墙，"
        "墙上挂着一些物品，桌子后面有一面墙，墙上挂着一些物品"
    )
    truncated, fired = _WORKER._truncate_repetition(loop)
    check(
        "T-08",
        "循环复读文本应被截断",
        fired and len(truncated) < len(loop),
        {"fired": fired, "before": len(loop), "after": len(truncated), "text": truncated[:70]},
    )
    normal = "画面里有一个人，他戴着眼镜，穿着白色的衣服，正在调整他的眼镜。"
    kept, fired_normal = _WORKER._truncate_repetition(normal)
    check("T-09", "正常文本不应被误截断", not fired_normal and kept == normal, {"fired": fired_normal})

    # ---------- 7) 输入侧归一化（ASR / 手输文本进入意图解析前） ----------
    # 与上面 1~6 的关键区别：1~6 治理的是**模型输出**（可以删词、可以截断）；
    # 这里治理的是**用户输入**，只做等价改写，绝不删除英文词、绝不截断。
    # 实测背景：ASR 会输出繁体（「300以內的水杯」原本整句解析不出预算），
    # 用户也会把「以内」写成「是内」，把「3 0 0」拆着写。
    input_cases = [
        ("T-10", "繁体输入应转为简体", "300以內的水杯", "300以内的水杯", "traditional_to_simplified"),
        ("T-11", "全角数字应转半角", "３００以内的水杯", "300以内的水杯", "fullwidth_to_halfwidth"),
        ("T-12", "被空格拆开的数字应折叠", "3 0 0 以内", "300 以内", "collapse_split_digits"),
        ("T-13", "「是内」应定向纠正为「以内」", "预算是内300的水杯", "预算以内300的水杯", "typo_corrected"),
        ("T-14", "非数量语境的「是内」不得被改写", "他是内行，推荐点东西", "他是内行，推荐点东西", None),
        ("T-15", "中文标点不得被转成半角", "推荐，看看。", "推荐，看看。", None),
        ("T-16", "英文词不得被删除（与生成侧区分）", "iPhone 手机壳 300以内", "iPhone 手机壳 300以内", None),
    ]
    for cid, desc, raw_text, expect, expect_action in input_cases:
        out, acts = normalize_input(raw_text)
        names = [a["action"] for a in acts]
        check(
            cid,
            desc,
            out == expect and (expect_action is None or expect_action in names),
            {"input": raw_text, "output": out, "actions": names, "expect": expect},
        )

    # ---------- 汇总 ----------
    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r["passed"])
    report["cases"] = RESULTS
    report["summary"] = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total, 4) if total else 0.0,
    }
    by_id = {r["case_id"]: r["passed"] for r in RESULTS}

    def _all(*ids: str) -> bool:
        return all(by_id.get(i, False) for i in ids)

    report["acceptance_checklist"] = {
        "用例全部通过（failed=0）": total > 0 and passed == total,
        "繁简判定与转换": _all("T-01", "T-02", "T-03"),
        "中英混排规范化与门控": _all("T-04", "T-05", "T-06b", "T-06c"),
        "无转换器时安全降级": _all("T-07"),
        "循环复读截断且不误伤正常文本": _all("T-08", "T-09"),
        "输入侧归一化：繁体/全角/拆位数字/定向纠错": _all("T-10", "T-11", "T-12", "T-13"),
        "输入侧归一化不得误伤：非数量语境/中文标点/英文词": _all("T-14", "T-15", "T-16"),
    }
    path = write_json(settings.PATHS["outputs"] / "text_norm_check.json", report)

    for r in RESULTS:
        print(f"[{'PASS' if r['passed'] else 'FAIL'}] {r['case_id']} {r['description']}")
        if not r["passed"]:
            print(f"        {r['detail']}")
    print(f"\n合计 {passed}/{total}")
    print(f"[12_text_norm_check] 证据已写入 {path.name}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
