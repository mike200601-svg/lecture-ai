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


def _prompt_echo_stats(text: str, terms: Iterable[str]) -> tuple[int, float]:
    """统计词表在文本中的命中数和字符覆盖率，识别弱语音处的 prompt 串入。"""
    compact = _normalized(text)
    if not compact:
        return 0, 0.0

    matched = 0
    spans: list[tuple[int, int]] = []
    seen: set[str] = set()
    for raw_term in terms:
        term = _normalized(raw_term)
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        start = 0
        found = False
        while True:
            index = compact.find(term, start)
            if index < 0:
                break
            found = True
            spans.append((index, index + len(term)))
            start = index + len(term)
        if found:
            matched += 1

    covered = 0
    end = 0
    for left, right in sorted(spans):
        if right <= end:
            continue
        covered += right - max(left, end)
        end = right
    return matched, covered / max(1, len(compact))


def measure_text(
    text: str,
    config: RepairConfig,
    *,
    no_speech_values: Iterable[float | None] = (),
    duration_sec: float | None = None,
    suspicious_terms: Iterable[str] = (),
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
    duration = max(0.0, float(duration_sec)) if duration_sec is not None else None
    chars_per_second = (
        len(compact) / duration if duration is not None and duration > 0 else None
    )
    prompt_echo_terms, prompt_echo_coverage = _prompt_echo_stats(
        text, suspicious_terms
    )

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
    if (
        duration is not None
        and duration >= config.sparse_segment_min_seconds
        and chars_per_second is not None
        and chars_per_second <= config.sparse_segment_max_chars_per_second
    ):
        reasons.append("low_text_density")
    if (
        prompt_echo_terms >= config.prompt_echo_min_terms
        and prompt_echo_coverage >= config.prompt_echo_min_coverage
    ):
        reasons.append("prompt_echo")

    score = (
        max(0.0, compression_ratio / config.compression_ratio_threshold - 1.0)
        + max(0.0, config.unique_char_ratio_threshold - unique_char_ratio)
        / max(0.01, config.unique_char_ratio_threshold)
        + max(0.0, repeated_ngram_ratio / config.repeated_ngram_ratio_threshold - 1.0)
        + max(0.0, longest_run / max(1, config.longest_run_threshold) - 1.0)
    )
    if "low_text_density" in reasons:
        score += 1.0 + min(
            4.0,
            max(
                0.0,
                config.sparse_segment_max_chars_per_second
                / max(0.01, chars_per_second or 0.0)
                - 1.0,
            ),
        )
    if "prompt_echo" in reasons:
        score += 1.0 + max(
            0.0,
            prompt_echo_coverage / max(0.01, config.prompt_echo_min_coverage) - 1.0,
        )
    return TextMetrics(
        character_count=len(compact),
        utf8_bytes=len(payload),
        compression_ratio=round(compression_ratio, 4),
        unique_char_ratio=round(unique_char_ratio, 4),
        repeated_ngram_ratio=round(repeated_ngram_ratio, 4),
        longest_run=longest_run,
        no_speech_mean=round(no_speech_mean, 4) if no_speech_mean is not None else None,
        duration_sec=round(duration, 3) if duration is not None else None,
        characters_per_second=(
            round(chars_per_second, 4) if chars_per_second is not None else None
        ),
        prompt_echo_terms=prompt_echo_terms,
        prompt_echo_coverage=round(prompt_echo_coverage, 4),
        anomaly_score=round(score, 4),
        suspicious=bool(reasons),
        reasons=tuple(reasons),
    )


def measure_segments(
    segments: Iterable[TranscriptSegment],
    config: RepairConfig,
    *,
    suspicious_terms: Iterable[str] = (),
) -> TextMetrics:
    items = list(segments)
    duration = (
        max(segment.end for segment in items) - min(segment.start for segment in items)
        if items else None
    )
    return measure_text(
        " ".join(segment.text for segment in items),
        config,
        no_speech_values=(segment.no_speech_prob for segment in items),
        duration_sec=duration,
        suspicious_terms=suspicious_terms,
    )


def detect_suspicious_regions(
    segments: list[TranscriptSegment],
    config: RepairConfig,
    *,
    duration_sec: float,
    suspicious_terms: Iterable[str] = (),
) -> list[SuspiciousRegion]:
    terms = tuple(suspicious_terms)
    candidates: list[SuspiciousRegion] = []
    padding = max(0.0, float(config.padding_seconds))
    for index, segment in enumerate(segments):
        metrics = measure_segments([segment], config, suspicious_terms=terms)
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

    # 单个短 segment 可能低于 min_text_bytes，但跨多个相邻片段机械重复仍是典型幻觉。
    index = 0
    while index < len(segments):
        key = _normalized(segments[index].text)
        end = index + 1
        while (
            key
            and end < len(segments)
            and _normalized(segments[end].text) == key
        ):
            end += 1
        if key and end - index >= config.longest_run_threshold:
            run = segments[index:end]
            metrics = measure_segments(run, config, suspicious_terms=terms)
            candidates.append(
                SuspiciousRegion(
                    region_id=len(candidates),
                    start=run[0].start,
                    end=run[-1].end,
                    window_start=max(0.0, run[0].start - padding),
                    window_end=min(duration_sec, run[-1].end + padding),
                    segment_ids=list(range(index, end)),
                    reasons=sorted(set(metrics.reasons + ("cross_segment_repetition",))),
                    original_metrics=metrics,
                    text_preview=" … ".join(segment.text for segment in run)[:320],
                )
            )
        index = end

    merged: list[SuspiciousRegion] = []
    for candidate in sorted(candidates, key=lambda item: (item.window_start, item.window_end)):
        if merged and candidate.window_start <= merged[-1].window_end:
            current = merged[-1]
            current.start = min(current.start, candidate.start)
            current.end = max(current.end, candidate.end)
            current.window_end = max(current.window_end, candidate.window_end)
            current.segment_ids = sorted(set(current.segment_ids + candidate.segment_ids))
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
        region.original_metrics = measure_segments(
            window_segments, config, suspicious_terms=terms
        )
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
