"""SQLite 访问层。所有 SQL 集中在这里，上层不写裸 SQL。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from lecture_ai.utils.timefmt import now_local, to_iso

SCHEMA_VERSION = 1
_SCHEMA_FILE = Path(__file__).with_name("schema.sql")


class Database:
    """薄封装：连接管理 + 仓储方法。

    WAL 模式让 watch 进程与 CLI 的 status 查询可以并发读，不互相阻塞。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ---------------------------------------------------------------- 连接

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self) -> None:
        """建表。幂等 —— 全部 IF NOT EXISTS。"""
        ddl = _SCHEMA_FILE.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(ddl)
            row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    # ---------------------------------------------------------------- courses

    def upsert_course(
        self,
        key: str,
        name: str,
        teacher: str | None = None,
        semester: str | None = None,
        glossary: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO courses (key, name, teacher, semester, glossary, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    name = excluded.name,
                    -- 传 None 时保留原值，避免 rebuild_index 之类的部分更新抹掉字段
                    teacher = COALESCE(excluded.teacher, courses.teacher),
                    semester = COALESCE(excluded.semester, courses.semester),
                    glossary = COALESCE(excluded.glossary, courses.glossary)
                """,
                (key, name, teacher, semester, glossary, to_iso(now_local())),
            )

    def list_courses(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM courses ORDER BY key").fetchall()

    # ---------------------------------------------------------------- sessions

    def upsert_session(
        self,
        session_id: str,
        course_key: str,
        date: str,
        state: str,
        dir_path: str,
        start_time: str | None = None,
        end_time: str | None = None,
        failed_from: str | None = None,
        error: str | None = None,
    ) -> None:
        now = to_iso(now_local())
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, course_key, date, start_time, end_time, state,
                     failed_from, error, dir, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    course_key = excluded.course_key,
                    date = excluded.date,
                    start_time = excluded.start_time,
                    end_time = excluded.end_time,
                    state = excluded.state,
                    failed_from = excluded.failed_from,
                    error = excluded.error,
                    dir = excluded.dir,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id, course_key, date, start_time, end_time, state,
                    failed_from, error, dir_path, now, now,
                ),
            )

    def delete_session(self, session_id: str) -> None:
        """删除 session 索引行。session 改名后用来清掉旧行。"""
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def get_session(self, session_id: str) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()

    def list_sessions(self, state: str | None = None, limit: int | None = None) -> list[sqlite3.Row]:
        sql = "SELECT * FROM sessions"
        params: list[Any] = []
        if state:
            sql += " WHERE state = ?"
            params.append(state)
        sql += " ORDER BY date DESC, id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return conn.execute(sql, params).fetchall()

    def count_sessions_by_state(self) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) AS n FROM sessions GROUP BY state"
            ).fetchall()
        return {r["state"]: r["n"] for r in rows}

    def next_session_seq(self, date: str, course_key: str) -> int:
        """同一天同一门课的第几次课（用于 session_id 末尾的 001/002）。"""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sessions WHERE date = ? AND course_key = ?",
                (date, course_key),
            ).fetchone()
        return int(row["n"]) + 1

    # ---------------------------------------------------------------- files

    def file_exists(self, sha256: str) -> sqlite3.Row | None:
        """去重的核心查询：这个内容处理过没有。"""
        with self.connect() as conn:
            return conn.execute("SELECT * FROM files WHERE sha256 = ?", (sha256,)).fetchone()

    def insert_file(
        self,
        sha256: str,
        path: str,
        file_type: str,
        size: int,
        orig_name: str | None = None,
        timestamp: str | None = None,
        session_id: str | None = None,
    ) -> bool:
        """登记文件。已存在则返回 False（不报错，交由调用方决定如何处理）。"""
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO files
                    (sha256, path, orig_name, type, size, timestamp, session_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (sha256, path, orig_name, file_type, size, timestamp, session_id,
                 to_iso(now_local())),
            )
            return cur.rowcount > 0

    def list_files(self, session_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM files WHERE session_id = ? ORDER BY timestamp", (session_id,)
            ).fetchall()

    # ---------------------------------------------------------------- processing

    def upsert_processing(
        self,
        session_id: str,
        step: str,
        status: str,
        provider: str | None = None,
        model: str | None = None,
        started_at: str | None = None,
        finished_at: str | None = None,
        elapsed_sec: float | None = None,
        error: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO processing
                    (session_id, step, status, provider, model,
                     started_at, finished_at, elapsed_sec, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, step) DO UPDATE SET
                    status = excluded.status,
                    provider = COALESCE(excluded.provider, processing.provider),
                    model = COALESCE(excluded.model, processing.model),
                    started_at = COALESCE(excluded.started_at, processing.started_at),
                    finished_at = excluded.finished_at,
                    elapsed_sec = excluded.elapsed_sec,
                    error = excluded.error
                """,
                (session_id, step, status, provider, model, started_at, finished_at,
                 elapsed_sec, error),
            )

    def list_processing(self, session_id: str) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM processing WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
