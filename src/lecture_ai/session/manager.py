"""SessionManager：session 的创建、加载、保存、状态迁移。

关键约定：
  - metadata.json 是权威真相源，SQLite 只是索引；
  - 所有写入走原子写；
  - 状态迁移必须经过 transition()，非法迁移直接抛异常（属于程序 bug）。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import SessionNotFound
from lecture_ai.logging_setup import get_logger
from lecture_ai.session.courses import Course
from lecture_ai.session.models import (
    SessionMeta,
    SessionState,
    StepStatus,
    check_transition,
)
from lecture_ai.utils.paths import atomic_write_text, ensure_dir
from lecture_ai.utils.slug import slugify
from lecture_ai.utils.timefmt import now_local, to_iso

METADATA_FILENAME = "metadata.json"

#: session 目录下的固定子目录
SUBDIRS = ("raw", "audio", "transcript", "images", "analysis", "note", "logs")


class SessionManager:
    def __init__(self, config: Config, db: Database) -> None:
        self.config = config
        self.db = db
        self.root = config.paths.session_dir
        self.log = get_logger(__name__)

    # ---------------------------------------------------------------- 路径

    def session_dir(self, session_id: str) -> Path:
        return self.root / session_id

    def metadata_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / METADATA_FILENAME

    # ---------------------------------------------------------------- 创建

    def make_session_id(self, course: Course, start: datetime) -> str:
        """生成 2026-09-03_quantum-mechanics_001。

        序号在「同日同课程」内递增，且要避开磁盘上已存在的目录
        （DB 被删过时仍然不能撞车）。
        """
        date_str = start.strftime("%Y-%m-%d")
        slug = slugify(course.key)
        seq = self.db.next_session_seq(date_str, course.key)
        while True:
            candidate = f"{date_str}_{slug}_{seq:03d}"
            if not self.session_dir(candidate).exists():
                return candidate
            seq += 1

    def create(
        self,
        course: Course,
        start_time: datetime,
        *,
        end_time: datetime | None = None,
        start_time_source: str | None = None,
        start_time_confidence: str = "low",
    ) -> SessionMeta:
        session_id = self.make_session_id(course, start_time)
        now = to_iso(now_local())

        meta = SessionMeta(
            session_id=session_id,
            course=course.to_ref(),
            date=start_time.strftime("%Y-%m-%d"),
            start_time=to_iso(start_time),
            end_time=to_iso(end_time),
            start_time_source=start_time_source,
            start_time_confidence=start_time_confidence,
            state=SessionState.NEW,
            created_at=now,
            updated_at=now,
        )

        sdir = ensure_dir(self.session_dir(session_id))
        for sub in SUBDIRS:
            ensure_dir(sdir / sub)

        self.db.upsert_course(
            course.key, course.name, course.teacher, course.semester, course.glossary
        )
        self.save(meta)
        self.log.info("创建 session %s（课程：%s）", session_id, course.name)
        return meta

    # ---------------------------------------------------------------- 读写

    def save(self, meta: SessionMeta) -> None:
        """写 metadata.json（原子）并同步 SQLite 索引。"""
        meta.updated_at = to_iso(now_local())
        text = json.dumps(meta.to_dict(), ensure_ascii=False, indent=2)
        atomic_write_text(self.metadata_path(meta.session_id), text)
        self.db.upsert_session(
            session_id=meta.session_id,
            course_key=meta.course.key,
            date=meta.date,
            state=str(meta.state),
            dir_path=str(self.session_dir(meta.session_id)),
            start_time=meta.start_time,
            end_time=meta.end_time,
            failed_from=str(meta.failed_from) if meta.failed_from else None,
            error=meta.error,
        )

    def load(self, session_id: str) -> SessionMeta:
        path = self.metadata_path(session_id)
        if not path.exists():
            raise SessionNotFound(f"session 不存在：{session_id}（找不到 {path}）")
        data = json.loads(path.read_text(encoding="utf-8"))
        return SessionMeta.from_dict(data)

    def exists(self, session_id: str) -> bool:
        return self.metadata_path(session_id).exists()

    def list_ids(self) -> list[str]:
        """扫描磁盘列出所有 session（不依赖 DB）。"""
        if not self.root.exists():
            return []
        return sorted(
            d.name for d in self.root.iterdir()
            if d.is_dir() and (d / METADATA_FILENAME).exists()
        )

    # ---------------------------------------------------------------- 状态

    def transition(self, meta: SessionMeta, target: SessionState) -> SessionMeta:
        """合法性校验后迁移状态并落盘。"""
        check_transition(meta.state, target)
        if meta.state != target:
            self.log.debug("session %s 状态 %s -> %s", meta.session_id, meta.state, target)
        meta.state = target
        if target != SessionState.FAILED:
            meta.error = None
            meta.failed_from = None
        self.save(meta)
        return meta

    def fail(self, meta: SessionMeta, error: str) -> SessionMeta:
        """标记失败，记住失败前的状态以便 retry 精确恢复。"""
        if meta.state != SessionState.FAILED:
            meta.failed_from = meta.state
        meta.state = SessionState.FAILED
        meta.error = error
        self.save(meta)
        self.log.error("session %s 失败（%s）：%s", meta.session_id, meta.failed_from, error)
        return meta

    def clear_failure(self, meta: SessionMeta) -> SessionMeta:
        """retry 前调用：从 FAILED 恢复到失败前的状态。"""
        if meta.state != SessionState.FAILED:
            return meta
        restored = meta.failed_from or SessionState.NEW
        meta.state = restored
        meta.failed_from = None
        meta.error = None
        self.save(meta)
        self.log.info("session %s 从 FAILED 恢复到 %s", meta.session_id, restored)
        return meta

    def mark_step(
        self,
        meta: SessionMeta,
        step: str,
        status: str,
        *,
        elapsed_sec: float | None = None,
        provider: str | None = None,
        model: str | None = None,
        error: str | None = None,
    ) -> None:
        """更新步骤状态，同时写 metadata 与 processing 表。"""
        now = to_iso(now_local())
        st: StepStatus = meta.step(step)
        st.status = status
        st.at = now
        if elapsed_sec is not None:
            st.elapsed_sec = round(elapsed_sec, 2)
        if provider:
            st.provider = provider
        if model:
            st.model = model
        st.error = error
        self.save(meta)

        self.db.upsert_processing(
            session_id=meta.session_id,
            step=step,
            status=status,
            provider=provider,
            model=model,
            started_at=now if status == "running" else None,
            finished_at=now if status in ("done", "failed", "skipped") else None,
            elapsed_sec=elapsed_sec,
            error=error,
        )

    # ---------------------------------------------------------------- 恢复

    def rebuild_index(self) -> int:
        """从磁盘上的 metadata.json 重建 SQLite 索引（DB 损坏/丢失时的救命通道）。

        重建三样东西，缺一不可：
          - courses：sessions 有外键指向它，缺了会插不进去；
          - sessions：状态与课程归属；
          - files：sha256 去重记录，否则同一份录音会被重新处理一遍。
        """
        count = 0
        for session_id in self.list_ids():
            try:
                meta = self.load(session_id)
            except (OSError, ValueError, KeyError) as exc:
                self.log.warning("重建索引时跳过 %s：%s", session_id, exc)
                continue

            self.db.upsert_course(
                meta.course.key, meta.course.name, meta.course.teacher, meta.course.semester
            )
            self.db.upsert_session(
                session_id=meta.session_id,
                course_key=meta.course.key,
                date=meta.date,
                state=str(meta.state),
                dir_path=str(self.session_dir(meta.session_id)),
                start_time=meta.start_time,
                end_time=meta.end_time,
                failed_from=str(meta.failed_from) if meta.failed_from else None,
                error=meta.error,
            )

            if meta.audio.sha256 and meta.audio.raw:
                raw_path = self.session_dir(session_id) / meta.audio.raw
                self.db.insert_file(
                    sha256=meta.audio.sha256,
                    path=str(raw_path),
                    file_type="audio",
                    size=raw_path.stat().st_size if raw_path.exists() else 0,
                    orig_name=meta.audio.orig_name,
                    timestamp=meta.start_time,
                    session_id=session_id,
                )
            count += 1
        self.log.info("索引重建完成，共 %d 个 session", count)
        return count
