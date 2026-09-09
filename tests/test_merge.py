"""录音中途断开后的 session 合并。

关键不变量：
  1. **绝不重跑 ASR** —— 合并只重排已有片段的时间轴；
  2. 时间戳继续对齐墙钟，断口用静音补齐，Phase 3 对时才不会整体偏移；
  3. 被并掉的 session 从此被 autopilot / 面板跳过，不产生重复投喂包。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from lecture_ai.errors import LectureAIError
from lecture_ai.merge import MAX_GAP_SEC, SessionMerger
from lecture_ai.session import SessionManager, SessionState, load_courses
from lecture_ai.transcription.writer import TRANSCRIPT_JSON


def _make(config, db, *, start: datetime, duration: float, texts: list[str],
          course_key: str = "quantum_mechanics"):
    """造一个已转录完成的 session。"""
    manager = SessionManager(config, db)
    course = load_courses(config.courses_path).get(course_key)
    meta = manager.create(course, start)
    meta.audio.duration_sec = duration
    meta.state = SessionState.TRANSCRIBED
    meta.end_time = (start + timedelta(seconds=duration)).isoformat()
    manager.save(meta)

    step = duration / max(1, len(texts))
    payload = {
        "schema_version": 1,
        "session_id": meta.session_id,
        "course": course.name,
        "date": meta.date,
        "audio_start": meta.start_time,
        "provider": "fake",
        "model": "fake",
        "language": "zh",
        "duration_sec": duration,
        "segment_count": len(texts),
        "extra": {},
        "segments": [
            {"id": i, "start": round(i * step, 3), "end": round((i + 1) * step - 0.1, 3),
             "text": t}
            for i, t in enumerate(texts)
        ],
    }
    path = manager.session_dir(meta.session_id) / "transcript" / TRANSCRIPT_JSON
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return manager, meta


BASE = datetime(2026, 9, 9, 9, 44)


@pytest.fixture
def two_parts(config, db):
    """一节课断成两段：09:44 起 48 分钟，10:34 起 64 分钟，中间断 2 分钟。"""
    manager, first = _make(config, db, start=BASE, duration=2880.0,
                           texts=["前半段一", "前半段二"])
    _, second = _make(config, db, start=BASE + timedelta(minutes=50), duration=3840.0,
                      texts=["后半段一", "后半段二"])
    return manager, first, second


# --------------------------------------------------------------- 计划与校验


def test_plan_orders_by_start_time_regardless_of_argument_order(config, two_parts):
    _manager, first, second = two_parts
    parts = SessionMerger(config).plan([second.session_id, first.session_id])
    assert [p.meta.session_id for p in parts] == [first.session_id, second.session_id]
    assert parts[0].offset_sec == 0.0
    assert parts[1].offset_sec == 50 * 60          # 09:44 -> 10:34


def test_plan_records_the_gap_between_parts(config, two_parts):
    """第一段 09:44+48min 到 10:32，第二段 10:34 开始 —— 中间断了 2 分钟。"""
    _manager, first, second = two_parts
    parts = SessionMerger(config).plan([first.session_id, second.session_id])
    assert parts[0].gap_before_sec == 0.0
    assert parts[1].gap_before_sec == pytest.approx(120.0)


def test_tiny_gaps_are_not_treated_as_interruptions(config, db):
    """录音机停止再开始本来就有几秒缝隙，不值得在转录里标出来。"""
    _m, first = _make(config, db, start=BASE, duration=600.0, texts=["a"])
    _m2, second = _make(config, db, start=BASE + timedelta(seconds=605), duration=600.0,
                        texts=["b"])
    parts = SessionMerger(config).plan([first.session_id, second.session_id])
    assert parts[1].gap_before_sec == 0.0


def test_refuses_a_single_session(config, two_parts):
    _manager, first, _second = two_parts
    with pytest.raises(LectureAIError, match="至少要给两个"):
        SessionMerger(config).plan([first.session_id])


def test_refuses_duplicate_ids(config, two_parts):
    _manager, first, _second = two_parts
    with pytest.raises(LectureAIError, match="不能合并两次"):
        SessionMerger(config).plan([first.session_id, first.session_id])


def test_refuses_sessions_that_are_not_transcribed_yet(config, db, two_parts):
    """合并不重跑 ASR，所以必须先有转录。"""
    manager, first, second = two_parts
    second.state = SessionState.AUDIO_READY
    manager.save(second)
    with pytest.raises(LectureAIError, match="还没转录完"):
        SessionMerger(config).plan([first.session_id, second.session_id])


def test_refuses_different_courses_unless_forced(config, db):
    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["x"],
                  course_key="quantum_mechanics")
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=15), duration=600.0,
                   texts=["y"], course_key="big_data")

    with pytest.raises(LectureAIError, match="不同课程"):
        SessionMerger(config).plan([a.session_id, b.session_id])

    parts = SessionMerger(config).plan([a.session_id, b.session_id], allow_any_course=True)
    assert len(parts) == 2


def test_refuses_overlapping_recordings(config, db):
    """时间上重叠 = 同一份内容被导入了两次，合并会产生重复。"""
    _m, a = _make(config, db, start=BASE, duration=3600.0, texts=["x"])
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=30), duration=600.0,
                   texts=["y"])
    with pytest.raises(LectureAIError, match="重叠"):
        SessionMerger(config).plan([a.session_id, b.session_id])


def test_refuses_an_absurdly_large_gap(config, db):
    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["x"])
    _m2, b = _make(config, db, start=BASE + timedelta(seconds=600 + MAX_GAP_SEC + 60),
                   duration=600.0, texts=["y"])
    with pytest.raises(LectureAIError, match="多半不是同一节课"):
        SessionMerger(config).plan([a.session_id, b.session_id])


# --------------------------------------------------------------- 执行


def test_merge_shifts_the_second_part_onto_one_timeline(config, two_parts):
    manager, first, second = two_parts
    outcome = SessionMerger(config).merge(
        [first.session_id, second.session_id], keep_audio=False
    )

    assert outcome.primary_id == first.session_id
    assert outcome.merged_ids == [second.session_id]
    assert outcome.segment_count == 4

    payload = json.loads(
        (manager.session_dir(first.session_id) / "transcript" / TRANSCRIPT_JSON)
        .read_text(encoding="utf-8")
    )
    segs = payload["segments"]
    assert [s["text"] for s in segs] == ["前半段一", "前半段二", "后半段一", "后半段二"]
    # 后半段整体右移 50 分钟
    assert segs[2]["start"] == pytest.approx(3000.0)
    # 时间轴单调递增
    assert all(segs[i]["start"] <= segs[i + 1]["start"] for i in range(len(segs) - 1))


def test_merge_records_provenance_and_gaps(config, two_parts):
    manager, first, second = two_parts
    SessionMerger(config).merge([first.session_id, second.session_id], keep_audio=False)

    payload = json.loads(
        (manager.session_dir(first.session_id) / "transcript" / TRANSCRIPT_JSON)
        .read_text(encoding="utf-8")
    )
    extra = payload["extra"]
    assert extra["merged_from"] == [first.session_id, second.session_id]
    assert extra["merged_gaps_sec"] == [[2880.0, 120.0]]


def test_merged_away_session_is_marked_and_skipped(config, two_parts):
    manager, first, second = two_parts
    SessionMerger(config).merge([first.session_id, second.session_id], keep_audio=False)

    assert manager.load(second.session_id).merged_into == first.session_id
    assert manager.load(first.session_id).merged_from == [second.session_id]


def test_autopilot_skips_merged_away_sessions(config, db, two_parts):
    """否则被并掉的那半节课还会自己生成一份投喂包。"""
    from lecture_ai.pipeline.autopilot import AutopilotService

    manager, first, second = two_parts
    SessionMerger(config).merge([first.session_id, second.session_id], keep_audio=False)

    touched = []
    service = AutopilotService(config)
    service._repair = lambda sid: touched.append(sid)
    service._export = lambda sid, _dir: touched.append(sid)
    service.run_once()

    assert second.session_id not in touched


def test_merge_updates_duration_and_end_time(config, two_parts):
    manager, first, second = two_parts
    outcome = SessionMerger(config).merge(
        [first.session_id, second.session_id], keep_audio=False
    )
    # 50 分钟偏移 + 后半段 64 分钟 = 114 分钟
    assert outcome.duration_sec == pytest.approx(6840.0)
    assert manager.load(first.session_id).audio.duration_sec == pytest.approx(6840.0)


def test_merge_drops_stale_repaired_output(config, two_parts):
    """旧 REPAIRED 是按合并前的时间轴算的，留着必然错位。"""
    manager, first, second = two_parts
    stale = manager.session_dir(first.session_id) / "transcript" / "transcript_repaired.md"
    stale.write_text("旧的修复稿", encoding="utf-8")

    SessionMerger(config).merge([first.session_id, second.session_id], keep_audio=False)
    assert not stale.exists()


def test_cannot_merge_an_already_merged_session(config, two_parts):
    manager, first, second = two_parts
    SessionMerger(config).merge([first.session_id, second.session_id], keep_audio=False)

    _m, third = _make(config, manager.db, start=BASE + timedelta(minutes=130),
                      duration=600.0, texts=["z"])
    with pytest.raises(LectureAIError, match="已经并进"):
        SessionMerger(config).merge([second.session_id, third.session_id], keep_audio=False)


def test_three_parts_merge_in_order(config, db):
    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["一"])
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=12), duration=600.0,
                   texts=["二"])
    _m3, c = _make(config, db, start=BASE + timedelta(minutes=24), duration=600.0,
                   texts=["三"])

    outcome = SessionMerger(config).merge(
        [c.session_id, a.session_id, b.session_id], keep_audio=False
    )
    assert outcome.primary_id == a.session_id
    assert outcome.segment_count == 3
    assert len(outcome.gaps) == 2
