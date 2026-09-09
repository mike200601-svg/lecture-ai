"""把一节课被切成几段的录音合并回一个 session。

录音机中途断一次（手滑、来电、存储抖动），一节课就会变成两个文件，
watch 会老老实实建成两个 session：转录分家、投喂包分家、连「这门课的第几次课」
的序号都被白占一个。2026-09-09 的量子力学就是这样断成 09:44 和 10:34 两段。

**为什么在转录之后合并，而不是先把音频拼起来再转录**：项目的红线是
「retry 绝不重跑已经成功的 ASR」。转录是整条流水线最贵的一步（一节课 45–65
分钟），已经跑完的结果必须留住。

**为什么连音频一起拼**：merge 完还要跑 repair，而 repair 是拿着时间戳回到
音频里重转可疑区间的。只并转录不并音频的话，第二段的时间戳会落在第一段音频
的范围之外，repair 要么越界要么重转出完全无关的内容。所以合并后的 session
必须是自洽的：一条时间轴，一个音频，两者对齐。

**断口用静音补齐**，不是首尾相接。时间戳继续对齐墙钟，Phase 3 拿板书照片的
EXIF 时间对时才不会整体偏移。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from lecture_ai.audio.ffmpeg import get_tools
from lecture_ai.audio.preprocess import PROCESSED_NAME, tools_from_config
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import LectureAIError
from lecture_ai.logging_setup import get_logger
from lecture_ai.session import SessionManager, SessionMeta, SessionState
from lecture_ai.transcription.base import TranscriptResult, TranscriptSegment
from lecture_ai.transcription.writer import TRANSCRIPT_JSON, write_transcript
from lecture_ai.utils.timefmt import hhmmss, to_iso

log = get_logger(__name__)

#: 两段之间超过这个秒数才认为是"中断"，否则当作紧邻拼接。
#: 录音机停止再开始通常要几秒，那点缝隙不值得在转录里标出来。
MIN_GAP_SEC = 20.0

#: 允许的最大中断。超过说明这两段多半不是同一节课，宁可拒绝也不要合错。
MAX_GAP_SEC = 40 * 60.0

#: Phase 1 做完才能合并 —— 必须已经有转录可并。
MERGEABLE_STATES = frozenset({
    SessionState.TRANSCRIBED,
    SessionState.IMAGES_READY,
    SessionState.GENERATING_NOTE,
    SessionState.EXPORTED,
    SessionState.DONE,
})


@dataclass
class MergePart:
    meta: SessionMeta
    offset_sec: float          # 相对第一段起点的偏移
    gap_before_sec: float      # 与上一段之间的静音长度（第一段为 0）
    segments: list[TranscriptSegment] = field(default_factory=list)


@dataclass
class MergeOutcome:
    primary_id: str
    merged_ids: list[str]
    segment_count: int
    duration_sec: float
    gaps: list[tuple[float, float]]     # [(在合并时间轴上的位置, 中断时长), ...]
    audio_merged: bool

    @property
    def message(self) -> str:
        gaps = "、".join(
            f"{hhmmss(at)} 处断 {hhmmss(length)}" for at, length in self.gaps
        ) or "无中断"
        return (f"并入 {len(self.merged_ids)} 段，共 {self.segment_count} 个片段 / "
                f"{hhmmss(self.duration_sec)}（{gaps}）")


class SessionMerger:
    def __init__(self, config: Config, db: Database | None = None) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)

    # ------------------------------------------------------------------ 校验

    def plan(self, session_ids: list[str], *, allow_any_course: bool = False) -> list[MergePart]:
        """做完全部校验并按时间排好序。不写任何东西，供 --dry-run 使用。"""
        if len(session_ids) < 2:
            raise LectureAIError("至少要给两个 session 才能合并。")
        if len(set(session_ids)) != len(session_ids):
            raise LectureAIError("同一个 session 不能合并两次。")

        metas = [self.sessions.load(sid) for sid in session_ids]

        for meta in metas:
            if meta.merged_into:
                raise LectureAIError(
                    f"{meta.session_id} 已经并进 {meta.merged_into} 了，不能再合并。"
                )
            if meta.state not in MERGEABLE_STATES:
                raise LectureAIError(
                    f"{meta.session_id} 还没转录完（当前 {meta.state}），"
                    "等它转完再合并 —— 合并不会重跑 ASR，所以必须先有转录。"
                )
            if not meta.start_time:
                raise LectureAIError(f"{meta.session_id} 没有起始时间，无法对齐时间轴。")

        courses = {m.course.key for m in metas}
        if len(courses) > 1 and not allow_any_course:
            names = "、".join(sorted(m.course.name for m in metas))
            raise LectureAIError(
                f"这些 session 属于不同课程（{names}）。确认无误可加 --allow-any-course。"
            )

        dates = {m.date for m in metas}
        if len(dates) > 1:
            raise LectureAIError(f"跨天的 session 不能合并：{'、'.join(sorted(dates))}")

        metas.sort(key=lambda m: _start_of(m))
        base = _start_of(metas[0])

        parts: list[MergePart] = []
        prev_end: datetime | None = None
        for meta in metas:
            start = _start_of(meta)
            gap = 0.0
            if prev_end is not None:
                gap = (start - prev_end).total_seconds()
                if gap < -1.0:
                    raise LectureAIError(
                        f"{meta.session_id} 与上一段在时间上重叠了 {hhmmss(-gap)}，"
                        "合并会产生重复内容。请先确认录音文件是否重复导入。"
                    )
                if gap > MAX_GAP_SEC:
                    raise LectureAIError(
                        f"{meta.session_id} 与上一段相隔 {hhmmss(gap)}，超过 "
                        f"{hhmmss(MAX_GAP_SEC)}，多半不是同一节课。"
                    )
                gap = max(0.0, gap)

            segments = _load_segments(self.sessions.session_dir(meta.session_id))
            if not segments:
                raise LectureAIError(f"{meta.session_id} 没有可用的转录片段。")

            parts.append(MergePart(
                meta=meta,
                offset_sec=(start - base).total_seconds(),
                gap_before_sec=gap if gap >= MIN_GAP_SEC else 0.0,
                segments=segments,
            ))
            prev_end = start + timedelta(seconds=meta.audio.duration_sec or 0.0)

        return parts

    # ------------------------------------------------------------------ 执行

    def merge(self, session_ids: list[str], *, allow_any_course: bool = False,
              keep_audio: bool = True) -> MergeOutcome:
        parts = self.plan(session_ids, allow_any_course=allow_any_course)
        primary, rest = parts[0], parts[1:]
        primary_dir = self.sessions.session_dir(primary.meta.session_id)

        merged: list[TranscriptSegment] = []
        gaps: list[tuple[float, float]] = []
        for part in parts:
            if part.gap_before_sec:
                gaps.append((part.offset_sec - part.gap_before_sec, part.gap_before_sec))
            merged.extend(
                TranscriptSegment(
                    start=seg.start + part.offset_sec,
                    end=seg.end + part.offset_sec,
                    text=seg.text,
                    no_speech_prob=seg.no_speech_prob,
                    avg_logprob=seg.avg_logprob,
                )
                for seg in part.segments
            )
        merged.sort(key=lambda s: (s.start, s.end))

        duration = max((s.end for s in merged), default=0.0)
        last = parts[-1]
        total_span = last.offset_sec + (last.meta.audio.duration_sec or 0.0)
        duration = max(duration, total_span)

        audio_ok = False
        if keep_audio:
            audio_ok = self._merge_audio(parts, primary_dir)

        source = _load_payload(primary_dir)
        write_transcript(
            TranscriptResult(
                segments=merged,
                language=source.get("language"),
                duration_sec=duration,
                provider=source.get("provider") or "",
                model=source.get("model") or "",
                extra={
                    **(source.get("extra") or {}),
                    "merged_from": [p.meta.session_id for p in parts],
                    "merged_gaps_sec": [[round(a, 2), round(b, 2)] for a, b in gaps],
                    "merged_audio": audio_ok,
                },
            ),
            primary_dir / "transcript",
            session_id=primary.meta.session_id,
            course_name=primary.meta.course.name,
            date=primary.meta.date,
            audio_start_iso=primary.meta.start_time,
        )

        # 旧的 REPAIRED / 分析产物是按合并前的时间轴算的，留着必然错位。
        # 删掉之后 autopilot 会在合并后的整段上重新跑一遍。
        _drop_stale_outputs(primary_dir)

        primary.meta.end_time = to_iso(_start_of(primary.meta) + timedelta(seconds=duration))
        primary.meta.audio.duration_sec = round(duration, 3)
        primary.meta.merged_from = [p.meta.session_id for p in rest]
        self.sessions.save(primary.meta)

        for part in rest:
            part.meta.merged_into = primary.meta.session_id
            self.sessions.save(part.meta)
            log.info("%s 已并入 %s", part.meta.session_id, primary.meta.session_id)

        outcome = MergeOutcome(
            primary_id=primary.meta.session_id,
            merged_ids=[p.meta.session_id for p in rest],
            segment_count=len(merged),
            duration_sec=duration,
            gaps=gaps,
            audio_merged=audio_ok,
        )
        log.info("合并完成：%s —— %s", outcome.primary_id, outcome.message)
        return outcome

    # ------------------------------------------------------------------ 音频

    def _merge_audio(self, parts: list[MergePart], primary_dir: Path) -> bool:
        """把各段的 16k wav 按时间轴拼起来，中断处填静音。

        用 concat demuxer + ``-c copy``：各段都是同一参数的 pcm_s16le，
        不需要重编码，一节课几百兆也就几秒钟。

        失败不抛异常 —— 转录已经合好了，音频没合上只是让 repair 用不了，
        比整个合并回滚要好。
        """
        wavs = [
            self.sessions.session_dir(p.meta.session_id) / "audio" / PROCESSED_NAME
            for p in parts
        ]
        missing = [w for w in wavs if not w.is_file()]
        if missing:
            log.warning("缺少已转码音频，跳过音频合并：%s", missing[0])
            return False

        tools = tools_from_config(self.config)
        work = primary_dir / "audio" / "_merge"
        work.mkdir(parents=True, exist_ok=True)
        try:
            entries: list[Path] = []
            for index, (part, wav) in enumerate(zip(parts, wavs)):
                if part.gap_before_sec:
                    silence = work / f"gap_{index:02d}.wav"
                    if not _make_silence(tools, silence, part.gap_before_sec, self.config):
                        return False
                    entries.append(silence)
                entries.append(wav)

            listing = work / "concat.txt"
            listing.write_text(
                "".join(f"file '{p.as_posix()}'\n" for p in entries), encoding="utf-8"
            )
            merged_wav = work / "merged.wav"
            if not _ffmpeg(tools, ["-f", "concat", "-safe", "0", "-i", str(listing),
                                   "-c", "copy", str(merged_wav)]):
                return False

            target = primary_dir / "audio" / PROCESSED_NAME
            backup = primary_dir / "audio" / f"{PROCESSED_NAME}.part1"
            if target.is_file() and not backup.is_file():
                target.replace(backup)      # 留一份原始第一段，合错了还能退回
            merged_wav.replace(target)
            return True
        except OSError as exc:
            log.warning("音频合并失败（转录已合并，仅 repair 受影响）：%s", exc)
            return False
        finally:
            shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------- 工具


def _start_of(meta: SessionMeta) -> datetime:
    parsed = datetime.fromisoformat(str(meta.start_time))
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _load_payload(session_dir: Path) -> dict:
    path = session_dir / "transcript" / TRANSCRIPT_JSON
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _load_segments(session_dir: Path) -> list[TranscriptSegment]:
    payload = _load_payload(session_dir)
    return [TranscriptSegment.from_dict(s) for s in payload.get("segments") or []]


def _drop_stale_outputs(session_dir: Path) -> None:
    """删掉按旧时间轴生成的产物。只删能自动重建的，绝不碰 transcript_raw。"""
    for name in ("transcript_repaired.json", "transcript_repaired.md"):
        (session_dir / "transcript" / name).unlink(missing_ok=True)
    shutil.rmtree(session_dir / "analysis", ignore_errors=True)


def _ffmpeg(tools, args: list[str]) -> bool:
    import subprocess

    cmd = [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=1800, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ffmpeg 调用失败：%s", exc)
        return False
    if result.returncode != 0:
        log.warning("ffmpeg 返回 %d：%s", result.returncode, (result.stderr or "")[:300])
        return False
    return True


def _make_silence(tools, target: Path, seconds: float, config: Config) -> bool:
    rate = config.audio.target_sample_rate
    layout = "mono" if config.audio.target_channels == 1 else "stereo"
    return _ffmpeg(tools, [
        "-f", "lavfi", "-t", f"{seconds:.3f}",
        "-i", f"anullsrc=r={rate}:cl={layout}",
        "-c:a", "pcm_s16le", str(target),
    ])


__all__ = ["SessionMerger", "MergeOutcome", "MergePart", "MIN_GAP_SEC", "MAX_GAP_SEC"]
