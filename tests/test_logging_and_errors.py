"""日志与异常层次测试。

日志的重点是编码：项目路径、课程名、错误信息全是中文，
Windows 默认 GBK，一不小心就是满屏乱码或 UnicodeEncodeError。
"""

from __future__ import annotations

import logging

import pytest

from lecture_ai import errors
from lecture_ai.logging_setup import (
    attach_session_log,
    detach_session_log,
    get_logger,
)


# --------------------------------------------------------------------- 异常


@pytest.mark.parametrize(
    "cls",
    [
        errors.ConfigError,
        errors.DependencyMissing,
        errors.IngestError,
        errors.AudioError,
        errors.TranscriptionError,
        errors.SessionNotFound,
        errors.InvalidTransition,
    ],
)
def test_all_errors_inherit_base(cls):
    """CLI 顶层只 catch LectureAIError，所以每个自定义异常都必须继承它。"""
    assert issubclass(cls, errors.LectureAIError)


def test_invalid_transition_message():
    exc = errors.InvalidTransition("NEW", "DONE")
    assert "NEW" in str(exc) and "DONE" in str(exc)
    assert exc.current == "NEW" and exc.target == "DONE"


# --------------------------------------------------------------------- 日志


def test_session_log_is_utf8(tmp_path):
    """中文日志写进文件必须能原样读回来。"""
    session_dir = tmp_path / "2026-09-03_量子力学_001"
    handler = attach_session_log(session_dir, "s1")
    try:
        log = get_logger("lecture_ai.test", "s1")
        log.info("转录完成：薛定谔方程、厄米算符 ✔")
    finally:
        detach_session_log(handler)

    content = (session_dir / "logs" / "session.log").read_text(encoding="utf-8")
    assert "薛定谔方程" in content
    assert "厄米算符" in content
    assert "s1" in content


def test_session_log_only_captures_own_session(tmp_path):
    """两个 session 并发处理时，日志不能串到对方文件里。"""
    dir_a, dir_b = tmp_path / "a", tmp_path / "b"
    ha = attach_session_log(dir_a, "session-a")
    hb = attach_session_log(dir_b, "session-b")
    try:
        get_logger("lecture_ai.test", "session-a").info("属于A的消息")
        get_logger("lecture_ai.test", "session-b").info("属于B的消息")
    finally:
        detach_session_log(ha)
        detach_session_log(hb)

    text_a = (dir_a / "logs" / "session.log").read_text(encoding="utf-8")
    text_b = (dir_b / "logs" / "session.log").read_text(encoding="utf-8")
    assert "属于A的消息" in text_a and "属于B的消息" not in text_a
    assert "属于B的消息" in text_b and "属于A的消息" not in text_b


def test_logger_without_session_does_not_crash(tmp_path):
    """未绑定 session 的日志也要能格式化（占位为 '-'）。"""
    handler = attach_session_log(tmp_path / "s", "sid")
    try:
        get_logger("lecture_ai.test").info("没有 session 的消息")
    finally:
        detach_session_log(handler)
    # 不抛异常即通过；该消息因 session 不匹配不会进 session.log
    assert (tmp_path / "s" / "logs" / "session.log").exists()


def test_detach_removes_handler(tmp_path):
    before = len(logging.getLogger("lecture_ai").handlers)
    handler = attach_session_log(tmp_path / "s", "sid")
    assert len(logging.getLogger("lecture_ai").handlers) == before + 1
    detach_session_log(handler)
    assert len(logging.getLogger("lecture_ai").handlers) == before
