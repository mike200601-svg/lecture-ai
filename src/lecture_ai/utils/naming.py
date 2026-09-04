"""Session 的对外显示身份与文件命名。

`export-package`（人工走 GPT 网页）和 `note`（走 API）都要产出同名的成稿文件，
命名规则必须只有一处定义，否则两条路线迟早会各写各的。

身份前缀形如 ``2026-09-01_0943_数字电子技术基础_001``：日期 + 开始时间 + 课程名 + 序号。
metadata 里缺失的字段写成 ``unknown-*``，不猜。
"""

from __future__ import annotations

import re

from lecture_ai.session.models import SessionMeta
from lecture_ai.utils.timefmt import parse_iso

#: Windows 文件名禁用字符。课程名直接来自配置，必须过滤后才能进文件名。
WINDOWS_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_course_label(value: str, max_len: int = 60) -> str:
    """生成文件名安全的课程显示名，同时保留 ``计算物理B`` 里的大小写。"""
    label = WINDOWS_UNSAFE_FILENAME.sub("-", value.strip())
    label = re.sub(r"[\s_]+", "-", label)
    label = re.sub(r"-{2,}", "-", label).strip("-.")
    label = label[:max_len].rstrip("-.")
    return label or "unknown-course"


def time_from_session_id(session_id: str) -> str:
    """新式 session 目录名里带 HHMM；旧式目录名没有，返回 ``unknown-time``。"""
    match = re.match(r"^\d{4}-\d{2}-\d{2}_(\d{4})_", session_id)
    return match.group(1) if match else "unknown-time"


def identity_prefix(meta: SessionMeta) -> str:
    """一节课的对外身份：``日期_时间_课程名_序号``。"""
    start = parse_iso(meta.start_time)
    date = meta.date or (start.strftime("%Y-%m-%d") if start else "unknown-date")
    time = start.strftime("%H%M") if start else time_from_session_id(meta.session_id)
    course = safe_course_label(meta.course.name or meta.course.key or "unknown-course")
    seq_match = re.search(r"_(\d{3})$", meta.session_id)
    sequence = seq_match.group(1) if seq_match else "001"
    return f"{date}_{time}_{course}_{sequence}"


def final_note_name(prefix: str) -> str:
    """成稿文件名。网页路线与 API 路线必须一致，否则同一节课会出现两种命名。"""
    return f"{prefix}_final_note.md"
