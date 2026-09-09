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


#: 2026-09-09 是周三。测试夹具里 quantum_mechanics 排在周三 14:00–15:40，
#: 容差 30 分钟 —— 所以 BASE 必须落在这个时段里，否则匹配不到课表，
#: 自动合并会走"无课表"那条更严的分支，测不到真正想测的逻辑。
BASE = datetime(2026, 9, 9, 14, 0)
#: 课表时段的结束时间，宽限期测试要用
SLOT_END = datetime(2026, 9, 9, 15, 40)
#: 落在任何课表之外的时刻（凌晨三点）
UNSCHEDULED = datetime(2026, 9, 9, 3, 0)


@pytest.fixture
def two_parts(config, db):
    """一节课断成两段：14:00 起 30 分钟，14:32 起 40 分钟，中间断 2 分钟。

    两段都落在 14:00–15:40 这一节里，走的是"同一课表时段"的判定路径。
    """
    manager, first = _make(config, db, start=BASE, duration=1800.0,
                           texts=["前半段一", "前半段二"])
    _, second = _make(config, db, start=BASE + timedelta(minutes=32), duration=2400.0,
                      texts=["后半段一", "后半段二"])
    return manager, first, second


# --------------------------------------------------------------- 计划与校验


def test_plan_orders_by_start_time_regardless_of_argument_order(config, two_parts):
    _manager, first, second = two_parts
    parts = SessionMerger(config).plan([second.session_id, first.session_id])
    assert [p.meta.session_id for p in parts] == [first.session_id, second.session_id]
    assert parts[0].offset_sec == 0.0
    assert parts[1].offset_sec == 32 * 60          # 14:00 -> 14:32


def test_plan_records_the_gap_between_parts(config, two_parts):
    """第一段 14:00+30min 到 14:30，第二段 14:32 开始 —— 中间断了 2 分钟。"""
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
    # 后半段整体右移 32 分钟
    assert segs[2]["start"] == pytest.approx(1920.0)
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
    assert extra["merged_gaps_sec"] == [[1800.0, 120.0]]


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
    # 32 分钟偏移 + 后半段 40 分钟 = 72 分钟
    assert outcome.duration_sec == pytest.approx(4320.0)
    assert manager.load(first.session_id).audio.duration_sec == pytest.approx(4320.0)


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


# --------------------------------------------------------------- 自动合并

# 自动合并没人盯着，所以宁可漏合（人工补一句 `lecture-ai merge`），
# 也绝不能错合把两节不相干的课搅在一起。下面每条都在钉这个方向的边界。


def test_auto_merge_groups_two_parts_of_one_class(config, two_parts):
    """两段都落在周三 14:00–15:40 这一节里 —— 走课表判定，该合。"""
    from lecture_ai.merge import AutoMerger

    _manager, first, second = two_parts
    groups = AutoMerger(config).find_groups()
    assert len(groups) == 1
    assert groups[0].session_ids == [first.session_id, second.session_id]
    assert "14:00" in groups[0].reason


def test_auto_merge_runs_end_to_end(config, two_parts):
    from lecture_ai.merge import AutoMerger

    manager, first, second = two_parts
    outcomes = AutoMerger(config).run_once()
    assert len(outcomes) == 1
    assert outcomes[0].primary_id == first.session_id
    assert manager.load(second.session_id).merged_into == first.session_id


def test_auto_merge_can_be_switched_off(config, two_parts):
    from lecture_ai.merge import AutoMerger

    config.processing.auto_merge = False
    assert AutoMerger(config).run_once() == []


def test_auto_merge_refuses_a_gap_beyond_the_limit(config, db):
    """同一课表时段内也有上限：默认 15 分钟，超过就交给人工。"""
    from lecture_ai.merge import AutoMerger

    config.processing.auto_merge_max_gap_minutes = 15
    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["x"])
    # 第一段 14:00–14:10，第二段 14:30 开始 —— 间隔 20 分钟，仍在同一节课内
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=30), duration=600.0,
                   texts=["y"])
    assert AutoMerger(config).find_groups() == []

    config.processing.auto_merge_max_gap_minutes = 25
    assert len(AutoMerger(config).find_groups()) == 1


def test_auto_merge_never_crosses_courses(config, db):
    """同一时刻的两段，如果归属不同课程，绝不合并。"""
    from lecture_ai.merge import AutoMerger

    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["x"],
                  course_key="quantum_mechanics")
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=12), duration=600.0,
                   texts=["y"], course_key="unknown")
    assert AutoMerger(config).find_groups() == []


def test_auto_merge_never_crosses_days(config, db):
    from lecture_ai.merge import AutoMerger

    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["x"])
    _m2, b = _make(config, db, start=BASE + timedelta(days=7), duration=600.0, texts=["y"])
    assert AutoMerger(config).find_groups() == []


def test_unscheduled_sessions_need_a_much_tighter_gap(config, db):
    """匹配不到课表时没有依据证明两段属于同一节课，只在间隔极小时才敢合。"""
    from lecture_ai.merge import AutoMerger

    _m, a = _make(config, db, start=UNSCHEDULED, duration=600.0, texts=["x"],
                  course_key="unknown")
    _m2, b = _make(config, db, start=UNSCHEDULED + timedelta(minutes=19), duration=600.0,
                   texts=["y"], course_key="unknown")
    assert AutoMerger(config).find_groups() == [], "9 分钟间隔对无课表的应当太长"

    _m3, c = _make(config, db, start=UNSCHEDULED + timedelta(hours=5), duration=600.0,
                   texts=["p"], course_key="unknown")
    _m4, d = _make(config, db, start=UNSCHEDULED + timedelta(hours=5, minutes=11),
                   duration=600.0, texts=["q"], course_key="unknown")
    ids = [g.session_ids for g in AutoMerger(config).find_groups()]
    assert [c.session_id, d.session_id] in ids, "1 分钟间隔应当合并"


def test_auto_merge_skips_already_merged(config, two_parts):
    """跑第二遍不该把已经合过的再合一次。"""
    from lecture_ai.merge import AutoMerger

    auto = AutoMerger(config)
    assert len(auto.run_once()) == 1
    assert auto.run_once() == []


def test_auto_merge_ignores_sessions_still_transcribing(config, db, two_parts):
    """另一半还在转录时不能先合 —— 合并需要两边都有转录。"""
    from lecture_ai.merge import AutoMerger

    manager, first, second = two_parts
    second.state = SessionState.TRANSCRIBING
    manager.save(second)
    assert AutoMerger(config).find_groups() == []


def test_auto_merge_survives_a_bad_group_without_stopping(config, two_parts, monkeypatch):
    """单组合并失败不能打断 watch 主循环。"""
    from lecture_ai.merge import AutoMerger

    auto = AutoMerger(config)
    monkeypatch.setattr(auto.merger, "merge",
                        lambda *a, **k: (_ for _ in ()).throw(LectureAIError("boom")))
    assert auto.run_once() == []


def test_three_parts_of_one_class_merge_together(config, db):
    """断两次也要能合。"""
    from lecture_ai.merge import AutoMerger

    _m, a = _make(config, db, start=BASE, duration=600.0, texts=["一"])
    _m2, b = _make(config, db, start=BASE + timedelta(minutes=12), duration=600.0,
                   texts=["二"])
    _m3, c = _make(config, db, start=BASE + timedelta(minutes=24), duration=600.0,
                   texts=["三"])
    groups = AutoMerger(config).find_groups()
    assert len(groups) == 1
    assert groups[0].session_ids == [a.session_id, b.session_id, c.session_id]


# --------------------------------------------------------------- 宽限期

# 后半段录音常常晚几十分钟才传完（100+ MB）。不等的话会先给半节课出一份包，
# 等另一半到了、自动合并完，又得推翻重来 —— 桌面上先冒出半截的包再消失，
# 比晚半小时拿到完整的包更让人困惑。


def test_autopilot_holds_during_the_merge_grace_window(config, db):
    """课表 15:40 下课 + 30 分钟宽限 = 16:10 之前不出包。"""
    from lecture_ai.pipeline.autopilot import AutopilotService

    _m, meta = _make(config, db, start=BASE, duration=600.0, texts=["x"])
    config.processing.merge_grace_minutes = 30

    service = AutopilotService(config)
    assert service._waiting_for_siblings(meta, now=SLOT_END + timedelta(minutes=10)) is True
    assert service._waiting_for_siblings(meta, now=SLOT_END + timedelta(minutes=31)) is False


def test_grace_window_can_be_switched_off(config, db):
    from lecture_ai.pipeline.autopilot import AutopilotService

    _m, meta = _make(config, db, start=BASE, duration=600.0, texts=["x"])
    config.processing.merge_grace_minutes = 0
    service = AutopilotService(config)
    assert service._waiting_for_siblings(meta, now=BASE) is False


def test_grace_window_does_not_apply_without_a_schedule(config, db):
    """匹配不到课表就没有依据说明还有下一段，不该干等。"""
    from lecture_ai.pipeline.autopilot import AutopilotService

    _m, meta = _make(config, db, start=UNSCHEDULED, duration=600.0, texts=["x"],
                     course_key="unknown")
    config.processing.merge_grace_minutes = 30
    service = AutopilotService(config)
    assert service._waiting_for_siblings(meta, now=UNSCHEDULED) is False
