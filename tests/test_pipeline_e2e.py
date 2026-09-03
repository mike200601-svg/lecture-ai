"""端到端测试：incoming 音频 -> session -> 转录 -> 带时间戳产物。

ffmpeg 存在时走真实转码路径；不存在时用 wave 模块替身，
保证在任何机器上都能跑（CI、没装 ffmpeg 的新电脑）。
ASR 一律用 FakeTranscriber —— 测的是链路，不是模型。
"""

from __future__ import annotations

import json
import os
import time
import wave
from pathlib import Path

import pytest

from lecture_ai.audio.ffmpeg import AudioInfoProbe
from lecture_ai.errors import TranscriptionError
from lecture_ai.pipeline import Phase1Pipeline
from lecture_ai.session import SessionState
from tests.conftest import make_wav


@pytest.fixture
def pipeline(config, db, has_ffmpeg, monkeypatch):
    """构造 pipeline；无 ffmpeg 时替换掉音频探测与转码。"""
    if not has_ffmpeg:
        _stub_audio_stack(monkeypatch)
    return Phase1Pipeline(config, db)


def _stub_audio_stack(monkeypatch):
    """用标准库 wave 顶替 ffmpeg：probe 读时长，preprocess 直接复制。"""
    import shutil

    from lecture_ai.audio import preprocess as preprocess_mod
    from lecture_ai.pipeline import phase1 as phase1_mod

    def fake_probe(path: Path, tools=None):
        with wave.open(str(path), "rb") as w:
            return AudioInfoProbe(
                duration_sec=w.getnframes() / w.getframerate(),
                sample_rate=w.getframerate(),
                channels=w.getnchannels(),
            )

    def fake_preprocess(raw_path, session_dir, config, *, force=False, session_id=None):
        out = session_dir / "audio" / "audio_16k.wav"
        out.parent.mkdir(parents=True, exist_ok=True)
        reused = out.exists() and not force
        if not reused:
            shutil.copy2(raw_path, out)
        return preprocess_mod.PreprocessResult(out, fake_probe(raw_path), reused=reused)

    monkeypatch.setattr(phase1_mod, "probe_audio", fake_probe)
    monkeypatch.setattr(phase1_mod, "preprocess_audio", fake_preprocess)
    monkeypatch.setattr(phase1_mod, "tools_from_config", lambda cfg: None)


def _drop_audio(config, name: str = "录音_20260902_140000.wav", seconds: float = 12.0) -> Path:
    """把一段录音放进 incoming，并把 mtime 调旧以通过稳定性检测。"""
    path = make_wav(config.paths.incoming_audio / name, seconds=seconds)
    old = time.time() - 300
    os.utime(path, (old, old))
    return path


# --------------------------------------------------------------------- 主链路


def test_full_pipeline(pipeline, config):
    """总 Prompt 的 Phase 1 验收链路：录音 -> session -> ASR -> 带时间戳 transcript。"""
    _drop_audio(config)

    created = pipeline.ingest_new_audio()
    assert len(created) == 1
    meta = created[0]

    # 文件名里带 2026-09-02 14:00（周三），应自动匹配到量子力学
    assert meta.course.key == "quantum_mechanics"
    assert meta.session_id == "2026-09-02_1400_quantum-mechanics_001"
    assert meta.state is SessionState.AUDIO_READY
    assert meta.start_time_confidence == "high"

    outcome = pipeline.process_session(meta.session_id)
    assert outcome.ok, outcome.message

    final = pipeline.sessions.load(meta.session_id)
    assert final.state is SessionState.TRANSCRIBED

    sdir = pipeline.sessions.session_dir(meta.session_id)
    transcript = sdir / "transcript" / "transcript_raw.json"
    assert transcript.exists()
    assert (sdir / "transcript" / "transcript_raw.md").exists()

    data = json.loads(transcript.read_text(encoding="utf-8"))
    assert data["segment_count"] > 0
    assert data["segments"][0]["start"] == 0.0
    assert "end" in data["segments"][0]
    assert data["session_id"] == meta.session_id


def test_original_audio_preserved(pipeline, config):
    """绝不删除、绝不覆盖原始录音。"""
    original = _drop_audio(config)
    original_bytes = original.read_bytes()

    meta = pipeline.ingest_new_audio()[0]
    pipeline.process_session(meta.session_id)

    raw_dir = pipeline.sessions.session_dir(meta.session_id) / "raw"
    archived = list(raw_dir.iterdir())
    assert len(archived) == 1
    assert archived[0].read_bytes() == original_bytes
    assert archived[0].name == "录音_20260902_140000.wav"


def test_keep_incoming_copies_instead_of_moving(pipeline, config):
    config.processing.keep_incoming = True
    original = _drop_audio(config)
    pipeline.ingest_new_audio()
    assert original.exists()


def test_duplicate_file_not_processed_twice(pipeline, config):
    _drop_audio(config)
    assert len(pipeline.ingest_new_audio()) == 1

    # 同样内容再放一次（换个名字）
    src = next((pipeline.sessions.session_dir(pipeline.sessions.list_ids()[0]) / "raw").iterdir())
    dup = config.paths.incoming_audio / "另一个名字.wav"
    dup.write_bytes(src.read_bytes())
    old = time.time() - 300
    os.utime(dup, (old, old))

    assert pipeline.ingest_new_audio() == []
    assert len(pipeline.sessions.list_ids()) == 1


def test_short_audio_skipped(pipeline, config):
    """误触录音（几秒钟）不该建 session。"""
    config.processing.min_audio_seconds = 60
    _drop_audio(config, name="误触_20260902_140000.wav", seconds=3)
    assert pipeline.ingest_new_audio() == []
    assert pipeline.sessions.list_ids() == []


def test_unmatched_time_falls_back_to_unknown(pipeline, config):
    """周日的录音匹配不到课表，但仍然必须完成转录。"""
    _drop_audio(config, name="录音_20260906_090000.wav")  # 2026-09-06 是周日
    meta = pipeline.ingest_new_audio()[0]
    assert meta.course.key == "unknown"
    assert pipeline.process_session(meta.session_id).ok


# --------------------------------------------------------------------- 幂等与重试


def test_reprocess_reuses_transcript(pipeline, config):
    """重复处理不能重跑 ASR —— 这是可恢复设计的核心。"""
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]
    pipeline.process_session(meta.session_id)

    transcript = (pipeline.sessions.session_dir(meta.session_id)
                  / "transcript" / "transcript_raw.json")
    mtime_before = transcript.stat().st_mtime

    outcome = pipeline.process_session(meta.session_id)
    assert outcome.ok
    assert "复用" in outcome.message
    assert transcript.stat().st_mtime == mtime_before  # 文件没被重写


def test_force_transcribe_reruns(pipeline, config):
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]
    pipeline.process_session(meta.session_id)

    outcome = pipeline.process_session(meta.session_id, force=("transcribe",))
    assert outcome.ok
    assert "复用" not in outcome.message


def test_failure_preserves_earlier_artifacts(pipeline, config, monkeypatch):
    """转录失败时，预处理产物必须留着，retry 才不用重跑。"""
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]

    def boom(*args, **kwargs):
        raise TranscriptionError("模拟模型加载失败")

    monkeypatch.setattr(pipeline._get_transcriber().__class__, "transcribe", boom)
    outcome = pipeline.process_session(meta.session_id)

    assert outcome.ok is False
    failed = pipeline.sessions.load(meta.session_id)
    assert failed.state is SessionState.FAILED
    assert failed.failed_from is SessionState.TRANSCRIBING
    assert "模拟模型加载失败" in failed.error

    sdir = pipeline.sessions.session_dir(meta.session_id)
    assert (sdir / "audio" / "audio_16k.wav").exists()      # 预处理产物保留
    assert next((sdir / "raw").iterdir()).exists()          # 原始录音保留
    assert failed.steps["preprocess"].status == "done"


def test_retry_after_failure_succeeds(pipeline, config, monkeypatch):
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]

    cls = pipeline._get_transcriber().__class__
    original = cls.transcribe
    monkeypatch.setattr(cls, "transcribe", lambda *a, **k: (_ for _ in ()).throw(
        TranscriptionError("临时故障")))
    assert pipeline.process_session(meta.session_id).ok is False

    monkeypatch.setattr(cls, "transcribe", original)
    outcome = pipeline.process_session(meta.session_id)

    assert outcome.ok
    assert pipeline.sessions.load(meta.session_id).state is SessionState.TRANSCRIBED


def test_empty_transcript_is_treated_as_failure(pipeline, config, monkeypatch):
    """全静音录音不能被当成成功 —— 否则 Phase 2 会拿到空数据。"""
    from lecture_ai.transcription.base import TranscriptResult

    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]
    monkeypatch.setattr(
        pipeline._get_transcriber().__class__, "transcribe",
        lambda *a, **k: TranscriptResult(segments=[], provider="fake", model="m"),
    )
    outcome = pipeline.process_session(meta.session_id)
    assert outcome.ok is False
    assert pipeline.sessions.load(meta.session_id).state is SessionState.FAILED


# --------------------------------------------------------------------- 批量


def test_process_pending_handles_all(pipeline, config):
    for i in range(2):
        _drop_audio(config, name=f"录音_2026090{i + 2}_140000.wav")
    pipeline.ingest_new_audio()

    outcomes = pipeline.process_pending()
    assert len(outcomes) == 2
    assert all(o.ok for o in outcomes)


def test_run_once_scans_and_processes(pipeline, config):
    _drop_audio(config)
    outcomes = pipeline.run_once()
    assert len(outcomes) == 1 and outcomes[0].ok


def test_auto_process_off_only_ingests(pipeline, config):
    config.processing.auto_process = False
    _drop_audio(config)
    assert pipeline.run_once() == []
    assert pipeline.sessions.load(pipeline.sessions.list_ids()[0]).state is SessionState.AUDIO_READY


def test_session_log_written(pipeline, config):
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]
    pipeline.process_session(meta.session_id)
    log_file = pipeline.sessions.session_dir(meta.session_id) / "logs" / "session.log"
    assert log_file.exists() and log_file.stat().st_size > 0


def test_later_phase_state_not_dragged_back(pipeline, config):
    """已经推进到 Phase 3 状态的 session，重跑 Phase 1 不能把状态往回拉。

    回归用：曾经无条件 transition 到 TRANSCRIBED，会在 IMAGES_READY 上抛非法迁移。
    """
    _drop_audio(config)
    meta = pipeline.ingest_new_audio()[0]
    pipeline.process_session(meta.session_id)

    meta = pipeline.sessions.load(meta.session_id)
    pipeline.sessions.transition(meta, SessionState.IMAGES_READY)

    outcome = pipeline.process_session(meta.session_id)
    assert outcome.ok, outcome.message
    assert pipeline.sessions.load(meta.session_id).state is SessionState.IMAGES_READY
