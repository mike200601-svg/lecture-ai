"""音频处理：ffmpeg 定位、探测、转码、切片。"""

from lecture_ai.audio.ffmpeg import (
    AudioInfoProbe,
    FFmpegTools,
    convert_to_wav,
    ffmpeg_version,
    get_tools,
    probe_audio,
    split_audio,
)
from lecture_ai.audio.preprocess import PROCESSED_NAME, PreprocessResult, preprocess_audio

__all__ = [
    "AudioInfoProbe",
    "FFmpegTools",
    "convert_to_wav",
    "ffmpeg_version",
    "get_tools",
    "probe_audio",
    "split_audio",
    "preprocess_audio",
    "PreprocessResult",
    "PROCESSED_NAME",
]
