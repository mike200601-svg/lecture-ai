"""转录进度文件的读写契约。

这层的价值全在「坏情况下不出事」：进度只是给人看的，
写不进去、读到坏内容、进程死了留下残留文件——任何一种都不许影响转录本身。
"""

from __future__ import annotations

import json
import time

import pytest

from lecture_ai.pipeline.progress import (
    STALE_AFTER_SEC,
    ProgressWriter,
    progress_path,
    read_progress,
)


def test_roundtrip_reports_percent(tmp_path):
    writer = ProgressWriter(tmp_path)
    writer.update(600.0, 2400.0, force=True)

    got = read_progress(tmp_path)
    assert got["done_sec"] == 600.0
    assert got["total_sec"] == 2400.0
    assert got["percent"] == 25.0
    assert got["stale"] is False


def test_missing_file_reads_as_none(tmp_path):
    assert read_progress(tmp_path) is None


def test_throttle_skips_writes_but_force_always_lands(tmp_path):
    writer = ProgressWriter(tmp_path, min_interval=3600)
    writer.update(10.0, 100.0, force=True)
    writer.update(50.0, 100.0)                 # 被节流吃掉
    assert read_progress(tmp_path)["done_sec"] == 10.0

    writer.update(90.0, 100.0, force=True)     # force 无视节流
    assert read_progress(tmp_path)["done_sec"] == 90.0


def test_zero_total_does_not_divide_by_zero(tmp_path):
    ProgressWriter(tmp_path).update(0.0, 0.0, force=True)
    assert read_progress(tmp_path)["percent"] is None


def test_percent_never_exceeds_100(tmp_path):
    """VAD 会让最后一段的 end 略超过容器时长，别让面板显示 103%。"""
    ProgressWriter(tmp_path).update(105.0, 100.0, force=True)
    assert read_progress(tmp_path)["percent"] == 100.0


def test_old_progress_is_flagged_stale(tmp_path):
    writer = ProgressWriter(tmp_path)
    writer.update(30.0, 100.0, force=True)

    data = json.loads(progress_path(tmp_path).read_text(encoding="utf-8"))
    data["updated_at"] = time.time() - STALE_AFTER_SEC - 10
    progress_path(tmp_path).write_text(json.dumps(data), encoding="utf-8")

    got = read_progress(tmp_path)
    assert got["stale"] is True
    assert got["percent"] == 30.0      # 仍然如实报数，只是标记为可疑


@pytest.mark.parametrize("junk", ["", "not json", "[1,2,3]", '{"done_sec": "abc"}'])
def test_corrupt_file_reads_as_none_instead_of_raising(tmp_path, junk):
    path = progress_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(junk, encoding="utf-8")
    assert read_progress(tmp_path) is None


def test_clear_removes_file_and_is_idempotent(tmp_path):
    writer = ProgressWriter(tmp_path)
    writer.update(1.0, 2.0, force=True)
    writer.clear()
    assert read_progress(tmp_path) is None
    writer.clear()      # 再来一次不该炸


def test_write_failure_never_raises(tmp_path, monkeypatch):
    """磁盘满、目录只读之类的情况绝不能把转录带崩。"""
    writer = ProgressWriter(tmp_path)

    def boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr("pathlib.Path.write_text", boom)
    writer.update(1.0, 2.0, force=True)     # 不抛就算过
