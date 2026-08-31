"""状态机与 SessionMeta 序列化测试。"""

from __future__ import annotations

import pytest

from lecture_ai.errors import InvalidTransition
from lecture_ai.session.models import (
    ALLOWED_TRANSITIONS,
    AudioInfo,
    CourseRef,
    SessionMeta,
    SessionState,
    StepStatus,
    check_transition,
)

LEGAL = [
    (SessionState.NEW, SessionState.AUDIO_READY),
    (SessionState.AUDIO_READY, SessionState.TRANSCRIBING),
    (SessionState.TRANSCRIBING, SessionState.TRANSCRIBED),
    (SessionState.TRANSCRIBED, SessionState.IMAGES_READY),
    (SessionState.IMAGES_READY, SessionState.FUSING),
    (SessionState.FUSING, SessionState.GENERATING_NOTE),
    (SessionState.GENERATING_NOTE, SessionState.EXPORTED),
    (SessionState.EXPORTED, SessionState.DONE),
]

ILLEGAL = [
    (SessionState.NEW, SessionState.TRANSCRIBED),        # 不能跳过转录
    (SessionState.NEW, SessionState.DONE),
    (SessionState.AUDIO_READY, SessionState.EXPORTED),
    (SessionState.TRANSCRIBED, SessionState.AUDIO_READY),  # 不能倒退
    (SessionState.DONE, SessionState.TRANSCRIBING),
]


@pytest.mark.parametrize("current,target", LEGAL)
def test_legal_transitions(current, target):
    check_transition(current, target)


@pytest.mark.parametrize("current,target", ILLEGAL)
def test_illegal_transitions(current, target):
    with pytest.raises(InvalidTransition):
        check_transition(current, target)


def test_same_state_is_idempotent():
    check_transition(SessionState.TRANSCRIBED, SessionState.TRANSCRIBED)


@pytest.mark.parametrize(
    "state",
    [s for s in SessionState if s not in (SessionState.DONE, SessionState.FAILED)],
)
def test_every_active_state_can_fail(state):
    """任何进行中的状态都必须能进 FAILED —— 否则出错就没地方落。"""
    assert SessionState.FAILED in ALLOWED_TRANSITIONS[state]


def test_metadata_roundtrip():
    meta = SessionMeta(
        session_id="2026-09-03_quantum-mechanics_001",
        course=CourseRef(key="quantum_mechanics", name="量子力学", teacher="张老师"),
        date="2026-09-03",
        start_time="2026-09-03T14:00:00+08:00",
        state=SessionState.TRANSCRIBED,
        audio=AudioInfo(raw="raw/录音.m4a", sha256="abc", duration_sec=5531.4),
        start_time_confidence="high",
    )
    meta.steps["transcribe"] = StepStatus(
        status="done", elapsed_sec=1633.2, provider="local_whisper", model="large-v3-turbo"
    )

    restored = SessionMeta.from_dict(meta.to_dict())

    assert restored.session_id == meta.session_id
    assert restored.course.name == "量子力学"
    assert restored.state is SessionState.TRANSCRIBED
    assert restored.audio.raw == "raw/录音.m4a"
    assert restored.audio.duration_sec == 5531.4
    assert restored.steps["transcribe"].model == "large-v3-turbo"
    assert restored.start_time_confidence == "high"


def test_failed_state_roundtrip():
    meta = SessionMeta(
        session_id="s1",
        state=SessionState.FAILED,
        failed_from=SessionState.TRANSCRIBING,
        error="模型加载失败",
    )
    restored = SessionMeta.from_dict(meta.to_dict())
    assert restored.state is SessionState.FAILED
    assert restored.failed_from is SessionState.TRANSCRIBING
    assert restored.error == "模型加载失败"


def test_step_creates_on_demand():
    meta = SessionMeta(session_id="s1")
    assert meta.step("transcribe").status == "pending"
    meta.step("transcribe").status = "done"
    assert meta.steps["transcribe"].status == "done"
