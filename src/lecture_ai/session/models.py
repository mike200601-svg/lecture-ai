"""Session 数据模型与状态机。

状态机对应总 Prompt 第七节。Phase 1 只会走到 TRANSCRIBED，
后续状态先定义好，Phase 3/4 直接接上，不用改这里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from lecture_ai.errors import InvalidTransition


class SessionState(StrEnum):
    NEW = "NEW"
    AUDIO_READY = "AUDIO_READY"
    TRANSCRIBING = "TRANSCRIBING"
    TRANSCRIBED = "TRANSCRIBED"
    IMAGES_READY = "IMAGES_READY"
    FUSING = "FUSING"
    GENERATING_NOTE = "GENERATING_NOTE"
    EXPORTED = "EXPORTED"
    DONE = "DONE"
    FAILED = "FAILED"


#: 合法迁移表。任何不在表中的迁移都会被 SessionManager.transition 拒绝。
#: 每个非终态都能进 FAILED；FAILED 的出路由 retry 逻辑（clear_failure）处理。
ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.NEW: {SessionState.AUDIO_READY, SessionState.FAILED},
    SessionState.AUDIO_READY: {SessionState.TRANSCRIBING, SessionState.FAILED},
    SessionState.TRANSCRIBING: {SessionState.TRANSCRIBED, SessionState.FAILED},
    # TRANSCRIBED 是 Phase 1 的终点；Phase 3 起可继续往下
    SessionState.TRANSCRIBED: {
        SessionState.IMAGES_READY,
        SessionState.GENERATING_NOTE,
        SessionState.FAILED,
    },
    SessionState.IMAGES_READY: {SessionState.FUSING, SessionState.FAILED},
    SessionState.FUSING: {SessionState.GENERATING_NOTE, SessionState.FAILED},
    SessionState.GENERATING_NOTE: {SessionState.EXPORTED, SessionState.FAILED},
    SessionState.EXPORTED: {SessionState.DONE, SessionState.FAILED},
    SessionState.DONE: set(),
    # FAILED 只能通过 retry 回到失败前的状态，由 clear_failure() 走 restore 路径
    SessionState.FAILED: set(),
}

#: Phase 1 认为「已完成」的状态
PHASE1_DONE_STATES = {
    SessionState.TRANSCRIBED,
    SessionState.IMAGES_READY,
    SessionState.FUSING,
    SessionState.GENERATING_NOTE,
    SessionState.EXPORTED,
    SessionState.DONE,
}


def check_transition(current: SessionState, target: SessionState) -> None:
    """校验迁移合法性，非法则抛 InvalidTransition。"""
    if current == target:
        return  # 幂等：重复设置同一状态无害
    if target not in ALLOWED_TRANSITIONS.get(current, set()):
        raise InvalidTransition(str(current), str(target))


# --------------------------------------------------------------------------- 数据


@dataclass
class StepStatus:
    """单个处理步骤的状态。对应 metadata.json 的 steps.<name>。"""

    status: str = "pending"  # pending | running | done | failed | skipped
    at: str | None = None
    elapsed_sec: float | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "StepStatus":
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class CourseRef:
    """session 关联的课程快照。存快照而不是只存 key —— 课表以后改了，历史 session 不受影响。"""

    key: str = "unknown"
    name: str = "未归类"
    teacher: str | None = None
    semester: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "CourseRef":
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AudioInfo:
    raw: str | None = None            # 相对 session 目录的路径
    sha256: str | None = None
    duration_sec: float | None = None
    processed: str | None = None      # audio_16k.wav
    orig_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AudioInfo":
        data = data or {}
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


METADATA_SCHEMA_VERSION = 1

#: 步骤顺序（Phase 1 只跑前三个，后面的先占位）
STEP_NAMES = (
    "ingest", "preprocess", "transcribe", "repair", "clean", "structure", "note", "obsidian"
)


@dataclass
class SessionMeta:
    """一堂课的全部元数据。metadata.json 是它的权威序列化形式。"""

    session_id: str
    course: CourseRef = field(default_factory=CourseRef)
    date: str = ""                    # YYYY-MM-DD
    start_time: str | None = None     # ISO8601
    end_time: str | None = None
    start_time_source: str | None = None       # ffprobe | filename | mtime-duration | ctime
    start_time_confidence: str = "low"         # high | medium | low
    state: SessionState = SessionState.NEW
    failed_from: SessionState | None = None
    error: str | None = None
    audio: AudioInfo = field(default_factory=AudioInfo)
    images: list[dict[str, Any]] = field(default_factory=list)  # Phase 3
    steps: dict[str, StepStatus] = field(default_factory=dict)
    created_at: str | None = None
    updated_at: str | None = None
    schema_version: int = METADATA_SCHEMA_VERSION

    def step(self, name: str) -> StepStatus:
        """取步骤状态，不存在则创建一个 pending。"""
        if name not in self.steps:
            self.steps[name] = StepStatus()
        return self.steps[name]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "course": self.course.to_dict(),
            "date": self.date,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "start_time_source": self.start_time_source,
            "start_time_confidence": self.start_time_confidence,
            "state": str(self.state),
            "failed_from": str(self.failed_from) if self.failed_from else None,
            "error": self.error,
            "audio": self.audio.to_dict(),
            "images": self.images,
            "steps": {k: v.to_dict() for k, v in self.steps.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionMeta":
        failed_from = data.get("failed_from")
        return cls(
            session_id=data["session_id"],
            course=CourseRef.from_dict(data.get("course")),
            date=data.get("date", ""),
            start_time=data.get("start_time"),
            end_time=data.get("end_time"),
            start_time_source=data.get("start_time_source"),
            start_time_confidence=data.get("start_time_confidence", "low"),
            state=SessionState(data.get("state", SessionState.NEW)),
            failed_from=SessionState(failed_from) if failed_from else None,
            error=data.get("error"),
            audio=AudioInfo.from_dict(data.get("audio")),
            images=data.get("images", []) or [],
            steps={k: StepStatus.from_dict(v) for k, v in (data.get("steps") or {}).items()},
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
            schema_version=int(data.get("schema_version", METADATA_SCHEMA_VERSION)),
        )
