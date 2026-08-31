"""课程配置与课表匹配。

录音进来时按起始时间自动判断是哪门课。匹配不上不是错误 —— 落到 unknown，
转录照跑，事后可以再归属。绝不能因为「调课了」就不给转录。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from pathlib import Path

import yaml

from lecture_ai.errors import ConfigError
from lecture_ai.session.models import CourseRef

UNKNOWN_KEY = "unknown"


@dataclass(frozen=True)
class ScheduleSlot:
    weekday: int      # 1=周一 ... 7=周日
    start: time
    end: time

    def contains(self, dt: datetime, tolerance_min: int = 0) -> bool:
        """dt 是否落在这个时间段内（含容差）。"""
        if dt.isoweekday() != self.weekday:
            return False
        minutes = dt.hour * 60 + dt.minute
        start_m = self.start.hour * 60 + self.start.minute - tolerance_min
        end_m = self.end.hour * 60 + self.end.minute + tolerance_min
        return start_m <= minutes <= end_m

    def distance_min(self, dt: datetime) -> int:
        """dt 距离该时段的分钟数，段内为 0。用于多个课程都命中时挑最近的。"""
        minutes = dt.hour * 60 + dt.minute
        start_m = self.start.hour * 60 + self.start.minute
        end_m = self.end.hour * 60 + self.end.minute
        if minutes < start_m:
            return start_m - minutes
        if minutes > end_m:
            return minutes - end_m
        return 0


@dataclass
class Course:
    key: str
    name: str
    teacher: str | None = None
    semester: str | None = None
    glossary: str | None = None
    obsidian_folder: str | None = None
    schedule: list[ScheduleSlot] = field(default_factory=list)

    def to_ref(self) -> CourseRef:
        return CourseRef(key=self.key, name=self.name, teacher=self.teacher,
                         semester=self.semester)


class CourseRegistry:
    """courses.yaml 的内存视图。"""

    def __init__(self, courses: dict[str, Course], default_key: str = UNKNOWN_KEY) -> None:
        self._courses = courses
        self._default_key = default_key
        if default_key not in self._courses:
            # 兜底课程必须存在，否则匹配失败无处可归
            self._courses[default_key] = Course(key=default_key, name="未归类")

    def __len__(self) -> int:
        return len(self._courses)

    def __contains__(self, key: str) -> bool:
        return key in self._courses

    def all(self) -> list[Course]:
        return list(self._courses.values())

    def get(self, key: str) -> Course:
        return self._courses.get(key) or self._courses[self._default_key]

    @property
    def default(self) -> Course:
        return self._courses[self._default_key]

    def match(self, dt: datetime, tolerance_min: int = 30) -> Course:
        """按时间匹配课程。多个命中时取时间距离最近的；无命中返回默认课程。"""
        best: tuple[int, Course] | None = None
        for course in self._courses.values():
            if course.key == self._default_key:
                continue
            for slot in course.schedule:
                if slot.contains(dt, tolerance_min):
                    d = slot.distance_min(dt)
                    if best is None or d < best[0]:
                        best = (d, course)
        return best[1] if best else self.default


def _parse_time(value: object, field_name: str, course_key: str) -> time:
    """接受 "14:00" / "14:00:00"，也接受 yaml 自动解析出的 time 对象。"""
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        parts = value.strip().split(":")
        try:
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
            return time(hour=hour, minute=minute)
        except (ValueError, IndexError) as exc:
            raise ConfigError(
                f"courses.yaml 中课程 `{course_key}` 的 {field_name} 时间格式非法：{value!r}"
            ) from exc
    raise ConfigError(f"courses.yaml 中课程 `{course_key}` 缺少合法的 {field_name}")


def load_courses(path: Path, default_key: str = UNKNOWN_KEY) -> CourseRegistry:
    """加载 courses.yaml。文件不存在时返回只含 unknown 的注册表（不报错）。"""
    if not path.exists():
        return CourseRegistry({}, default_key)

    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"courses.yaml 解析失败：{exc}") from exc

    entries = raw.get("courses") or {}
    if not isinstance(entries, dict):
        raise ConfigError("courses.yaml 的 `courses` 必须是映射（课程 key -> 课程定义）")

    courses: dict[str, Course] = {}
    for key, data in entries.items():
        data = data or {}
        slots: list[ScheduleSlot] = []
        for item in data.get("schedule") or []:
            weekday = int(item.get("weekday", 0))
            if not 1 <= weekday <= 7:
                raise ConfigError(
                    f"courses.yaml 中课程 `{key}` 的 weekday 必须是 1-7，实际为 {weekday}"
                )
            slots.append(
                ScheduleSlot(
                    weekday=weekday,
                    start=_parse_time(item.get("start"), "start", str(key)),
                    end=_parse_time(item.get("end"), "end", str(key)),
                )
            )
        courses[str(key)] = Course(
            key=str(key),
            name=str(data.get("name") or key),
            teacher=(data.get("teacher") or None),
            semester=(data.get("semester") or None),
            glossary=(data.get("glossary") or None),
            obsidian_folder=(data.get("obsidian_folder") or None),
            schedule=slots,
        )

    return CourseRegistry(courses, default_key)
