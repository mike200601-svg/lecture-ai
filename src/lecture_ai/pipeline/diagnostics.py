"""Phase 1 音频诊断：检查手机录音元数据与起始时间推断。

这是面向真实录音验收的只读工具，不创建 Session、不转码，也不修改原文件。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from lecture_ai.audio import probe_audio
from lecture_ai.audio.preprocess import tools_from_config
from lecture_ai.config import Config
from lecture_ai.errors import AudioError
from lecture_ai.ingestion import guess_start_time


@dataclass(frozen=True)
class AudioProbeReport:
    file: Path
    duration_sec: float
    codec: str | None
    sample_rate: int | None
    channels: int | None
    creation_time: datetime | None
    mtime: datetime
    ctime: datetime
    inferred_start_time: datetime
    start_time_source: str
    start_time_confidence: str


def probe_audio_metadata(path: Path, config: Config) -> AudioProbeReport:
    """只读探测一个音频文件，并按 Session 规则推断绝对起始时间。"""
    audio_path = Path(path).expanduser().resolve()
    if not audio_path.is_file():
        raise AudioError(f"音频文件不存在或不是普通文件：{audio_path}")

    info = probe_audio(audio_path, tools_from_config(config))
    stat = audio_path.stat()
    mtime = datetime.fromtimestamp(stat.st_mtime).astimezone()
    ctime = datetime.fromtimestamp(stat.st_ctime).astimezone()
    guess = guess_start_time(
        audio_path,
        duration_sec=info.duration_sec,
        creation_time=info.creation_time,
    )

    return AudioProbeReport(
        file=audio_path,
        duration_sec=info.duration_sec,
        codec=info.codec,
        sample_rate=info.sample_rate,
        channels=info.channels,
        creation_time=info.creation_time,
        mtime=mtime,
        ctime=ctime,
        inferred_start_time=guess.dt,
        start_time_source=guess.source,
        start_time_confidence=guess.confidence,
    )
