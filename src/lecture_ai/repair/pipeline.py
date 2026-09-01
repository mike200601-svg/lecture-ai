"""Phase 1.5 编排：检测 -> 扩窗 -> 重转录 -> 质量门控 -> 无损分层输出。"""

from __future__ import annotations

import hashlib
import json
import time
import wave
from dataclasses import asdict
from pathlib import Path
from typing import Any, Callable

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import RepairError
from lecture_ai.repair.detector import (
    decide_repair,
    detect_suspicious_regions,
    measure_segments,
)
from lecture_ai.repair.models import RepairOutcome, SuspiciousRegion
from lecture_ai.session import SessionManager, load_courses
from lecture_ai.transcription import (
    TranscribeOptions,
    TranscriptResult,
    TranscriptSegment,
    build_transcriber,
    load_glossary,
)
from lecture_ai.transcription.writer import TRANSCRIPT_JSON, TRANSCRIPT_MD
from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import hhmmss, now_local, to_iso

REPAIRED_JSON = "transcript_repaired.json"
REPAIRED_MD = "transcript_repaired.md"
REPAIR_CACHE = "repair_cache.json"
REPAIR_SCHEMA_VERSION = 1
STEP_REPAIR = "repair"

ClipExtractor = Callable[[Path, Path, float, float], Path]


def extract_wav_region(src: Path, dst: Path, start: float, end: float) -> Path:
    """从 Phase 1 的 PCM WAV 精确截取时间窗，不重新编码、不触碰源文件。"""
    if start < 0 or end <= start:
        raise RepairError(f"非法修复窗口：{start:.3f}-{end:.3f}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp.wav")
    try:
        with wave.open(str(src), "rb") as reader:
            rate = reader.getframerate()
            total_frames = reader.getnframes()
            begin_frame = min(total_frames, max(0, int(start * rate)))
            end_frame = min(total_frames, max(begin_frame, int(end * rate)))
            reader.setpos(begin_frame)
            frames = reader.readframes(end_frame - begin_frame)
            params = reader.getparams()
        if not frames:
            raise RepairError(f"修复窗口没有音频数据：{start:.3f}-{end:.3f}")
        with wave.open(str(tmp), "wb") as writer:
            writer.setparams(params)
            writer.writeframes(frames)
        tmp.replace(dst)
        return dst
    except (wave.Error, OSError) as exc:
        tmp.unlink(missing_ok=True)
        if isinstance(exc, RepairError):
            raise
        raise RepairError(f"截取修复音频失败：{exc}") from exc


class RepairPipeline:
    def __init__(
        self,
        config: Config,
        db: Database | None = None,
        *,
        transcriber=None,
        clip_extractor: ClipExtractor = extract_wav_region,
    ) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self.courses = load_courses(config.courses_path, config.course.default_course_key)
        self._transcriber = transcriber
        self._owns_transcriber = transcriber is None
        self.clip_extractor = clip_extractor

    def run(
        self,
        session_id: str,
        *,
        dry_run: bool = False,
        region: int | tuple[float, float] | None = None,
        force: bool = False,
    ) -> RepairOutcome:
        started = time.monotonic()
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        transcript_dir = session_dir / "transcript"
        analysis_dir = session_dir / "analysis"
        raw_json = transcript_dir / TRANSCRIPT_JSON
        raw_md = transcript_dir / TRANSCRIPT_MD
        if not raw_json.exists() or not raw_md.exists():
            raise RepairError(f"RAW 转录不完整：需要 {TRANSCRIPT_JSON} 和 {TRANSCRIPT_MD}")

        raw_sha = {"json": sha256_file(raw_json), "md": sha256_file(raw_md)}
        payload = self._read_raw(raw_json)
        segments = [TranscriptSegment.from_dict(item) for item in payload["segments"]]
        duration = float(payload.get("duration_sec") or meta.audio.duration_sec or 0.0)
        if duration <= 0:
            duration = max((segment.end for segment in segments), default=0.0)

        regions = detect_suspicious_regions(
            segments, self.config.repair, duration_sec=duration
        )
        if region is not None:
            regions = self._select_region(regions, segments, region, duration)
        detected_count = sum(len(item.segment_ids) for item in regions)

        glossary = self._glossary(meta.course.key)
        fingerprint = self._fingerprint(raw_sha, glossary.terms, region)
        output_json = transcript_dir / REPAIRED_JSON
        output_md = transcript_dir / REPAIRED_MD
        previous_asr_elapsed = self._previous_asr_elapsed(
            output_json, fingerprint, raw_sha
        )

        if dry_run:
            return RepairOutcome(
                session_id=session_id,
                regions_detected=detected_count,
                dry_run=True,
                elapsed_sec=time.monotonic() - started,
                message=(
                    f"检测 {detected_count} 个可疑 segments，"
                    f"合并为 {len(regions)} 个扩窗区域"
                ),
                regions=[item.to_dict() for item in regions],
            )

        if not force and self._valid_cached_output(output_json, fingerprint, raw_sha):
            data = json.loads(output_json.read_text(encoding="utf-8"))
            summary = data.get("repair_summary") or {}
            self.sessions.mark_step(
                meta,
                STEP_REPAIR,
                "done",
                elapsed_sec=0.0,
                provider=data.get("provider"),
                model=data.get("model"),
            )
            self._assert_raw_unchanged(raw_json, raw_md, raw_sha)
            return RepairOutcome(
                session_id=session_id,
                regions_detected=int(summary.get("regions_detected", len(regions))),
                regions_processed=int(summary.get("regions_processed", 0)),
                regions_accepted=int(summary.get("regions_accepted", 0)),
                reused=True,
                output_json=str(output_json),
                output_md=str(output_md),
                elapsed_sec=time.monotonic() - started,
                message="复用已有选择性修复产物",
            )

        transcriber = None
        self.sessions.mark_step(meta, STEP_REPAIR, "running")
        try:
            cache_path = analysis_dir / REPAIR_CACHE
            cache = (
                {
                    "schema_version": REPAIR_SCHEMA_VERSION,
                    "fingerprint": fingerprint,
                    "raw_sha256": raw_sha,
                    "windows": {},
                }
                if force
                else self._load_cache(cache_path, fingerprint, raw_sha)
            )
            options = self._transcribe_options(glossary.as_hotwords())
            history: list[dict[str, Any]] = []

            if regions:
                transcriber = self._get_transcriber()
            for item in regions:
                key = f"{item.window_start:.3f}-{item.window_end:.3f}"
                cached = cache["windows"].get(key)
                if cached is not None:
                    record = dict(cached)
                    record["cache_hit"] = True
                    original = self._segments_in_window(
                        segments, item.window_start, item.window_end
                    )
                    replacements = [
                        TranscriptSegment.from_dict(value)
                        for value in record.get("replacement_segments", [])
                    ]
                    record.setdefault(
                        "original_text", " ".join(segment.text for segment in original)
                    )
                    record.setdefault(
                        "repaired_text", " ".join(segment.text for segment in replacements)
                    )
                    cache["windows"][key] = record
                    atomic_write_text(
                        cache_path, json.dumps(cache, ensure_ascii=False, indent=2)
                    )
                    history.append(record)
                    continue

                clip = analysis_dir / "repair_clips" / (
                    f"region_{item.region_id:03d}_{item.window_start:.3f}_"
                    f"{item.window_end:.3f}.wav"
                )
                self.clip_extractor(
                    self._processed_audio(meta, session_dir),
                    clip,
                    item.window_start,
                    item.window_end,
                )
                result: TranscriptResult = transcriber.transcribe(clip, options)
                replacement = self._offset_result(result, item.window_start, item.window_end)
                original = self._segments_in_window(
                    segments, item.window_start, item.window_end
                )
                before = measure_segments(original, self.config.repair)
                after = measure_segments(replacement, self.config.repair)
                decision = decide_repair(before, after, self.config.repair)
                record = {
                    **item.to_dict(),
                    "before_metrics": before.to_dict(),
                    "after_metrics": after.to_dict(),
                    "decision": decision.to_dict(),
                    "replacement_segment_count": len(replacement),
                    "replacement_segments": [segment.to_dict() for segment in replacement],
                    "original_text": " ".join(segment.text for segment in original),
                    "repaired_text": " ".join(segment.text for segment in replacement),
                    "asr_elapsed_sec": float(result.extra.get("elapsed_sec") or 0.0),
                    "cache_hit": False,
                }
                history.append(record)
                cache["windows"][key] = record
                atomic_write_text(
                    cache_path, json.dumps(cache, ensure_ascii=False, indent=2)
                )

            repaired = merge_repairs(segments, history, duration)
            self._assert_raw_unchanged(raw_json, raw_md, raw_sha)
            result_payload = self._result_payload(
                payload=payload,
                repaired=repaired,
                history=history,
                raw_sha=raw_sha,
                fingerprint=fingerprint,
                glossary_sources=glossary.sources,
                elapsed_sec=time.monotonic() - started,
                previous_asr_elapsed=previous_asr_elapsed,
            )
            atomic_write_text(
                output_json, json.dumps(result_payload, ensure_ascii=False, indent=2)
            )
            atomic_write_text(output_md, render_repaired_markdown(result_payload))
            self._assert_raw_unchanged(raw_json, raw_md, raw_sha)

            accepted = sum(
                bool(record.get("decision", {}).get("accepted")) for record in history
            )
            elapsed = time.monotonic() - started
            provider, model = self._provider_model(transcriber)
            self.sessions.mark_step(
                meta,
                STEP_REPAIR,
                "done",
                elapsed_sec=elapsed,
                provider=provider,
                model=model,
            )
            return RepairOutcome(
                session_id=session_id,
                regions_detected=detected_count,
                regions_processed=len(history),
                regions_accepted=accepted,
                output_json=str(output_json),
                output_md=str(output_md),
                elapsed_sec=elapsed,
                message=f"处理 {len(history)} 个区域，接受 {accepted} 个修复",
                regions=history,
            )
        except Exception as exc:
            self.sessions.mark_step(meta, STEP_REPAIR, "failed", error=str(exc))
            self._assert_raw_unchanged(raw_json, raw_md, raw_sha)
            if isinstance(exc, RepairError):
                raise
            raise RepairError(f"选择性重转录失败：{exc}") from exc
        finally:
            if self._owns_transcriber and transcriber is not None:
                transcriber.close()
                self._transcriber = None

    def _read_raw(self, path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RepairError(f"无法读取 RAW 转录：{exc}") from exc
        if not isinstance(data.get("segments"), list) or not data["segments"]:
            raise RepairError("RAW 转录没有有效 segments")
        return data

    def _processed_audio(self, meta, session_dir: Path) -> Path:
        value = meta.audio.processed or "audio/audio_16k.wav"
        path = Path(value)
        path = path if path.is_absolute() else session_dir / path
        if not path.exists():
            raise RepairError(f"预处理音频不存在：{path}")
        return path

    def _glossary(self, course_key: str):
        course = self.courses.get(course_key)
        return load_glossary(
            self.config.glossary_dir, course.glossary, include_common=False
        )

    def _transcribe_options(self, hotwords: str | None) -> TranscribeOptions:
        lw = self.config.transcription.local_whisper
        return TranscribeOptions(
            language=lw.language,
            hotwords=hotwords,
            vad_filter=lw.vad_filter,
            beam_size=lw.beam_size,
            condition_on_previous_text=lw.condition_on_previous_text,
            repetition_penalty=lw.repetition_penalty,
            no_repeat_ngram_size=lw.no_repeat_ngram_size,
        )

    def _get_transcriber(self):
        if self._transcriber is None:
            self._transcriber = build_transcriber(self.config)
        return self._transcriber

    def _fingerprint(
        self,
        raw_sha: dict[str, str],
        glossary_terms: list[str],
        selected_region: int | tuple[float, float] | None,
    ) -> str:
        data = {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "raw_sha256": raw_sha,
            "repair": asdict(self.config.repair),
            "provider": self.config.transcription.provider,
            "local_whisper": asdict(self.config.transcription.local_whisper),
            "glossary_terms": glossary_terms,
            "selected_region": selected_region,
        }
        canonical = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_cached_output(
        path: Path, fingerprint: str, raw_sha: dict[str, str]
    ) -> bool:
        if not path.exists():
            return False
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            data.get("repair", {}).get("fingerprint") == fingerprint
            and data.get("source", {}).get("raw_sha256") == raw_sha
            and isinstance(data.get("source_transcript"), dict)
            and bool(data.get("repair_model"))
            and isinstance(data.get("repair_config"), dict)
            and all(
                "original_text" in item and "repaired_text" in item
                for item in data.get("repair_history", [])
            )
            and isinstance(data.get("segments"), list)
        )

    @staticmethod
    def _previous_asr_elapsed(
        path: Path, fingerprint: str, raw_sha: dict[str, str]
    ) -> float:
        if not path.exists():
            return 0.0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0.0
        if (
            data.get("repair", {}).get("fingerprint") != fingerprint
            or data.get("source", {}).get("raw_sha256") != raw_sha
        ):
            return 0.0
        summary = data.get("repair_summary") or {}
        return float(
            summary.get("asr_extra_elapsed_sec")
            or summary.get("elapsed_sec")
            or 0.0
        )

    @staticmethod
    def _load_cache(
        path: Path, fingerprint: str, raw_sha: dict[str, str]
    ) -> dict[str, Any]:
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("fingerprint") == fingerprint:
                    return data
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "fingerprint": fingerprint,
            "raw_sha256": raw_sha,
            "windows": {},
        }

    def _select_region(
        self,
        detected: list[SuspiciousRegion],
        segments: list[TranscriptSegment],
        requested: int | tuple[float, float],
        duration: float,
    ) -> list[SuspiciousRegion]:
        if isinstance(requested, int):
            selected = [item for item in detected if item.region_id == requested]
            if not selected:
                choices = ", ".join(str(item.region_id) for item in detected) or "无"
                raise RepairError(
                    f"--region {requested} 不存在；当前可疑 region ids：{choices}"
                )
            selected[0].region_id = requested
            return selected
        start, end = requested
        if start < 0 or end <= start or start >= duration:
            raise RepairError(f"非法 --region：{start:.3f}-{end:.3f}")
        end = min(duration, end)
        selected = [item for item in detected if item.end > start and item.start < end]
        if selected:
            for index, item in enumerate(selected):
                item.region_id = index
            return selected

        padding = max(0.0, self.config.repair.padding_seconds)
        window_start = max(0.0, start - padding)
        window_end = min(duration, end + padding)
        ids = [
            index for index, segment in enumerate(segments)
            if segment.end > start and segment.start < end
        ]
        window_segments = self._segments_in_window(segments, window_start, window_end)
        return [
            SuspiciousRegion(
                region_id=0,
                start=start,
                end=end,
                window_start=window_start,
                window_end=window_end,
                segment_ids=ids,
                reasons=["manual_region"],
                original_metrics=measure_segments(window_segments, self.config.repair),
                text_preview=" ".join(segment.text for segment in window_segments)[:160],
            )
        ]

    @staticmethod
    def _segments_in_window(
        segments: list[TranscriptSegment], start: float, end: float
    ) -> list[TranscriptSegment]:
        return [segment for segment in segments if segment.end > start and segment.start < end]

    @staticmethod
    def _offset_result(
        result: TranscriptResult, offset: float, window_end: float
    ) -> list[TranscriptSegment]:
        replacement: list[TranscriptSegment] = []
        for segment in result.segments:
            start = max(offset, offset + segment.start)
            end = min(window_end, offset + segment.end)
            text = segment.text.strip()
            if text and end > start:
                replacement.append(
                    TranscriptSegment(
                        start=start,
                        end=end,
                        text=text,
                        no_speech_prob=segment.no_speech_prob,
                        avg_logprob=segment.avg_logprob,
                    )
                )
        return replacement

    def _result_payload(
        self,
        *,
        payload: dict[str, Any],
        repaired: list[TranscriptSegment],
        history: list[dict[str, Any]],
        raw_sha: dict[str, str],
        fingerprint: str,
        glossary_sources: list[str],
        elapsed_sec: float,
        previous_asr_elapsed: float,
    ) -> dict[str, Any]:
        accepted = sum(
            bool(record.get("decision", {}).get("accepted")) for record in history
        )
        original = [TranscriptSegment.from_dict(item) for item in payload["segments"]]
        before = measure_segments(original, self.config.repair)
        after = measure_segments(repaired, self.config.repair)
        remaining = detect_suspicious_regions(
            repaired,
            self.config.repair,
            duration_sec=float(payload.get("duration_sec") or 0.0),
        )
        detected_segment_ids = sorted({
            int(segment_id)
            for record in history
            for segment_id in record.get("segment_ids", [])
        })
        original_suspicious_duration = sum(
            max(0.0, original[index].end - original[index].start)
            for index in detected_segment_ids
            if 0 <= index < len(original)
        )
        remaining_ids = sorted({
            int(segment_id)
            for region in remaining
            for segment_id in region.segment_ids
        })
        final_suspicious_duration = sum(
            max(0.0, repaired[index].end - repaired[index].start)
            for index in remaining_ids
            if 0 <= index < len(repaired)
        )
        original_max_compression = max(
            (measure_segments([segment], self.config.repair).compression_ratio for segment in original),
            default=0.0,
        )
        final_max_compression = max(
            (measure_segments([segment], self.config.repair).compression_ratio for segment in repaired),
            default=0.0,
        )
        provider, model = self._provider_model(self._transcriber)
        measured_asr_elapsed = sum(
            float(record.get("asr_elapsed_sec") or 0.0) for record in history
        )
        asr_extra_elapsed = measured_asr_elapsed or previous_asr_elapsed
        return {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "layer": "REPAIRED",
            "session_id": payload.get("session_id"),
            "course": payload.get("course"),
            "date": payload.get("date"),
            "audio_start": payload.get("audio_start"),
            "duration_sec": payload.get("duration_sec"),
            "language": payload.get("language"),
            "provider": provider or payload.get("provider"),
            "model": model or payload.get("model"),
            "created_at": to_iso(now_local()),
            "source": {
                "layer": "RAW",
                "json": TRANSCRIPT_JSON,
                "markdown": TRANSCRIPT_MD,
                "raw_sha256": raw_sha,
            },
            "source_transcript": {
                "json": TRANSCRIPT_JSON,
                "markdown": TRANSCRIPT_MD,
                "sha256": raw_sha,
            },
            "repair_model": model or payload.get("model"),
            "repair_config": asdict(self.config.repair),
            "repair": {
                "fingerprint": fingerprint,
                "config": asdict(self.config.repair),
                "glossary_sources": glossary_sources,
            },
            "repair_summary": {
                "detected_regions": len(detected_segment_ids),
                "regions_detected": len(detected_segment_ids),
                "repair_windows": len(history),
                "attempted": len(history),
                "regions_processed": len(history),
                "accepted": accepted,
                "regions_accepted": accepted,
                "rejected": len(history) - accepted,
                "regions_rejected": len(history) - accepted,
                "original_suspicious_duration_sec": round(original_suspicious_duration, 3),
                "final_suspicious_duration_sec": round(final_suspicious_duration, 3),
                "original_max_compression_ratio": round(original_max_compression, 4),
                "final_max_compression_ratio": round(final_max_compression, 4),
                "segments_before": len(original),
                "segments_after": len(repaired),
                "elapsed_sec": round(elapsed_sec, 2),
                "asr_extra_elapsed_sec": round(asr_extra_elapsed, 2),
                "metrics_before": before.to_dict(),
                "metrics_after": after.to_dict(),
            },
            "repair_history": history,
            "segment_count": len(repaired),
            "segments": [segment.to_dict(index) for index, segment in enumerate(repaired)],
        }

    @staticmethod
    def _provider_model(transcriber) -> tuple[str | None, str | None]:
        if transcriber is None:
            return None, None
        return getattr(transcriber, "name", None), getattr(transcriber, "model_name", None)

    @staticmethod
    def _assert_raw_unchanged(
        raw_json: Path, raw_md: Path, expected: dict[str, str]
    ) -> None:
        actual = {"json": sha256_file(raw_json), "md": sha256_file(raw_md)}
        if actual != expected:
            raise RepairError(
                "RAW 完整性校验失败：transcript_raw 文件在修复过程中发生变化"
            )


def merge_repairs(
    original: list[TranscriptSegment],
    history: list[dict[str, Any]],
    duration_sec: float,
) -> list[TranscriptSegment]:
    """按接受窗口替换原 segments，并强制得到单调、无重叠时间轴。"""
    accepted = [
        record for record in history if record.get("decision", {}).get("accepted")
    ]
    accepted.sort(key=lambda record: float(record["window_start"]))
    for left, right in zip(accepted, accepted[1:]):
        if float(right["window_start"]) < float(left["window_end"]):
            raise RepairError("接受的修复窗口互相重叠，拒绝生成不确定合并结果")

    retained = [
        segment for segment in original
        if not any(
            segment.end > float(record["window_start"])
            and segment.start < float(record["window_end"])
            for record in accepted
        )
    ]
    replacements = [
        TranscriptSegment.from_dict(item)
        for record in accepted
        for item in record.get("replacement_segments", [])
    ]
    candidates = sorted(retained + replacements, key=lambda segment: (segment.start, segment.end))

    merged: list[TranscriptSegment] = []
    for segment in candidates:
        start = max(0.0, float(segment.start))
        end = min(float(duration_sec), float(segment.end))
        if merged and start < merged[-1].end:
            start = merged[-1].end
        if end <= start or not segment.text.strip():
            continue
        merged.append(
            TranscriptSegment(
                start=start,
                end=end,
                text=segment.text.strip(),
                no_speech_prob=segment.no_speech_prob,
                avg_logprob=segment.avg_logprob,
            )
        )
    _validate_timeline(merged, duration_sec)
    return merged


def _validate_timeline(segments: list[TranscriptSegment], duration_sec: float) -> None:
    previous_end = 0.0
    for index, segment in enumerate(segments):
        if segment.start < previous_end - 1e-6:
            raise RepairError(f"修复结果时间轴重叠：segment {index}")
        if segment.end < segment.start or segment.start < 0:
            raise RepairError(f"修复结果时间戳非法：segment {index}")
        if duration_sec and segment.end > duration_sec + 1e-3:
            raise RepairError(f"修复结果越过音频结尾：segment {index}")
        previous_end = segment.end


def render_repaired_markdown(payload: dict[str, Any]) -> str:
    summary = payload["repair_summary"]
    lines = [
        "---",
        f"session_id: {payload.get('session_id', '')}",
        f"course: {payload.get('course', '')}",
        f"date: {payload.get('date', '')}",
        "type: transcript_repaired",
        "source: transcript_raw.json",
        f"repair_fingerprint: {payload['repair']['fingerprint']}",
        "---",
        "",
        f"# 选择性修复转录 · {payload.get('course') or payload.get('session_id')}",
        "",
        (
            f"> 检测 {summary['regions_detected']} 个可疑 segments，合并为 "
            f"{summary['repair_windows']} 个窗口；接受 {summary['regions_accepted']} 个修复，"
            f"拒绝 {summary['regions_rejected']} 个。"
        ),
        "> RAW 转录未被覆盖；每个决定见 JSON 的 repair_history。",
        "",
    ]
    for segment in payload["segments"]:
        lines.extend((f"`[{hhmmss(float(segment['start']))}]` {segment['text']}", ""))
    return "\n".join(lines)
