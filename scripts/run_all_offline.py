"""
scripts/run_all_offline.py
==========================

一键跑完整离线套件：依次执行 00~06、08、12 九个脚本，汇总通过情况。

用途：验收现场第一件事跑这个，一次确认整套链路是否健康。
（可选的真实模式脚本 07/09/10/11 与网页验收 13 依赖外部服务或设备，不在此列。）

用法：
    python scripts/run_all_offline.py            # 跑全部
    python scripts/run_all_offline.py --quiet    # 只打印汇总
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time

from _bootstrap import PROJECT_ROOT, banner

SUITE = [
    ("00", "环境检查", "00_check_env.py"),
    ("01", "契约与降级矩阵", "01_contract_test.py"),
    ("02", "意图与会话上下文", "02_context_test.py"),
    ("03", "编排 / 召回 / 精排", "03_orchestrator_demo.py"),
    ("04", "证据与解释 / 呈现", "04_explanation_test.py"),
    ("05", "反馈闭环 A/B", "05_feedback_ab.py"),
    ("06", "端到端与故障降级", "06_e2e_acceptance.py"),
    ("08", "文档生成", "08_gen_docs.py"),
    ("12", "文本规范化", "12_text_norm_check.py"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="一键跑完整离线套件")
    parser.add_argument("--quiet", action="store_true", help="不打印各脚本输出")
    args = parser.parse_args()

    banner("离线套件一键运行（9 个脚本）")
    rows: list[tuple[str, str, str, float]] = []
    total_start = time.perf_counter()

    for stage, name, script in SUITE:
        if not args.quiet:
            print(f"\n{'─' * 60}\n▶ 阶段{stage} · {name}　({script})\n{'─' * 60}")
        start = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-B", str(PROJECT_ROOT / "scripts" / script)],
            capture_output=args.quiet, text=True, encoding="utf-8", errors="replace",
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        cost = time.perf_counter() - start
        if args.quiet and proc.stdout:
            print(proc.stdout.rstrip().splitlines()[-1] if proc.stdout.strip() else "")
        rows.append((stage, name, "PASS" if proc.returncode == 0 else "FAIL", cost))

    print(f"\n{'=' * 60}")
    print("离线套件汇总")
    print(f"{'=' * 60}")
    print(f"{'阶段':<6}{'模块':<22}{'结果':<8}{'耗时':>8}")
    for stage, name, verdict, cost in rows:
        mark = "✅" if verdict == "PASS" else "❌"
        print(f"{stage:<6}{name:<22}{mark} {verdict:<6}{cost:>7.1f}s")
    failed = [r for r in rows if r[2] != "PASS"]
    print(f"{'-' * 60}")
    print(f"合计 {len(rows) - len(failed)}/{len(rows)} 通过，"
          f"总耗时 {time.perf_counter() - total_start:.1f}s")
    if failed:
        print("未通过：" + "、".join(f"阶段{s}" for s, _, _, _ in failed))
    else:
        print("证据已刷新到 outputs/，可用 `python scripts/08_gen_docs.py` 同步文档。")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
