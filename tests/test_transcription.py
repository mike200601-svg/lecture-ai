"""转录层测试：术语词典、抽象接口、输出格式、provider 选择与隐私闸门。"""

from __future__ import annotations

import json

import pytest

from lecture_ai.errors import ConfigError
from lecture_ai.transcription import (
    TranscribeOptions,
    TranscriptResult,
    TranscriptSegment,
    build_transcriber,
    load_glossary,
    merge_chunk_results,
)
from lecture_ai.transcription.fake import FakeTranscriber
from lecture_ai.transcription.writer import (
    is_valid_transcript,
    read_transcript,
    write_transcript,
)
from tests.conftest import make_wav


# --------------------------------------------------------------------- 词典


def test_glossary_loads_and_dedups(config):
    g = load_glossary(config.glossary_dir, "quantum_mechanics.txt")
    assert "本征值" in g.terms
    assert "薛定谔方程" in g.terms
    assert g.terms.count("本征值") == 1        # 重复项去重
    assert not any(t.startswith("#") for t in g.terms)  # 注释被过滤


def test_glossary_missing_course_file_is_tolerated(config):
    g = load_glossary(config.glossary_dir, "nonexistent.txt")
    assert "本征值" in g.terms  # common.txt 仍然生效


def test_glossary_missing_dir_returns_empty(tmp_path):
    g = load_glossary(tmp_path / "nope", "x.txt")
    assert len(g) == 0
    assert g.as_hotwords() is None


def test_glossary_hotwords_truncated(config):
    g = load_glossary(config.glossary_dir, "quantum_mechanics.txt")
    assert len(g.as_hotwords(max_terms=2).split(" ")) == 2


def test_glossary_initial_prompt(config):
    prompt = load_glossary(config.glossary_dir, "quantum_mechanics.txt").as_initial_prompt(3)
    assert "薛定谔方程" in prompt or "本征值" in prompt


# --------------------------------------------------------------------- Fake


def test_fake_transcriber_produces_timestamps(tmp_path):
    wav = make_wav(tmp_path / "a.wav", seconds=12)
    result = FakeTranscriber(segment_sec=5).transcribe(wav)

    assert len(result.segments) == 3
    assert result.segments[0].start == 0.0
    assert result.duration_sec == pytest.approx(12.0, abs=0.1)
    # 时间轴必须单调递增 —— Phase 3 靠它对齐板书
    starts = [s.start for s in result.segments]
    assert starts == sorted(starts)
    assert all(s.end > s.start for s in result.segments)


def test_fake_transcriber_progress_callback(tmp_path):
    wav = make_wav(tmp_path / "a.wav", seconds=10)
    seen = []
    FakeTranscriber(segment_sec=5).transcribe(wav, progress=lambda d, t: seen.append((d, t)))
    assert seen and seen[-1][0] == pytest.approx(10.0, abs=0.1)


# --------------------------------------------------------------------- 合并


def test_merge_chunk_results_offsets_timestamps():
    def mk(texts, base=0.0):
        return TranscriptResult(
            segments=[TranscriptSegment(base + i * 5, base + i * 5 + 5, t)
                      for i, t in enumerate(texts)],
            provider="fake", model="m",
        )

    merged = merge_chunk_results([(mk(["a", "b"]), 0.0), (mk(["c", "d"]), 10.0)])
    starts = [s.start for s in merged.segments]
    assert starts == sorted(starts)
    assert starts[-1] == 15.0
    assert merged.extra["chunks"] == 2


def test_merge_empty():
    assert merge_chunk_results([]).segments == []


# --------------------------------------------------------------------- 输出


def test_write_transcript_creates_both_files(tmp_path):
    result = TranscriptResult(
        segments=[
            TranscriptSegment(0.0, 5.2, "我们今天讲波函数。"),
            TranscriptSegment(5.2, 11.0, "注意这里是模方。"),
        ],
        language="zh", duration_sec=11.0, provider="fake", model="fake-v1",
    )
    files = write_transcript(result, tmp_path, session_id="s1",
                             course_name="量子力学", date="2026-09-03")

    assert files.json_path.exists() and files.md_path.exists()

    data = json.loads(files.json_path.read_text(encoding="utf-8"))
    assert data["segment_count"] == 2
    assert data["course"] == "量子力学"
    # 硬要求：必须带时间戳，绝不能只存纯文本
    assert data["segments"][0]["start"] == 0.0
    assert data["segments"][0]["end"] == 5.2
    assert data["segments"][1]["id"] == 1

    md = files.md_path.read_text(encoding="utf-8")
    assert "[00:00:00]" in md
    assert "我们今天讲波函数。" in md
    assert "session_id: s1" in md


def test_transcript_roundtrip(tmp_path):
    result = TranscriptResult(
        segments=[TranscriptSegment(1.0, 2.0, "测试")],
        language="zh", duration_sec=2.0, provider="fake", model="m",
    )
    files = write_transcript(result, tmp_path, session_id="s1")
    restored = read_transcript(files.json_path)
    assert restored.segments[0].text == "测试"
    assert restored.segments[0].start == 1.0
    assert restored.language == "zh"


def test_is_valid_transcript(tmp_path):
    assert is_valid_transcript(tmp_path / "nope.json") is False

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert is_valid_transcript(bad) is False

    empty = tmp_path / "empty.json"
    empty.write_text('{"segments": []}', encoding="utf-8")
    assert is_valid_transcript(empty) is False

    no_ts = tmp_path / "nots.json"
    no_ts.write_text('{"segments": [{"text": "只有文本"}]}', encoding="utf-8")
    assert is_valid_transcript(no_ts) is False

    good = tmp_path / "good.json"
    good.write_text('{"segments": [{"start": 0, "end": 1, "text": "好"}]}', encoding="utf-8")
    assert is_valid_transcript(good) is True


def test_transcript_files_use_lf(tmp_path):
    result = TranscriptResult(segments=[TranscriptSegment(0, 1, "a")], provider="f", model="m")
    files = write_transcript(result, tmp_path, session_id="s1")
    assert b"\r\n" not in files.md_path.read_bytes()


# --------------------------------------------------------------------- registry


def test_build_fake_transcriber(config):
    assert build_transcriber(config).name == "fake"


def test_unknown_provider_rejected(config):
    config.transcription.provider = "magic_asr"
    with pytest.raises(ConfigError, match="未知"):
        build_transcriber(config)


def test_cloud_blocked_by_privacy_switch(config):
    """隐私开关是硬闸门：关着就绝不允许把录音传上云。"""
    config.transcription.provider = "openai"
    config.privacy.allow_cloud_audio = False
    with pytest.raises(ConfigError, match="allow_cloud_audio"):
        build_transcriber(config)


def test_cloud_allowed_when_switch_on(config):
    config.transcription.provider = "openai"
    config.privacy.allow_cloud_audio = True
    assert build_transcriber(config).name == "openai"  # 构造成功，调用时才需要 key


def test_local_whisper_built_from_config(config):
    config.transcription.provider = "local_whisper"
    config.transcription.local_whisper.model = "tiny"
    t = build_transcriber(config)
    assert t.name == "local_whisper"
    assert t.model_name == "tiny"


def test_transcribe_options_defaults():
    o = TranscribeOptions()
    # 长音频必须关掉 condition_on_previous_text，否则 Whisper 会复读
    assert o.condition_on_previous_text is False
    assert o.vad_filter is True


# --------------------------------------------------------------------- 模型缓存


def test_find_cached_model_detects_downloaded(config):
    from lecture_ai.transcription import find_cached_model

    root = config.paths.cache_dir / "models" / "models--Systran--faster-whisper-tiny"
    (root / "snapshots" / "abc").mkdir(parents=True)
    (root / "snapshots" / "abc" / "model.bin").write_bytes(b"weights")

    assert find_cached_model("tiny", config.paths.cache_dir) is not None


def test_find_cached_model_ignores_partial_download(config):
    """只下了 config.json、model.bin 还没落盘时，不能算「已下载」。"""
    from lecture_ai.transcription import find_cached_model

    root = config.paths.cache_dir / "models" / "models--Systran--faster-whisper-small"
    (root / "snapshots" / "abc").mkdir(parents=True)
    (root / "snapshots" / "abc" / "config.json").write_text("{}", encoding="utf-8")

    assert find_cached_model("small", config.paths.cache_dir) is None

    from lecture_ai.transcription import inspect_model_cache

    status = inspect_model_cache("small", config.paths.cache_dir)
    assert status.state == "partial"
    assert status.path == root


def test_find_cached_model_missing(config):
    from lecture_ai.transcription import find_cached_model, inspect_model_cache

    assert find_cached_model("large-v3-turbo", config.paths.cache_dir) is None
    status = inspect_model_cache("large-v3-turbo", config.paths.cache_dir)
    assert status.state == "missing"
    assert status.size_bytes == 0
