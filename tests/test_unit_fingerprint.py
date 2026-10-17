"""tests/_fingerprint.py —— 证据指纹的归一化口径。

回归锁：证据里含绝对路径（config.project_root / paths.* / catalog_origin /
audio_path）。这些路径只随「项目放在哪个目录」变化，不代表行为变化；
不归一化会让「换个目录重跑」产生一批假失败。
"""

from __future__ import annotations

from _fingerprint import norm
from conftest import PROJECT_ROOT


def test_absolute_paths_outside_project_collapse_to_basename():
    a = {"x": r"C:\Users\alice\work\out.json"}
    b = {"x": r"D:\tmp\work\out.json"}
    assert norm(a) == norm(b) == {"x": "<ABS>/out.json"}


def test_project_root_collapses_to_placeholder():
    real = str(PROJECT_ROOT / "outputs" / "stage01_contract_report.json")
    assert norm({"p": real}) == {"p": "<ROOT>/outputs/stage01_contract_report.json"}


def test_relative_paths_are_untouched():
    assert norm({"p": "outputs/x.json"}) == {"p": "outputs/x.json"}


def test_embedded_absolute_path_is_normalized():
    value = "offline_snapshot:" + str(PROJECT_ROOT / "data" / "offline")
    assert norm({"catalog_origin": value}) == {"catalog_origin": "offline_snapshot:<ROOT>/data/offline"}
