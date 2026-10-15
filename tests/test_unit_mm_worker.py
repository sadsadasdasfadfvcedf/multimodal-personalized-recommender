"""scripts/mm_worker.py —— 外部多模态项目的子进程桥接。

为什么这个文件是重点
--------------------
批次三（PTH：os.path -> pathlib）81 处里有 45 处集中在本文件，
是整个重构风险最集中的地方。本文件把它的可观察行为钉死，
尤其是「非 ASCII 路径图像读写」—— 这是文件顶部注释里明确记录过的踩坑点：
cv2.imread/imwrite 在 Windows 上无法处理中文路径，所以改用
np.fromfile + cv2.imdecode 的组合。重构时若把它改写回 cv2.imread，
测试会立刻失败。
"""

from __future__ import annotations

import importlib.util
import json

import numpy as np
import pytest
from conftest import PROJECT_ROOT

pytest.importorskip("cv2", reason="需要 opencv 才能测试图像编解码")


@pytest.fixture(scope="module")
def mm():
    path = PROJECT_ROOT / "scripts" / "mm_worker.py"
    spec = importlib.util.spec_from_file_location("mm_worker_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- 循环复读截断


@pytest.mark.parametrize("text", ["", "这是一句正常的话", "短"])
def test_truncate_repetition_leaves_normal_text_alone(mm, text):
    out, truncated = mm._truncate_repetition(text)
    assert out == text
    assert truncated is False


def test_truncate_repetition_detects_loop(mm):
    looped = "ABCDEFGH" * 4
    out, truncated = mm._truncate_repetition(looped)
    assert truncated is True
    assert len(out) < len(looped)
    assert out.endswith("。")


def test_truncate_repetition_handles_chinese_loop(mm):
    looped = "桌子后面有一面墙，" * 5
    out, truncated = mm._truncate_repetition(looped)
    assert truncated is True
    assert len(out) < len(looped)


# ------------------------------------------------------------ 非 ASCII 路径图像


def test_imwrite_imread_roundtrip_with_chinese_path(mm, tmp_path):
    image = np.zeros((8, 12, 3), dtype=np.uint8)
    image[:, :, 2] = 255  # BGR 的红色通道

    target = tmp_path / "中文目录" / "测试图.png"
    mm._imwrite(str(target), image)

    assert target.is_file()
    assert target.stat().st_size > 0

    back = mm._imread(str(target))
    assert back is not None
    assert back.shape == (8, 12, 3)
    assert back[0, 0, 2] == 255


def test_imwrite_creates_missing_parent_directories(mm, tmp_path):
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    target = tmp_path / "a" / "b" / "图.jpg"
    mm._imwrite(str(target), image)
    assert target.is_file()


def test_imwrite_defaults_to_jpg_without_extension(mm, tmp_path):
    image = np.zeros((4, 4, 3), dtype=np.uint8)
    target = tmp_path / "无扩展名"
    mm._imwrite(str(target), image)
    assert target.is_file() and target.stat().st_size > 0


def test_imread_returns_none_for_empty_file(mm, tmp_path):
    target = tmp_path / "空.jpg"
    target.write_bytes(b"")
    assert mm._imread(str(target)) is None


def test_imread_raises_for_missing_file(mm, tmp_path):
    with pytest.raises(FileNotFoundError):
        mm._imread(str(tmp_path / "不存在.jpg"))


# ------------------------------------------------------------------- 模型可用性


def test_usable_false_for_missing_directory(mm, tmp_path):
    assert mm._usable(str(tmp_path / "缺失")) is False


def test_usable_false_without_config(mm, tmp_path):
    (tmp_path / "weights.safetensors").write_bytes(b"x")
    assert mm._usable(str(tmp_path)) is False


def test_usable_false_without_weights(mm, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    assert mm._usable(str(tmp_path)) is False


def test_usable_true_with_config_and_safetensors(mm, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors").write_bytes(b"x")
    assert mm._usable(str(tmp_path)) is True


def test_usable_ignores_incomplete_weights(mm, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.incomplete").write_bytes(b"x")
    assert mm._usable(str(tmp_path)) is False


def test_usable_accepts_funasr_config_names(mm, tmp_path):
    (tmp_path / "configuration.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.pt").write_bytes(b"x")
    assert mm._usable(str(tmp_path)) is True


def test_usable_false_when_shard_missing(mm, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": "shard-1.safetensors"}}), encoding="utf-8"
    )
    assert mm._usable(str(tmp_path)) is False


def test_usable_true_when_all_shards_present(mm, tmp_path):
    (tmp_path / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / "model.safetensors.index.json").write_text(
        json.dumps({"weight_map": {"a": "shard-1.safetensors"}}), encoding="utf-8"
    )
    (tmp_path / "shard-1.safetensors").write_bytes(b"x")
    assert mm._usable(str(tmp_path)) is True


# -------------------------------------------------------------------------- MM_ROOT


def test_root_raises_without_env(mm, monkeypatch):
    monkeypatch.delenv("MM_ROOT", raising=False)
    with pytest.raises(EnvironmentError):
        mm._root()


def test_root_raises_when_dir_missing(mm, monkeypatch, tmp_path):
    monkeypatch.setenv("MM_ROOT", str(tmp_path / "不存在"))
    with pytest.raises(EnvironmentError):
        mm._root()


def test_root_accepts_existing_dir(mm, monkeypatch, tmp_path):
    monkeypatch.setenv("MM_ROOT", str(tmp_path))
    assert mm._root() == str(tmp_path)


# ----------------------------------------------------------------- stdout 协议


def test_ok_emits_single_json_line(mm, capsys):
    rc = mm._ok("asr", {"text": "你好"}, 12.3456)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True
    assert payload["action"] == "asr"
    assert payload["data"] == {"text": "你好"}
    assert payload["latency_ms"] == pytest.approx(12.346)


def test_fail_emits_structured_error(mm, capsys):
    rc = mm._fail("vl", ValueError("模型缺失"))
    assert rc == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert payload["action"] == "vl"
    assert payload["error"]["type"] == "ValueError"
    assert "模型缺失" in payload["error"]["message"]
