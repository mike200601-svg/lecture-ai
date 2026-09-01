"""Ingestion 测试：稳定性判定、去重、起始时间推断。"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from lecture_ai.ingestion.scanner import AudioScanner, guess_start_time, is_stable
from tests.conftest import make_wav


# --------------------------------------------------------------------- 稳定性


def test_recently_modified_file_is_unstable(tmp_path):
    """刚写入的文件不能碰 —— 手机可能还在同步。"""
    f = tmp_path / "a.wav"
    f.write_bytes(b"x" * 100)
    assert is_stable(f, quiet_seconds=60) is False


def test_quiet_file_is_stable(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x" * 100)
    _age_file(f, seconds=120)
    assert is_stable(f, stable_checks=1, quiet_seconds=10) is True


def test_growing_file_is_unstable(tmp_path):
    """模拟同步中的文件：大小在变，即使 mtime 够旧也不能处理。"""
    f = tmp_path / "a.wav"
    samples: dict[Path, list] = {}

    f.write_bytes(b"x" * 100)
    _age_file(f, seconds=120)
    assert is_stable(f, stable_checks=2, quiet_seconds=10, _samples=samples) is False  # 首次采样

    f.write_bytes(b"x" * 200)  # 又长大了
    _age_file(f, seconds=120)
    assert is_stable(f, stable_checks=2, quiet_seconds=10, _samples=samples) is False


def test_stable_after_repeated_identical_samples(tmp_path):
    f = tmp_path / "a.wav"
    f.write_bytes(b"x" * 100)
    _age_file(f, seconds=120)
    samples: dict[Path, list] = {}
    assert is_stable(f, stable_checks=2, quiet_seconds=10, _samples=samples) is False
    assert is_stable(f, stable_checks=2, quiet_seconds=10, _samples=samples) is True


def test_missing_file_is_unstable(tmp_path):
    assert is_stable(tmp_path / "nope.wav") is False


# --------------------------------------------------------------------- 扫描


def test_scanner_finds_audio(config, db):
    make_wav(config.paths.incoming_audio / "rec1.wav", seconds=1)
    _age_dir(config.paths.incoming_audio)
    found = AudioScanner(config, db).scan()
    assert len(found) == 1
    assert found[0].path.name == "rec1.wav"
    assert len(found[0].sha256) == 64


def test_scanner_ignores_non_audio(config, db):
    (config.paths.incoming_audio / "notes.txt").write_text("hi", encoding="utf-8")
    (config.paths.incoming_audio / ".hidden.wav").write_bytes(b"x")
    _age_dir(config.paths.incoming_audio)
    assert AudioScanner(config, db).scan() == []


def test_scanner_dedups_by_content(config, db):
    """同一份录音换个名字再放进来，不能重复处理。"""
    a = make_wav(config.paths.incoming_audio / "rec.wav", seconds=1)
    copy = config.paths.incoming_audio / "rec_副本.wav"
    copy.write_bytes(a.read_bytes())
    _age_dir(config.paths.incoming_audio)

    scanner = AudioScanner(config, db)
    found = scanner.scan()
    assert len(found) == 2  # 入库前两个都算新的

    db.insert_file(found[0].sha256, str(found[0].path), "audio", found[0].size)
    again = scanner.scan()
    assert again == [] or all(f.sha256 != found[0].sha256 for f in again)


def test_scanner_skips_unstable(config, db):
    make_wav(config.paths.incoming_audio / "rec.wav", seconds=1)  # 刚写入，未变旧
    config.processing.quiet_seconds = 300
    assert AudioScanner(config, db).scan() == []


# --------------------------------------------------------------------- 起始时间


@pytest.mark.parametrize(
    "name,expected",
    [
        ("录音_20260903_140000.m4a", datetime(2026, 9, 3, 14, 0, 0)),
        ("20260903-140000.mp3", datetime(2026, 9, 3, 14, 0, 0)),
        ("REC 2026-09-03 14.00.00.wav", datetime(2026, 9, 3, 14, 0, 0)),
        ("2026_09_03 14_30.m4a", datetime(2026, 9, 3, 14, 30, 0)),
    ],
)
def test_start_time_from_filename(tmp_path, name, expected):
    f = tmp_path / name
    f.write_bytes(b"x")
    guess = guess_start_time(f)
    assert guess.dt == expected
    assert guess.source == "filename"
    assert guess.confidence == "high"


def test_creation_time_wins(tmp_path):
    """容器元数据最可靠，优先于文件名。"""
    f = tmp_path / "录音_20260903_140000.m4a"
    f.write_bytes(b"x")
    creation = datetime(2026, 9, 3, 16, 0)
    guess = guess_start_time(f, creation_time=creation)
    assert guess.source == "ffprobe"
    assert guess.confidence == "high"


def test_creation_time_near_recording_end_does_not_override_filename(tmp_path):
    """某些 Android 录音机把封口时间写进 creation_time，文件名才是起点。"""
    f = tmp_path / "Recorder - 20260901-0943.m4a"
    f.write_bytes(b"x")

    guess = guess_start_time(
        f,
        duration_sec=5682.381,
        creation_time=datetime(2026, 9, 1, 11, 22, 19),
    )

    assert guess.dt == datetime(2026, 9, 1, 9, 43)
    assert guess.source == "filename"
    assert guess.confidence == "high"


def test_start_time_from_mtime_minus_duration(tmp_path):
    """无元数据无文件名时间时：mtime 通常是录音结束时刻。"""
    f = tmp_path / "audio.m4a"
    f.write_bytes(b"x")
    end = datetime(2026, 9, 3, 15, 40)
    os.utime(f, (end.timestamp(), end.timestamp()))

    guess = guess_start_time(f, duration_sec=5400)  # 90 分钟
    assert guess.source == "mtime-duration"
    assert guess.confidence == "medium"
    assert abs((guess.dt - (end - timedelta(seconds=5400))).total_seconds()) < 2


def test_start_time_fallback_low_confidence(tmp_path):
    f = tmp_path / "audio.m4a"
    f.write_bytes(b"x")
    guess = guess_start_time(f)
    assert guess.confidence == "low"


# --------------------------------------------------------------------- helpers


def _age_file(path: Path, seconds: int) -> None:
    old = time.time() - seconds
    os.utime(path, (old, old))


def _age_dir(directory: Path, seconds: int = 120) -> None:
    for p in directory.iterdir():
        if p.is_file():
            _age_file(p, seconds)


def test_one_shot_scan_finds_file_in_single_pass(config, db):
    """一次性 scan 必须在单次调用内完成稳定性判定。

    回归用：曾经采样历史只在进程内累积，导致 `lecture-ai scan`
    在默认 stable_checks=2 下永远返回空。
    """
    config.processing.stable_checks = 2
    make_wav(config.paths.incoming_audio / "rec.wav", seconds=1)
    _age_dir(config.paths.incoming_audio)

    scanner = AudioScanner(config, db, one_shot=True)
    scanner.resample_delay = 0.05  # 测试里不真等 1 秒
    assert len(scanner.scan()) == 1


def test_watch_mode_needs_two_polls(config, db):
    """watch 模式不就地等待，靠两轮轮询之间的间隔累积采样。"""
    config.processing.stable_checks = 2
    make_wav(config.paths.incoming_audio / "rec.wav", seconds=1)
    _age_dir(config.paths.incoming_audio)

    scanner = AudioScanner(config, db, one_shot=False)
    assert scanner.scan() == []
    assert len(scanner.scan()) == 1
