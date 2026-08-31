"""SessionManager 测试：ID 序号、原子写、状态迁移、失败恢复、索引重建。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from lecture_ai.errors import InvalidTransition, SessionNotFound
from lecture_ai.session import SessionManager, SessionState, load_courses

START = datetime(2026, 9, 3, 14, 0)


@pytest.fixture
def manager(config, db):
    return SessionManager(config, db)


@pytest.fixture
def course(config):
    return load_courses(config.courses_path).get("quantum_mechanics")


def test_create_session_id_format(manager, course):
    meta = manager.create(course, START)
    assert meta.session_id == "2026-09-03_quantum-mechanics_001"
    assert meta.date == "2026-09-03"
    assert meta.state is SessionState.NEW
    assert meta.course.name == "量子力学"


def test_sequence_increments_same_day(manager, course):
    ids = [manager.create(course, START).session_id for _ in range(3)]
    assert ids == [
        "2026-09-03_quantum-mechanics_001",
        "2026-09-03_quantum-mechanics_002",
        "2026-09-03_quantum-mechanics_003",
    ]


def test_sequence_resets_next_day(manager, course):
    manager.create(course, START)
    other = manager.create(course, datetime(2026, 9, 5, 10, 0))
    assert other.session_id.endswith("_001")


def test_session_directories_created(manager, course):
    meta = manager.create(course, START)
    sdir = manager.session_dir(meta.session_id)
    for sub in ("raw", "audio", "transcript", "images", "analysis", "note", "logs"):
        assert (sdir / sub).is_dir(), sub


def test_metadata_written_as_utf8_json(manager, course):
    meta = manager.create(course, START)
    path = manager.metadata_path(meta.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["course"]["name"] == "量子力学"
    assert data["state"] == "NEW"
    assert list(path.parent.glob("*.tmp")) == []  # 原子写不留临时文件


def test_load_roundtrip(manager, course):
    created = manager.create(course, START)
    loaded = manager.load(created.session_id)
    assert loaded.session_id == created.session_id
    assert loaded.course.key == "quantum_mechanics"


def test_load_missing_raises(manager):
    with pytest.raises(SessionNotFound):
        manager.load("2026-01-01_nope_001")


def test_transition_updates_db(manager, db, course):
    meta = manager.create(course, START)
    manager.transition(meta, SessionState.AUDIO_READY)
    assert db.get_session(meta.session_id)["state"] == "AUDIO_READY"
    assert manager.load(meta.session_id).state is SessionState.AUDIO_READY


def test_illegal_transition_rejected(manager, course):
    meta = manager.create(course, START)
    with pytest.raises(InvalidTransition):
        manager.transition(meta, SessionState.DONE)


def test_fail_records_previous_state(manager, course):
    meta = manager.create(course, START)
    manager.transition(meta, SessionState.AUDIO_READY)
    manager.transition(meta, SessionState.TRANSCRIBING)
    manager.fail(meta, "模型加载失败")

    reloaded = manager.load(meta.session_id)
    assert reloaded.state is SessionState.FAILED
    assert reloaded.failed_from is SessionState.TRANSCRIBING
    assert reloaded.error == "模型加载失败"


def test_clear_failure_restores_state(manager, course):
    """retry 必须回到失败点，而不是从头再来。"""
    meta = manager.create(course, START)
    manager.transition(meta, SessionState.AUDIO_READY)
    manager.transition(meta, SessionState.TRANSCRIBING)
    manager.fail(meta, "网络错误")

    manager.clear_failure(meta)
    assert meta.state is SessionState.TRANSCRIBING
    assert meta.error is None
    assert meta.failed_from is None


def test_mark_step_writes_both_stores(manager, db, course):
    meta = manager.create(course, START)
    manager.mark_step(meta, "transcribe", "done", elapsed_sec=12.34,
                      provider="fake", model="fake-v1")

    assert manager.load(meta.session_id).steps["transcribe"].status == "done"
    row = db.list_processing(meta.session_id)[0]
    assert row["step"] == "transcribe"
    assert row["status"] == "done"
    assert row["elapsed_sec"] == 12.34


def test_list_ids_scans_disk(manager, course):
    manager.create(course, START)
    manager.create(course, datetime(2026, 9, 5, 10, 0))
    assert len(manager.list_ids()) == 2


def test_rebuild_index_from_disk(manager, config, course):
    """DB 被删掉后必须能从 metadata.json 完整恢复。"""
    from lecture_ai.database import Database

    meta = manager.create(course, START)
    manager.transition(meta, SessionState.AUDIO_READY)

    config.paths.database.unlink()
    fresh_db = Database(config.paths.database)
    assert fresh_db.get_session(meta.session_id) is None

    restored_manager = SessionManager(config, fresh_db)
    assert restored_manager.rebuild_index() == 1
    row = fresh_db.get_session(meta.session_id)
    assert row["state"] == "AUDIO_READY"
    assert row["course_key"] == "quantum_mechanics"


def test_id_avoids_existing_directory(manager, course, config):
    """DB 计数与磁盘不一致时（比如手工删过库），也不能撞目录名。"""
    (config.paths.session_dir / "2026-09-03_quantum-mechanics_001").mkdir(parents=True)
    meta = manager.create(course, START)
    assert meta.session_id == "2026-09-03_quantum-mechanics_002"


def test_rebuild_index_restores_file_dedup(manager, config, course, db):
    """重建索引必须连 sha256 去重记录一起恢复，否则同一份录音会被重复处理。"""
    from lecture_ai.database import Database

    meta = manager.create(course, START)
    raw = manager.session_dir(meta.session_id) / "raw" / "rec.wav"
    raw.write_bytes(b"audio-bytes")
    meta.audio.raw = "raw/rec.wav"
    meta.audio.sha256 = "deadbeef"
    meta.audio.orig_name = "rec.wav"
    manager.save(meta)

    config.paths.database.unlink()
    fresh = Database(config.paths.database)
    SessionManager(config, fresh).rebuild_index()

    assert fresh.file_exists("deadbeef") is not None
    assert fresh.list_courses()[0]["name"] == "量子力学"
