"""回归锁：证据指纹必须与黄金基准一致。

这是整套测试里最强的一层 —— 它不关心「脚本有没有报错」，
而是直接比对 13 个证据文件在归一化（剔除 trace_id / session_id / 时间戳）之后的
内容哈希。任何未被察觉的行为漂移都会在这里暴露。

什么时候需要更新黄金基准
------------------------
只有当**行为确实按预期改变**时（例如修了一个缺陷、调整了排序权重），
才用 `python tests/update_golden.py` 重新生成，并把它当作一次有意识的决策。
重构、改名、换路径写法这类「不应改变行为」的改动，绝不应该需要更新它。
"""

from __future__ import annotations

import json

from _fingerprint import fingerprint_dir
from conftest import GOLDEN_FINGERPRINT, SUITE_OUTPUTS


def test_golden_file_exists():
    assert GOLDEN_FINGERPRINT.is_file(), "缺少黄金指纹基准，先运行：python tests/update_golden.py"


def test_evidence_matches_golden(evidence):
    golden = json.loads(GOLDEN_FINGERPRINT.read_text(encoding="utf-8"))
    now = fingerprint_dir(evidence, SUITE_OUTPUTS)

    missing = sorted(set(golden) - set(now))
    assert not missing, f"黄金基准里的证据文件消失：{missing}"

    changed = sorted(k for k in golden if golden[k] != now.get(k))
    assert not changed, (
        "证据内容发生漂移（行为可能被改变）：\n  "
        + "\n  ".join(changed)
        + "\n\n如果这是预期的行为变更，运行 python tests/update_golden.py 更新基准；"
        + "如果只是一次重构，说明改动破坏了行为，应当回退。"
    )
