"""Session：一堂课的生命周期管理（元数据 + 状态机 + 课程归属）。"""

from lecture_ai.session.courses import Course, CourseRegistry, load_courses
from lecture_ai.session.manager import SessionManager
from lecture_ai.session.models import (
    ALLOWED_TRANSITIONS,
    PHASE1_DONE_STATES,
    AudioInfo,
    CourseRef,
    SessionMeta,
    SessionState,
    StepStatus,
    check_transition,
)

__all__ = [
    "Course",
    "CourseRegistry",
    "load_courses",
    "SessionManager",
    "SessionMeta",
    "SessionState",
    "StepStatus",
    "CourseRef",
    "AudioInfo",
    "ALLOWED_TRANSITIONS",
    "PHASE1_DONE_STATES",
    "check_transition",
]
