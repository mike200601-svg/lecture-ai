"""Phase 1.5：异常检测、选择性 ASR、门控、合并与 RAW 不可变性。"""

from __future__ import annotations

import json
import wave
from datetime import datetime
from pathlib import Path

import pytest

from lecture_ai.cli import _parse_region, build_parser
from lecture_ai.repair import (
    RepairPipeline,
    decide_repair,
    detect_suspicious_regions,
    extract_wav_region,
    measure_text,
    merge_repairs,
)
from lecture_ai.session import SessionManager, SessionState, load_courses
from lecture_ai.transcription import TranscriptResult, TranscriptSegment, write_transcript
from lecture_ai.utils.hashing import sha256_file
from tests.conftest import make_wav


class CountingTranscriber:
    name = "test_asr"
    model_name = "test-medium"

    def __init__(self, text: str = "现在讲数字电路的基本概念，这段课堂内容清楚而且完整。"):
        self.text = text
        self.calls = 0
        self.options = []

    def transcribe(self, audio_path, options=None, progress=None):
        self.calls += 1
        self.options.append(options)
        return TranscriptResult(
            segments=[TranscriptSegment(0.0, 10.0, self.text, no_speech_prob=0.01)],
            language="zh",
            duration_sec=10.0,
            provider=self.name,
            model=self.model_name,
        )

    def close(self):
        return None


def _make_transcribed_session(config, db):
    courses = load_courses(config.courses_path)
    manager = SessionManager(config, db)
    meta = manager.create(
        courses.get("quantum_mechanics"), datetime.fromisoformat("2026-09-02T14:00:00+08:00")
    )
    session_dir = manager.session_dir(meta.session_id)
    audio = make_wav(session_dir / "audio" / "audio_16k.wav", seconds=30)
    meta.audio.processed = "audio/audio_16k.wav"
    meta.audio.duration_sec = 30.0
    manager.save(meta)
    manager.transition(meta, SessionState.AUDIO_READY)
    manager.transition(meta, SessionState.TRANSCRIBING)
    manager.transition(meta, SessionState.TRANSCRIBED)

    result = TranscriptResult(
        segments=[
            TranscriptSegment(0, 5, "今天开始讲数字电路。"),
            TranscriptSegment(5, 15, "梯度 " * 80, no_speech_prob=0.2),
            TranscriptSegment(15, 30, "下面介绍二进制和逻辑门。"),
        ],
        language="zh",
        duration_sec=30.0,
        provider="fake",
        model="raw-model",
    )
    write_transcript(
        result,
        session_dir / "transcript",
        session_id=meta.session_id,
        course_name=meta.course.name,
        date=meta.date,
        audio_start_iso=meta.start_time,
    )
    return manager.load(meta.session_id), session_dir, audio


def test_metrics_detect_repetition_and_accept_only_improvement(config):
    before = measure_text("尤其是" * 80, config.repair)
    after = measure_text("这里说明逻辑门的输入和输出关系。", config.repair)
    worse = measure_text("梯度" * 100, config.repair)

    assert before.suspicious
    assert before.compression_ratio > config.repair.compression_ratio_threshold
    assert decide_repair(before, after, config.repair).accepted
    assert not decide_repair(before, worse, config.repair).accepted


def test_detector_merges_overlapping_padded_windows(config):
    segments = [
        TranscriptSegment(10, 20, "尤其是" * 60),
        TranscriptSegment(25, 35, "梯度" * 60),
        TranscriptSegment(80, 90, "这是正常的课堂讲解文本，内容有足够的多样性。"),
    ]
    regions = detect_suspicious_regions(segments, config.repair, duration_sec=100)

    assert len(regions) == 1
    assert regions[0].segment_ids == [0, 1]
    assert regions[0].window_start == 0
    assert regions[0].window_end == 50


def test_detector_flags_long_segment_with_too_little_text(config):
    segments = [
        TranscriptSegment(
            9.01,
            179.38,
            "上课之前我先做一下自我介绍，我是信息学院的老师。",
            no_speech_prob=0.70,
        ),
        TranscriptSegment(179.38, 187.38, "这里是我的联系方式。"),
    ]

    regions = detect_suspicious_regions(
        segments, config.repair, duration_sec=200
    )

    assert len(regions) == 1
    assert regions[0].segment_ids == [0]
    assert "low_text_density" in regions[0].reasons
    assert regions[0].original_metrics.characters_per_second < 0.6


def test_detector_flags_glossary_prompt_echo(config):
    terms = [
        "薛定谔方程", "Schrodinger", "波函数", "概率密度", "玻恩", "厄米算符",
    ]
    segments = [
        TranscriptSegment(
            60,
            72,
            "薛定谔方程 Schrodinger 波函数 概率密度 玻恩 厄米算符",
        )
    ]

    regions = detect_suspicious_regions(
        segments,
        config.repair,
        duration_sec=100,
        suspicious_terms=terms,
    )

    assert len(regions) == 1
    assert "prompt_echo" in regions[0].reasons
    assert regions[0].original_metrics.prompt_echo_terms == 6
    assert regions[0].original_metrics.prompt_echo_coverage > 0.9


def test_detector_flags_repetition_split_across_short_segments(config):
    segments = [
        TranscriptSegment(index * 3, index * 3 + 3, "巴丁、布拉顿")
        for index in range(config.repair.longest_run_threshold + 1)
    ]

    regions = detect_suspicious_regions(
        segments, config.repair, duration_sec=30
    )

    assert len(regions) == 1
    assert len(regions[0].segment_ids) == config.repair.longest_run_threshold + 1
    assert "cross_segment_repetition" in regions[0].reasons


def test_sparse_recovery_disables_hotwords_and_vad(config, db):
    pipeline = RepairPipeline(config, db, transcriber=CountingTranscriber())
    region = detect_suspicious_regions(
        [TranscriptSegment(0, 120, "只有一句很短的课堂文字。")],
        config.repair,
        duration_sec=120,
    )[0]

    options, strategy = pipeline._region_transcribe_options(
        region, "波函数 薛定谔方程"
    )

    assert options.hotwords is None
    assert options.vad_filter is False
    assert strategy == "sparse_recovery_no_hotwords_no_vad"


def test_sparse_recovery_drops_repeated_short_placeholders(config, db):
    pipeline = RepairPipeline(config, db, transcriber=CountingTranscriber())
    segments = [
        TranscriptSegment(index * 2, index * 2 + 2, "嗯")
        for index in range(config.repair.longest_run_threshold + 2)
    ] + [TranscriptSegment(30, 34, "好，我们开始上课。")]

    kept, dropped = pipeline._drop_short_repetition_runs(
        segments, min_run=config.repair.longest_run_threshold
    )

    assert [segment.text for segment in kept] == ["好，我们开始上课。"]
    assert len(dropped) == config.repair.longest_run_threshold + 2


def test_merge_replaces_only_accepted_window_and_keeps_timeline():
    original = [
        TranscriptSegment(0, 5, "before"),
        TranscriptSegment(5, 15, "bad"),
        TranscriptSegment(15, 20, "after"),
    ]
    history = [{
        "window_start": 5,
        "window_end": 15,
        "decision": {"accepted": True},
        "replacement_segments": [
            TranscriptSegment(5, 10, "fixed one").to_dict(),
            TranscriptSegment(10, 15, "fixed two").to_dict(),
        ],
    }]

    merged = merge_repairs(original, history, 20)

    assert [segment.text for segment in merged] == [
        "before", "fixed one", "fixed two", "after"
    ]
    assert all(a.end <= b.start for a, b in zip(merged, merged[1:]))


def test_rejected_window_keeps_original():
    original = [TranscriptSegment(0, 10, "原始内容")]
    history = [{
        "window_start": 0,
        "window_end": 10,
        "decision": {"accepted": False},
        "replacement_segments": [TranscriptSegment(0, 10, "候选").to_dict()],
    }]
    assert merge_repairs(original, history, 10) == original


def test_repair_pipeline_is_idempotent_and_preserves_raw(config, db):
    meta, session_dir, _ = _make_transcribed_session(config, db)
    raw_json = session_dir / "transcript" / "transcript_raw.json"
    raw_md = session_dir / "transcript" / "transcript_raw.md"
    before = (sha256_file(raw_json), sha256_file(raw_md))
    transcriber = CountingTranscriber()

    def fake_extract(src, dst, start, end):
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"test clip")
        return dst

    pipeline = RepairPipeline(
        config, db, transcriber=transcriber, clip_extractor=fake_extract
    )
    first = pipeline.run(meta.session_id)
    second = pipeline.run(meta.session_id)

    assert first.regions_detected == 1
    assert first.regions_accepted == 1
    assert second.reused
    assert transcriber.calls == 1
    assert before == (sha256_file(raw_json), sha256_file(raw_md))

    repaired = json.loads(
        (session_dir / "transcript" / "transcript_repaired.json").read_text(encoding="utf-8")
    )
    assert repaired["layer"] == "REPAIRED"
    assert repaired["source"]["raw_sha256"] == {"json": before[0], "md": before[1]}
    assert repaired["repair_summary"]["regions_accepted"] == 1
    assert all(
        left["end"] <= right["start"]
        for left, right in zip(repaired["segments"], repaired["segments"][1:])
    )
    final = pipeline.sessions.load(meta.session_id)
    assert final.state is SessionState.TRANSCRIBED
    assert final.steps["repair"].status == "done"
    assert transcriber.options[0].condition_on_previous_text is False

    forced = pipeline.run(meta.session_id, force=True)
    assert not forced.reused
    assert transcriber.calls == 2

    config.repair.padding_seconds = 12
    changed_config = pipeline.run(meta.session_id)
    assert not changed_config.reused
    assert transcriber.calls == 3


def test_repair_dry_run_writes_nothing(config, db):
    meta, session_dir, _ = _make_transcribed_session(config, db)
    outcome = RepairPipeline(config, db, transcriber=CountingTranscriber()).run(
        meta.session_id, dry_run=True
    )
    assert outcome.dry_run and outcome.regions_detected == 1
    assert not (session_dir / "transcript" / "transcript_repaired.json").exists()
    assert "repair" not in RepairPipeline(config, db).sessions.load(meta.session_id).steps


def test_extract_wav_region_has_expected_duration(tmp_path):
    source = make_wav(tmp_path / "source.wav", seconds=4)
    target = extract_wav_region(source, tmp_path / "clip.wav", 1.0, 2.5)
    with wave.open(str(target), "rb") as reader:
        duration = reader.getnframes() / reader.getframerate()
    assert duration == pytest.approx(1.5, abs=0.01)


def test_cli_region_parses_seconds_and_clock():
    assert _parse_region("10-20.5") == (10.0, 20.5)
    assert _parse_region("01:02:03-01:03:04.5") == (3723.0, 3784.5)
    assert _parse_region("11") == 11
    args = build_parser().parse_args([
        "repair", "s1", "--dry-run", "--region", "10-20", "--force"
    ])
    assert args.dry_run and args.region == (10.0, 20.0) and args.force
