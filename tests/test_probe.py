"""真实手机录音验收前的 metadata probe 测试。"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

from lecture_ai.audio.ffmpeg import AudioInfoProbe
from lecture_ai.cli import build_parser
from lecture_ai.errors import AudioError
from lecture_ai.pipeline.diagnostics import probe_audio_metadata
from tests.conftest import make_wav


def test_probe_report_contains_metadata_and_inference(config, tmp_path, monkeypatch):
    audio = make_wav(tmp_path / "录音_20260903_140000.wav", seconds=2)
    creation = datetime(2026, 9, 3, 6, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "lecture_ai.pipeline.diagnostics.probe_audio",
        lambda path, tools: AudioInfoProbe(
            duration_sec=2.0,
            sample_rate=16000,
            channels=1,
            codec="pcm_s16le",
            creation_time=creation,
        ),
    )
    monkeypatch.setattr("lecture_ai.pipeline.diagnostics.tools_from_config", lambda cfg: None)

    report = probe_audio_metadata(audio, config)

    assert report.file == audio.resolve()
    assert report.duration_sec == 2.0
    assert report.codec == "pcm_s16le"
    assert report.sample_rate == 16000
    assert report.channels == 1
    assert report.creation_time == creation
    assert report.start_time_source == "ffprobe"
    assert report.start_time_confidence == "high"
    assert report.mtime.tzinfo is not None
    assert report.ctime.tzinfo is not None


def test_probe_rejects_missing_file(config, tmp_path):
    with pytest.raises(AudioError, match="不存在"):
        probe_audio_metadata(tmp_path / "missing.m4a", config)


def test_probe_cli_is_registered():
    args = build_parser().parse_args(["probe", "课堂录音.m4a"])
    assert args.command == "probe"
    assert args.audio == "课堂录音.m4a"


def test_start_time_final_fallback_uses_ctime(tmp_path):
    from lecture_ai.ingestion import guess_start_time

    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"x")
    old = datetime(2026, 8, 1, 10, 0).timestamp()
    os.utime(audio, (old, old))

    guess = guess_start_time(audio)

    assert guess.source == "ctime"
    assert guess.confidence == "low"
