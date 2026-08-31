"""FakeTranscriber：不依赖任何模型的假实现。

存在意义：
  - 端到端测试可以在没有 GPU、没有模型、没有网络的情况下跑通整条链路；
  - 开发时调试 pipeline 不必等几十分钟的真实转录。
"""

from __future__ import annotations

import wave
from pathlib import Path

from lecture_ai.transcription.base import (
    ProgressCallback,
    TranscribeOptions,
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
)

_DEFAULT_TEXTS = [
    "我们今天讲波函数的统计解释。",
    "这个式子里面，模方代表概率密度。",
    "把它代进去，就得到上面这个结果。",
    "注意这里是模方，不是波函数本身，考试经常错。",
]


class FakeTranscriber(Transcriber):
    name = "fake"

    def __init__(self, segment_sec: float = 5.0, texts: list[str] | None = None,
                 model: str = "fake-v1") -> None:
        self.segment_sec = segment_sec
        self.texts = texts or _DEFAULT_TEXTS
        self.model_name = model

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> TranscriptResult:
        duration = _wav_duration(audio_path) or 30.0
        segments: list[TranscriptSegment] = []
        t = 0.0
        i = 0
        while t < duration:
            end = min(t + self.segment_sec, duration)
            segments.append(
                TranscriptSegment(start=t, end=end, text=self.texts[i % len(self.texts)])
            )
            if progress:
                progress(end, duration)
            t = end
            i += 1

        return TranscriptResult(
            segments=segments,
            language=(options.language if options else None) or "zh",
            duration_sec=duration,
            provider=self.name,
            model=self.model_name,
            extra={"fake": True},
        )


def _wav_duration(path: Path) -> float | None:
    """用标准库读 wav 时长，不依赖 ffmpeg（测试环境可能没装）。"""
    try:
        with wave.open(str(path), "rb") as w:
            rate = w.getframerate()
            return w.getnframes() / rate if rate else None
    except (wave.Error, OSError, EOFError):
        return None
