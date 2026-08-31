"""转录结果落盘。

总 Prompt 1.6 的硬要求：
  - 必须同时保存 transcript_raw.json 与 transcript_raw.md；
  - JSON 必须带时间戳，**绝对不能只保存纯文本**（Phase 3 要靠时间轴融合板书）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from lecture_ai.transcription.base import TranscriptResult, TranscriptSegment
from lecture_ai.utils.paths import atomic_write_text
from lecture_ai.utils.timefmt import hhmmss, now_local, to_iso

TRANSCRIPT_JSON = "transcript_raw.json"
TRANSCRIPT_MD = "transcript_raw.md"
TRANSCRIPT_SCHEMA_VERSION = 1


@dataclass
class TranscriptFiles:
    json_path: Path
    md_path: Path


def write_transcript(
    result: TranscriptResult,
    transcript_dir: Path,
    *,
    session_id: str,
    course_name: str | None = None,
    date: str | None = None,
    audio_start_iso: str | None = None,
) -> TranscriptFiles:
    """写出 json + md。两个文件都是原子写。"""
    transcript_dir.mkdir(parents=True, exist_ok=True)
    json_path = transcript_dir / TRANSCRIPT_JSON
    md_path = transcript_dir / TRANSCRIPT_MD

    payload = {
        "schema_version": TRANSCRIPT_SCHEMA_VERSION,
        "session_id": session_id,
        "course": course_name,
        "date": date,
        "audio_start": audio_start_iso,
        "provider": result.provider,
        "model": result.model,
        "language": result.language,
        "duration_sec": round(result.duration_sec, 3) if result.duration_sec else None,
        "segment_count": len(result.segments),
        "created_at": to_iso(now_local()),
        "extra": result.extra,
        "segments": [seg.to_dict(i) for i, seg in enumerate(result.segments)],
    }
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write_text(md_path, _render_markdown(result, session_id, course_name, date))
    return TranscriptFiles(json_path=json_path, md_path=md_path)


def _render_markdown(
    result: TranscriptResult,
    session_id: str,
    course_name: str | None,
    date: str | None,
) -> str:
    """人类可读版本。带 front-matter，方便直接丢进 Obsidian 查看原始转录。"""
    lines = [
        "---",
        f"session_id: {session_id}",
        f"course: {course_name or ''}",
        f"date: {date or ''}",
        f"provider: {result.provider}",
        f"model: {result.model}",
        f"language: {result.language or ''}",
        f"duration: {hhmmss(result.duration_sec or 0)}",
        "type: transcript_raw",
        "---",
        "",
        f"# 原始转录 · {course_name or session_id}",
        "",
        "> 本文件是 ASR 直出结果，未经 AI 纠错整理（Phase 2 负责）。",
        "> 专业术语可能有误，以原始录音为准。",
        "",
    ]
    for seg in result.segments:
        lines.append(f"`[{hhmmss(seg.start)}]` {seg.text}")
        lines.append("")
    return "\n".join(lines)


def read_transcript(json_path: Path) -> TranscriptResult:
    """读回 transcript_raw.json。用于 retry 时复用已有结果。"""
    data = json.loads(json_path.read_text(encoding="utf-8"))
    segments = [TranscriptSegment.from_dict(s) for s in data.get("segments", [])]
    return TranscriptResult(
        segments=segments,
        language=data.get("language"),
        duration_sec=data.get("duration_sec"),
        provider=data.get("provider", ""),
        model=data.get("model", ""),
        extra=data.get("extra") or {},
    )


def is_valid_transcript(json_path: Path) -> bool:
    """判断已有转录是否可复用。

    条件放得很严：宁可重跑，也不能拿一个残缺的转录去喂 Phase 2。
    但只要合法就必须复用 —— retry 绝不重跑成功过的 ASR。
    """
    if not json_path.exists():
        return False
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        return False
    first = segments[0]
    return isinstance(first, dict) and "start" in first and "end" in first and "text" in first
