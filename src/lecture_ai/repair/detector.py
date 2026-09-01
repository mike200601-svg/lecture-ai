"""ASR 复读/幻觉检测与修复质量门控。全部函数均为确定性纯函数。"""

from __future__ import annotations

import re
import zlib
from collections import Counter
from typing import Iterable

from lecture_ai.config import RepairConfig
from lecture_ai.repair.models import RepairDecision, SuspiciousRegion, TextMetrics
from lecture_ai.transcription.base import TranscriptSegment

_NON_CONTENT = re.compile(r"[\s，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]—…·]+")
_TOKENS = re.compile(r"[A-Za-z]+(?:-[A-Za-z0-9]+)*|\d+|[\u3400-\u9fff]+")


def _normalized(text: str) -> str:
    return _NON_CONTENT.sub("", text or "").lower()


def _longest_consecutive_run(text: str) -> int:
    """同时捕获空格分词复读和中文/数字无分隔循环。"""
    tokens = _TOKENS.findall(text.lower())
    best = run = 1 if tokens else 0
    for left, right in zip(tokens, tokens[1:]):
        run = run + 1 if left == right else 1
        best = max(best, run)

    compact = _normalized(text)
    n = len(compact)
    for unit_len in range(1, min(12, n // 2) + 1):
        i = 0
        while i + 2 * unit_len <= n:
            unit = compact[i:i + unit_len]
            count = 1
            while compact[i + count * unit_len:i + (count + 1) * unit_len] == unit:
                count += 1
            best = max(best, count)
            i += max(1, count * unit_len)
    return best


def measure_text(
    text: str,
    config: RepairConfig,
    *,
    no_speech_values: Iterable[float | None] = (),
) -> TextMetrics:
    compact = _normalized(text)
    payload = compact.encode("utf-8")
    compressed = zlib.compress(payload) if payload else b""
    compression_ratio = len(payload) / max(1, len(compressed))
    unique_char_ratio = len(set(compact)) / max(1, len(compact))

    ngrams = [compact[i:i + 3] for i in range(max(0, len(compact) - 2))]
    counts = Counter(ngrams)
    repeated = sum(count - 1 for count in counts.values() if count > 1)
    repeated_ngram_ratio = repeated / max(1, len(ngrams))
    longest_run = _longest_consecutive_run(text)

    speech = [float(value) for value in no_speech_values if value is not None]
    no_speech_mean = sum(speech) / len(speech) if speech else None

    reasons: list[str] = []
    if len(payload) >= config.min_text_bytes:
        if compression_ratio >= config.compression_ratio_threshold:
            reasons.append("high_compression")
        if unique_char_ratio <= config.unique_char_ratio_threshold:
            reasons.append("low_character_diversity")
        if repeated_ngram_ratio >= config.repeated_ngram_ratio_threshold:
            reasons.append("repeated_ngrams")
        if longest_run >= config.longest_run_threshold:
            reasons.append("consecutive_repetition")

    score = (
        max(0.0, compression_ratio / config.compression_ratio_threshold - 1.0)
        + max(0.0, config.unique_char_ratio_threshold - unique_char_ratio)
        / max(0.01, config.unique_char_ratio_threshold)
        + max(0.0, repeated_ngram_ratio / config.repeated_ngram_ratio_threshold - 1.0)
        + max(0.0, longest_run / max(1, config.longest_run_threshold) - 1.0)
    )
    return TextMetrics(
        character_count=len(compact),
        utf8_bytes=len(payload),
        compression_ratio=round(compression_ratio, 4),
        unique_char_ratio=round(unique_char_ratio, 4),
        repeated_ngram_ratio=round(repeated_ngram_ratio, 4),
        longest_run=longest_run,
        no_speech_mean=round(no_speech_mean, 4) if no_speech_mean is not None else None,
        anomaly_score=round(score, 4),
        suspicious=bool(reasons),
        reasons=tuple(reasons),
    )


def measure_segments(segments: Iterable[TranscriptSegment], config: RepairConfig) -> TextMetrics:
    items = list(segments)
    return measure_text(
        " ".join(segment.text for segment in items),
        config,
        no_speech_values=(segment.no_speech_prob for segment in items),
    )


def detect_suspicious_regions(
    segments: list[TranscriptSegment],
    config: RepairConfig,
    *,
    duration_sec: float,
) -> list[SuspiciousRegion]:
    candidates: list[SuspiciousRegion] = []
    padding = max(0.0, float(config.padding_seconds))
    for index, segment in enumerate(segments):
        metrics = measure_segments([segment], config)
        if not metrics.suspicious:
            continue
        candidates.append(
            SuspiciousRegion(
                region_id=len(candidates),
                start=segment.start,
                end=segment.end,
                window_start=max(0.0, segment.start - padding),
                window_end=min(duration_sec, segment.end + padding),
                segment_ids=[index],
                reasons=list(metrics.reasons),
                original_metrics=metrics,
                text_preview=segment.text[:160],
            )
        )

    merged: list[SuspiciousRegion] = []
    for candidate in candidates:
        if merged and candidate.window_start <= merged[-1].window_end:
            current = merged[-1]
            current.start = min(current.start, candidate.start)
            current.end = max(current.end, candidate.end)
            current.window_end = max(current.window_end, candidate.window_end)
            current.segment_ids.extend(candidate.segment_ids)
            current.reasons = sorted(set(current.reasons + candidate.reasons))
            current.text_preview = (current.text_preview + " … " + candidate.text_preview)[:320]
            continue
        candidate.region_id = len(merged)
        merged.append(candidate)

    # 窗口级指标才是重转录前后的可比基线。
    for region in merged:
        window_segments = [
            segment for segment in segments
            if segment.end > region.window_start and segment.start < region.window_end
        ]
        region.original_metrics = measure_segments(window_segments, config)
    return merged


def decide_repair(
    before: TextMetrics,
    after: TextMetrics,
    config: RepairConfig,
) -> RepairDecision:
    if after.character_count == 0:
        return RepairDecision(False, "replacement_empty", 0.0)
    length_ratio = after.character_count / max(1, before.character_count)
    if length_ratio < config.min_length_ratio:
        return RepairDecision(False, "replacement_too_short", 0.0)
    if after.anomaly_score > before.anomaly_score + 1e-9:
        return RepairDecision(False, "anomaly_score_worse", 0.0)

    if before.anomaly_score <= 0:
        improvement = 1.0 if not after.suspicious else 0.0
    else:
        improvement = (before.anomaly_score - after.anomaly_score) / before.anomaly_score

    if not after.suspicious and before.suspicious:
        return RepairDecision(True, "suspicion_cleared", round(improvement, 4))
    if improvement >= config.min_improvement_ratio:
        return RepairDecision(True, "anomaly_score_improved", round(improvement, 4))
    return RepairDecision(False, "insufficient_improvement", round(improvement, 4))
