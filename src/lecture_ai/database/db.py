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
        """删除 session 索引行。

        注意 files / processing 对 sessions 是 NO ACTION（schema 里没写
        ON DELETE CASCADE），只要还有子行引用就会被外键挡住。改名请用
        `rename_session`，彻底删除请先自行清掉子行。
        """
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))

    def rename_session(self, old_id: str, new_id: str) -> None:
        """把索引整体迁到新 session_id，在一个事务里完成。

        files / processing 都带 `REFERENCES sessions(id)` 却没有级联，所以直接
        删旧行会抛 FOREIGN KEY constraint failed —— 任何已经跑过 ingest 的
        session 都会因此 relabel 失败。必须先把子行的引用迁到新 id 再删旧行。

        调用前新 session 行必须已存在（否则 UPDATE 又会撞外键）。
        """
        with self.connect() as conn:
            if conn.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (new_id,)
            ).fetchone() is None:
                raise ValueError(f"重命名目标尚未入库：{new_id}")
            conn.execute(
                "UPDATE files SET session_id = ? WHERE session_id = ?", (new_id, old_id)
            )
            conn.execute(
                "UPDATE processing SET session_id = ? WHERE session_id = ?",
                (new_id, old_id),
            )
            conn.execute("DELETE FROM sessions WHERE id = ?", (old_id,))

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

    def course_sequence(self, course_key: str, start_time: str | None = None) -> int:
        """这门课的第几次课（用于 session_id 末尾的 001/002/003）。

        以前是按「同日同课」计数的，可一门课一天基本只上一次，序号于是永远停在
        001，等于没有信息量 —— 第 2 次课的录音仍然叫 `..._001`。改成按课程累计：
        序号 = 这门课里开始时间早于本节的 session 数 + 1。

        用「早于本节的条数」而不是「总条数 + 1」，是为了让已经入库的 session 也能
        算出正确的位次（relabel 时要用），自己不会把自己算进去。
        start_time 缺失时退化成总数 + 1。
        """
        with self.connect() as conn:
            if start_time is None:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions WHERE course_key = ?",
                    (course_key,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM sessions "
                    "WHERE course_key = ? AND start_time IS NOT NULL AND start_time < ?",
                    (course_key, start_time),
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
