"""ASR 抽象接口。

总 Prompt 1.4 的硬要求：必须能替换 ASR 实现而不动上层代码。
因此上层只允许依赖本文件的类型，具体实现一律通过 registry 构造。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

#: 进度回调：(已处理秒数, 总秒数)
ProgressCallback = Callable[[float, float], None]


@dataclass(frozen=True)
class TranscriptSegment:
    """一个转录片段。start/end 是相对录音起点的秒数。

    时间戳是 Phase 3 板书融合的基础，绝不能省。
    """

    start: float
    end: float
    text: str
    no_speech_prob: float | None = None
    avg_logprob: float | None = None

    def to_dict(self, index: int | None = None) -> dict[str, Any]:
        data: dict[str, Any] = {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
        }
        if index is not None:
            data = {"id": index, **data}
        if self.no_speech_prob is not None:
            data["no_speech_prob"] = round(self.no_speech_prob, 4)
        if self.avg_logprob is not None:
            data["avg_logprob"] = round(self.avg_logprob, 4)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TranscriptSegment":
        return cls(
            start=float(data["start"]),
            end=float(data["end"]),
            text=str(data.get("text", "")),
            no_speech_prob=data.get("no_speech_prob"),
            avg_logprob=data.get("avg_logprob"),
        )


@dataclass(frozen=True)
class TranscriptResult:
    segments: list[TranscriptSegment]
    language: str | None = None
    duration_sec: float | None = None
    provider: str = ""
    model: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.segments)

    def __len__(self) -> int:
        return len(self.segments)


@dataclass(frozen=True)
class TranscribeOptions:
    """转录参数。默认值面向「中文理工科课堂长录音」。"""

    language: str | None = None
    hotwords: str | None = None
    initial_prompt: str | None = None
    vad_filter: bool = True
    beam_size: int = 5
    temperature: float = 0.0
    #: 长音频必须关掉，否则 Whisper 容易陷入复读
    condition_on_previous_text: bool = False


class Transcriber(ABC):
    """ASR 实现的统一接口。"""

    name: str = "base"
    model_name: str = ""

    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> TranscriptResult:
        """转录音频，返回带时间戳的结果。

        实现必须保证：segments 按时间升序，且 start <= end。
        """

    def close(self) -> None:
        """释放模型资源。默认无操作。"""


def merge_chunk_results(
    results: list[tuple[TranscriptResult, float]],
    overlap_sec: float = 0.0,
) -> TranscriptResult:
    """合并切片转录结果，按偏移量修正时间戳。

    results: [(该片结果, 该片在原音频中的起始偏移秒), ...]
    重叠区去重策略：丢弃起点早于「上一片已覆盖终点」的片段，避免重复文本。
    """
    if not results:
        return TranscriptResult(segments=[])

    merged: list[TranscriptSegment] = []
    covered_until = 0.0
    for result, offset in sorted(results, key=lambda r: r[1]):
        for seg in result.segments:
            start = seg.start + offset
            end = seg.end + offset
            # 落在上一片已覆盖区间内的重复内容直接丢弃
            if merged and start < covered_until - overlap_sec / 2:
                continue
            merged.append(
                TranscriptSegment(
                    start=start,
                    end=end,
                    text=seg.text,
                    no_speech_prob=seg.no_speech_prob,
                    avg_logprob=seg.avg_logprob,
                )
            )
            covered_until = max(covered_until, end)

    first = results[0][0]
    total = max((s.end for s in merged), default=0.0)
    return TranscriptResult(
        segments=merged,
        language=first.language,
        duration_sec=total,
        provider=first.provider,
        model=first.model,
        extra={"chunks": len(results)},
    )
