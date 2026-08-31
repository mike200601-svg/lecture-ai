"""课表匹配测试。匹配不上必须优雅退化为 unknown，而不是报错。"""

from __future__ import annotations

from datetime import datetime

import pytest

from lecture_ai.errors import ConfigError
from lecture_ai.session.courses import load_courses

# 2026-09-02 是周三；2026-09-01 是周二
WED_1410 = datetime(2026, 9, 2, 14, 10)
WED_1335 = datetime(2026, 9, 2, 13, 35)   # 早于上课 25 分钟，在 30 分钟容差内
WED_1300 = datetime(2026, 9, 2, 13, 0)    # 早 60 分钟，超出容差
TUE_0830 = datetime(2026, 9, 1, 8, 30)
SUN_0900 = datetime(2026, 9, 6, 9, 0)


@pytest.fixture
def registry(config):
    return load_courses(config.courses_path)


def test_match_inside_slot(registry):
    assert registry.match(WED_1410, 30).key == "quantum_mechanics"


def test_match_within_tolerance(registry):
    """提前到教室就开始录音是常态，容差必须管用。"""
    assert registry.match(WED_1335, 30).key == "quantum_mechanics"


def test_outside_tolerance_falls_back(registry):
    assert registry.match(WED_1300, 30).key == "unknown"


def test_zero_tolerance_is_strict(registry):
    assert registry.match(WED_1335, 0).key == "unknown"
    assert registry.match(WED_1410, 0).key == "quantum_mechanics"


def test_different_weekday(registry):
    assert registry.match(TUE_0830, 30).key == "electrodynamics"


def test_no_class_returns_default(registry):
    assert registry.match(SUN_0900, 30).key == "unknown"


def test_course_metadata_loaded(registry):
    course = registry.get("quantum_mechanics")
    assert course.name == "量子力学"
    assert course.teacher == "张老师"
    assert course.glossary == "quantum_mechanics.txt"


def test_unknown_key_returns_default(registry):
    assert registry.get("nonexistent").key == "unknown"


def test_missing_file_does_not_crash(tmp_path):
    """课表文件不存在也要能跑 —— 转录不能被配置问题卡住。"""
    registry = load_courses(tmp_path / "nope.yaml")
    assert registry.match(WED_1410, 30).key == "unknown"


def test_default_course_always_present(tmp_path):
    (tmp_path / "c.yaml").write_text("courses:\n  physics:\n    name: 物理\n", encoding="utf-8")
    registry = load_courses(tmp_path / "c.yaml")
    assert "unknown" in registry
    assert registry.default.key == "unknown"


def test_invalid_weekday_raises(tmp_path):
    (tmp_path / "c.yaml").write_text(
        'courses:\n  x:\n    name: X\n    schedule:\n      - weekday: 9\n'
        '        start: "10:00"\n        end: "11:00"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="weekday"):
        load_courses(tmp_path / "c.yaml")


def test_invalid_time_raises(tmp_path):
    (tmp_path / "c.yaml").write_text(
        'courses:\n  x:\n    name: X\n    schedule:\n      - weekday: 1\n'
        '        start: "两点"\n        end: "11:00"\n',
        encoding="utf-8",
    )
    with pytest.raises(ConfigError):
        load_courses(tmp_path / "c.yaml")


def test_overlapping_courses_pick_nearest(tmp_path):
    """两门课时间重叠时取距离最近的，而不是字典序第一个。"""
    (tmp_path / "c.yaml").write_text(
        'courses:\n'
        '  a:\n    name: A\n    schedule:\n      - weekday: 3\n'
        '        start: "08:00"\n        end: "09:00"\n'
        '  b:\n    name: B\n    schedule:\n      - weekday: 3\n'
        '        start: "10:00"\n        end: "11:00"\n',
        encoding="utf-8",
    )
    registry = load_courses(tmp_path / "c.yaml")
    # 09:40 距 A 结束 40 分钟、距 B 开始 20 分钟 -> 应选 B
    assert registry.match(datetime(2026, 9, 2, 9, 40), 60).key == "b"
