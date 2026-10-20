"""src/utils.py —— 基础工具。

重点：write_json / append_jsonl 走的是「临时文件 + os.replace 原子替换」路径，
批次三会把 os.replace 改写成 Path.replace，这里锁住其可观察行为
（含中文路径 —— 这正是 mm_worker 里踩过的坑）。
"""

from __future__ import annotations

import sys

import pytest

from src import utils


@pytest.mark.parametrize(
    "text,needles,expected",
    [
        ("Hello World", ["world"], "world"),
        ("HELLO", ["hello"], "hello"),
        ("abc", ["x", "b"], "b"),
        ("abc", ["zzz"], None),
        ("", ["a"], None),
        (None, ["a"], None),
    ],
)
def test_contains_any_is_case_insensitive(text, needles, expected):
    assert utils.contains_any(text, needles) == expected


def test_clamp():
    assert utils.clamp(5, 0, 3) == 3
    assert utils.clamp(-5, 0, 3) == 0
    assert utils.clamp(2, 0, 3) == 2


def test_minmax_normalize_range():
    out = utils.minmax_normalize({"a": 1.0, "b": 3.0})
    assert out["a"] == pytest.approx(0.0)
    assert out["b"] == pytest.approx(1.0)


def test_truncate_keeps_short_text():
    assert utils.truncate("短文本", limit=60) == "短文本"


def test_truncate_marks_ellipsis():
    out = utils.truncate("字" * 70, limit=60)
    assert len(out) == 60
    assert out.endswith("…")


def test_stable_key_is_deterministic_and_order_sensitive():
    assert utils.stable_key("a", 1) == utils.stable_key("a", 1)
    assert utils.stable_key("a", 1) != utils.stable_key("1", "a")


def test_write_read_json_roundtrip_chinese_path(tmp_path):
    target = tmp_path / "中文目录" / "产物.json"
    payload = {"名称": "结婚礼物", "价格": 49.99, "标签": ["婚礼", "装饰品"]}
    written = utils.write_json(target, payload)
    assert written.exists()
    assert utils.read_json(target) == payload


def test_write_json_replaces_existing_file(tmp_path):
    target = tmp_path / "a.json"
    utils.write_json(target, {"v": 1})
    utils.write_json(target, {"v": 2})
    assert utils.read_json(target) == {"v": 2}


def test_write_json_leaves_no_temp_file(tmp_path):
    target = tmp_path / "b.json"
    utils.write_json(target, {"v": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "b.json"]
    assert leftovers == [], f"原子写残留临时文件：{leftovers}"


def test_read_json_returns_default_for_missing(tmp_path):
    assert utils.read_json(tmp_path / "nope.json", default={"d": 1}) == {"d": 1}


def test_append_and_read_jsonl_chinese_path(tmp_path):
    target = tmp_path / "记录" / "log.jsonl"
    utils.append_jsonl(target, [{"i": 1, "文": "甲"}])
    utils.append_jsonl(target, [{"i": 2, "文": "乙"}])
    rows = utils.read_jsonl(target)
    assert [r["i"] for r in rows] == [1, 2]
    assert rows[0]["文"] == "甲"


def test_trace_id_shape():
    tid = utils.new_trace_id()
    assert tid.startswith("tr-")
    assert len(tid) > 6


def test_now_iso_is_iso8601():
    assert utils.is_iso8601(utils.now_iso())


def test_ensure_utf8_stdio_reconfigures_console_streams(monkeypatch):
    calls: list[dict] = []

    class FakeStream:
        def reconfigure(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(sys, "stdin", FakeStream())
    monkeypatch.setattr(sys, "stdout", FakeStream())
    monkeypatch.setattr(sys, "stderr", FakeStream())

    utils.ensure_utf8_stdio()

    assert calls == [
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
        {"encoding": "utf-8", "errors": "replace"},
    ]
