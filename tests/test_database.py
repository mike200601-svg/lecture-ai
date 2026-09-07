"""SQLite 层测试。重点：幂等建表、内容去重、processing upsert。"""

from __future__ import annotations

from lecture_ai.database import SCHEMA_VERSION, Database


def test_init_is_idempotent(config):
    Database(config.paths.database)
    db = Database(config.paths.database)  # 第二次不应报错
    with db.connect() as conn:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        assert row["version"] == SCHEMA_VERSION
        # 版本行不能因为重复 init 变成两条
        assert conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()["c"] == 1


def test_wal_enabled(db):
    with db.connect() as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_course_upsert(db):
    db.upsert_course("qm", "量子力学", "张老师", "2026-秋", "qm.txt")
    db.upsert_course("qm", "量子力学(改)", "李老师", "2026-秋", "qm.txt")
    courses = db.list_courses()
    assert len(courses) == 1
    assert courses[0]["name"] == "量子力学(改)"
    assert courses[0]["teacher"] == "李老师"


def test_session_upsert_and_state(db):
    db.upsert_course("qm", "量子力学")
    db.upsert_session("s1", "qm", "2026-09-03", "NEW", "/tmp/s1")
    db.upsert_session("s1", "qm", "2026-09-03", "TRANSCRIBED", "/tmp/s1")

    row = db.get_session("s1")
    assert row["state"] == "TRANSCRIBED"
    assert len(db.list_sessions()) == 1


def test_list_sessions_filter_and_counts(db):
    db.upsert_course("qm", "量子力学")
    db.upsert_session("s1", "qm", "2026-09-01", "TRANSCRIBED", "/d/s1")
    db.upsert_session("s2", "qm", "2026-09-02", "FAILED", "/d/s2")
    db.upsert_session("s3", "qm", "2026-09-03", "TRANSCRIBED", "/d/s3")

    assert len(db.list_sessions(state="TRANSCRIBED")) == 2
    assert db.count_sessions_by_state() == {"TRANSCRIBED": 2, "FAILED": 1}


def test_course_sequence_counts_across_the_whole_term(db):
    """序号是「这门课的第几次课」，不是「今天的第几次」。

    一门课一天只上一次，按天计数会让每节课都叫 001，等于没有信息量。
    """
    db.upsert_course("qm", "量子力学")
    assert db.course_sequence("qm", "2026-09-02T09:45:00+08:00") == 1

    db.upsert_session("s1", "qm", "2026-09-02", "NEW", "/d/s1",
                      start_time="2026-09-02T09:45:00+08:00")
    # 换一天不再重新计数：这是第 2 次课
    assert db.course_sequence("qm", "2026-09-07T09:44:00+08:00") == 2

    db.upsert_session("s2", "qm", "2026-09-07", "NEW", "/d/s2",
                      start_time="2026-09-07T09:44:00+08:00")
    assert db.course_sequence("qm", "2026-09-09T09:45:00+08:00") == 3


def test_course_sequence_is_positional_not_total(db):
    """已入库的 session 自己不能把自己算进去，否则 relabel 会一直往后漂。"""
    db.upsert_course("qm", "量子力学")
    for i, ts in enumerate(("2026-09-02T09:45:00+08:00",
                            "2026-09-07T09:44:00+08:00",
                            "2026-09-09T09:45:00+08:00"), start=1):
        db.upsert_session(f"s{i}", "qm", ts[:10], "NEW", f"/d/s{i}", start_time=ts)

    # 中间那节课重新求位次，仍应是 2 而不是 4
    assert db.course_sequence("qm", "2026-09-07T09:44:00+08:00") == 2
    # 补录一节更早的课，排在最前面
    assert db.course_sequence("qm", "2026-08-31T09:45:00+08:00") == 1


def test_course_sequence_is_per_course(db):
    db.upsert_course("qm", "量子力学")
    db.upsert_course("de", "数字电子技术基础")
    db.upsert_session("s1", "qm", "2026-09-02", "NEW", "/d/s1",
                      start_time="2026-09-02T09:45:00+08:00")
    db.upsert_session("s2", "qm", "2026-09-07", "NEW", "/d/s2",
                      start_time="2026-09-07T09:44:00+08:00")
    # 另一门课独立计数
    assert db.course_sequence("de", "2026-09-08T09:45:00+08:00") == 1


def test_course_sequence_without_start_time(db):
    db.upsert_course("qm", "量子力学")
    db.upsert_session("s1", "qm", "2026-09-02", "NEW", "/d/s1",
                      start_time="2026-09-02T09:45:00+08:00")
    assert db.course_sequence("qm", None) == 2


def test_file_dedup(db):
    assert db.insert_file("hash1", "/d/a.m4a", "audio", 100) is True
    # 同 sha256 第二次插入必须被拒绝 —— 这就是「不重复处理同一文件」的底层保证
    assert db.insert_file("hash1", "/d/b.m4a", "audio", 100) is False
    assert db.file_exists("hash1")["path"] == "/d/a.m4a"
    assert db.file_exists("nope") is None


def test_processing_upsert_is_idempotent(db):
    db.upsert_course("qm", "量子力学")
    db.upsert_session("s1", "qm", "2026-09-03", "NEW", "/d")

    db.upsert_processing("s1", "transcribe", "running", provider="local_whisper",
                         model="large-v3-turbo", started_at="T1")
    db.upsert_processing("s1", "transcribe", "done", finished_at="T2", elapsed_sec=12.5)

    rows = db.list_processing("s1")
    assert len(rows) == 1
    assert rows[0]["status"] == "done"
    assert rows[0]["elapsed_sec"] == 12.5
    # provider/model 在第二次未传入时必须保留，不能被 NULL 覆盖
    assert rows[0]["model"] == "large-v3-turbo"
    assert rows[0]["started_at"] == "T1"


def test_cjk_content_roundtrip(db):
    db.upsert_course("qm", "量子力学", "张老师")
    db.upsert_session("s1", "qm", "2026-09-03", "FAILED", "D:/原创项目/课堂/s1",
                      error="转录失败：模型加载超时")
    row = db.get_session("s1")
    assert row["dir"] == "D:/原创项目/课堂/s1"
    assert row["error"] == "转录失败：模型加载超时"
