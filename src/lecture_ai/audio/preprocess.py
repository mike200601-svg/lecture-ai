"""音频预处理编排：原始录音 -> audio_16k.wav。

幂等：产物已存在且有效就直接复用，retry 不会重复转码。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lecture_ai.audio.ffmpeg import (
    AudioInfoProbe,
    FFmpegTools,
    convert_to_wav,
    get_tools,
    probe_audio,
    split_audio,
)
from lecture_ai.config import Config
from lecture_ai.logging_setup import get_logger

PROCESSED_NAME = "audio_16k.wav"


@dataclass
class PreprocessResult:
    processed_path: Path
    probe: AudioInfoProbe
    reused: bool                 # True 表示复用了已有产物，没有重新转码
    chunks: list[tuple[Path, float]] | None = None


def tools_from_config(config: Config) -> FFmpegTools:
    return get_tools(config.audio.ffmpeg_path, config.audio.ffprobe_path)


def preprocess_audio(
    raw_path: Path,
    session_dir: Path,
    config: Config,
    *,
    force: bool = False,
    session_id: str | None = None,
) -> PreprocessResult:
    """把原始录音转成 16kHz 单声道 wav。

    raw_path 全程只读 —— 总 Prompt 第十六条：绝不覆盖原始录音。
    """
    log = get_logger(__name__, session_id)
    tools = tools_from_config(config)
    probe = probe_audio(raw_path, tools)
    out_path = session_dir / "audio" / PROCESSED_NAME

    if not force and _is_valid_wav(out_path):
        log.info("复用已有预处理音频：%s", out_path.name)
        return PreprocessResult(out_path, probe, reused=True)

    log.info(
        "开始转码：%s（时长 %.1f 分钟，%s Hz / %s ch）",
        raw_path.name, probe.duration_sec / 60, probe.sample_rate, probe.channels,
    )
    convert_to_wav(
        raw_path,
        out_path,
        tools,
        sample_rate=config.audio.target_sample_rate,
        channels=config.audio.target_channels,
        normalize=config.audio.normalize,
    )
    log.info("转码完成：%s（%.1f MB）", out_path.name, out_path.stat().st_size / 1e6)

    chunks = None
    if _should_chunk(probe.duration_sec, config):
        chunk_sec = config.audio.chunking.chunk_minutes * 60
        log.info("音频较长，启用切片（每片 %d 分钟）", config.audio.chunking.chunk_minutes)
        chunks = split_audio(
            out_path,
            session_dir / "audio" / "chunks",
            tools,
            chunk_sec=chunk_sec,
            overlap_sec=config.audio.chunking.overlap_seconds,
            duration_sec=probe.duration_sec,
        )

    return PreprocessResult(out_path, probe, reused=False, chunks=chunks)


def _should_chunk(duration_sec: float, config: Config) -> bool:
    c = config.audio.chunking
    if c.enabled:
        return True
    return duration_sec > c.auto_threshold_minutes * 60


def _is_valid_wav(path: Path) -> bool:
    """产物是否可复用。只做便宜的检查：存在 + 大于一个 wav 头。"""
    return path.exists() and path.stat().st_size > 1024
