"""按时间轴生成带重叠的确定性清洗分块。"""

from __future__ import annotations

from lecture_ai.cleaning.models import ChunkPlan
from lecture_ai.errors import LLMError


def build_chunk_plan(
    segments: list[dict],
    *,
    duration_sec: float,
    chunk_minutes: int,
    overlap_seconds: int,
) -> list[ChunkPlan]:
    if not 5 <= int(chunk_minutes) <= 10:
        raise LLMError("clean.chunk_minutes 必须在 5–10 分钟之间")
    if overlap_seconds < 0 or overlap_seconds >= chunk_minutes * 60:
        raise LLMError("clean.overlap_seconds 必须非负且小于分块时长")
    if duration_sec <= 0:
        raise LLMError("清洗输入缺少有效 duration_sec")

    chunk_sec = float(chunk_minutes * 60)
    plans: list[ChunkPlan] = []
    index = 0
    core_start = 0.0
    while core_start < duration_sec:
        core_end = min(duration_sec, core_start + chunk_sec)
        window_start = max(0.0, core_start - overlap_seconds)
        window_end = min(duration_sec, core_end + overlap_seconds)
        ids = tuple(
            int(segment["id"])
            for segment in segments
            if float(segment["end"]) > window_start
            and float(segment["start"]) < window_end
        )
        if ids:
            plans.append(
                ChunkPlan(
                    index=index,
                    core_start=core_start,
                    core_end=core_end,
                    window_start=window_start,
                    window_end=window_end,
                    segment_ids=ids,
                )
            )
        index += 1
        core_start = core_end
    return plans
