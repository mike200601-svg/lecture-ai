"""Phase 1 编排：incoming 音频 -> Session -> ASR -> 带时间戳 transcript。

设计红线（对应总 Prompt 第十三条「可恢复设计」）：
  - 每一步先查缓存，已完成就复用；
  - 任何一步失败，之前步骤的产物必须保留；
  - **retry 绝不重跑已经成功的 ASR** —— 那是最贵的一步。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from lecture_ai.audio import PROCESSED_NAME, preprocess_audio, probe_audio
from lecture_ai.audio.preprocess import tools_from_config
from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.errors import AudioError, IngestError, LectureAIError
from lecture_ai.ingestion.scanner import AudioScanner, DiscoveredFile, guess_start_time
from lecture_ai.logging_setup import attach_session_log, detach_session_log, get_logger
from lecture_ai.pipeline.progress import ProgressWriter
from lecture_ai.session import (
    PHASE1_DONE_STATES,
    SessionManager,
    SessionMeta,
    SessionState,
    load_courses,
)
from lecture_ai.transcription import (
    TranscribeOptions,
    build_transcriber,
    load_glossary,
    write_transcript,
)
from lecture_ai.transcription.base import merge_chunk_results
from lecture_ai.transcription.writer import (
    TRANSCRIPT_JSON,
    is_valid_transcript,
)
from lecture_ai.utils.paths import rel_to, safe_move
from lecture_ai.utils.timefmt import hhmmss, to_iso

log = get_logger(__name__)

STEP_INGEST = "ingest"
STEP_PREPROCESS = "preprocess"
STEP_TRANSCRIBE = "transcribe"

# ingest 失败后的重试退避（秒）。ingest 失败不建 session，没有任何状态记住
# 这个文件失败过，不退避的话 watch 会每个 poll_interval 起两个 ffmpeg 硬撞
# 同一个文件，直到天荒地老。
# 分两档，因为这是两种完全不同的失败：
#
# - **真错误**（文件损坏、ffmpeg 不见了）：等多久都不会自己好，退到半小时一次，
#   只为留一条「你回来看一眼」的日志。
# - **还没写完**（见 _INCOMPLETE_MARKERS）：是会自己好的暂态，而且重试成本极低
#   —— ffprobe 找不到 moov 会立刻失败，根本不解码。封顶必须小。
#
# 2026-09-08 的教训：这两档一开始是合并的，一律顶到 1800 秒。当天上午那节课在
# 11:56–12:26 之间同步完成，却因为退避窗口一直等到 12:26 才被发现，白等了半小时
# —— 比转录本身（46 分钟）的一半还多。
_INGEST_BACKOFF_SEC = [30, 60, 120, 300, 600, 1800]
_INCOMPLETE_BACKOFF_SEC = [15, 30, 60]

# 「还在录 / 还没同步完」的特征：m4a 的 moov atom 写在文件末尾，录音机没收尾
# 就没有它。这不是错误，是预期状态，日志降到 info，别刷屏。
_INCOMPLETE_MARKERS = ("moov atom not found", "Invalid data found when processing input")


@dataclass
class ProcessOutcome:
    session_id: str
    state: SessionState
    ok: bool
    message: str = ""
    elapsed_sec: float = 0.0


class Phase1Pipeline:
    def __init__(self, config: Config, db: Database | None = None, *,
                 one_shot: bool = False) -> None:
        self.config = config
        self.db = db or Database(config.paths.database)
        self.sessions = SessionManager(config, self.db)
        self.courses = load_courses(config.courses_path, config.course.default_course_key)
        # one_shot：命令跑完就退出，扫描时需要就地补采样（详见 scanner.is_stable）
        self.scanner = AudioScanner(config, self.db, one_shot=one_shot)
        self._transcriber = None  # 懒加载并在多个 session 间复用，避免反复加载模型
        # path -> (失败次数, 下次可重试的时间戳)。只在进程内存活，重启即清空。
        # path -> (失败次数, 下次可重试的时间戳, 上次看到的 (size, mtime))
        # 只在进程内存活，重启即清空。
        self._ingest_backoff: dict[Path, tuple[int, float, tuple[int, float] | None]] = {}

    # ------------------------------------------------------------------ ingest

    def ingest_new_audio(self) -> list[SessionMeta]:
        """扫描 incoming，为每个新音频建立 session。"""
        discovered = self.scanner.scan()
        created: list[SessionMeta] = []
        now = time.time()
        for item in discovered:
            if self._ingest_deferred(item.path, now):
                continue
            try:
                meta = self.ingest_file(item)
                if meta is not None:
                    created.append(meta)
            except LectureAIError as exc:
                # 单个文件失败不能拖垮整轮扫描
                self._note_ingest_failure(item.path, exc)
            else:
                self._ingest_backoff.pop(item.path, None)
        return created

    @staticmethod
    def _file_signature(path: Path) -> tuple[int, float] | None:
        try:
            st = path.stat()
        except OSError:
            return None
        return (st.st_size, st.st_mtime)

    def _ingest_deferred(self, path: Path, now: float) -> bool:
        """这个文件还在退避窗口里就跳过，避免每轮都去起 ffmpeg 硬撞。

        但只要文件本身变过（Syncthing 又写进来一截），就立刻清空退避重新试 ——
        「变了」是进展的证据，没有理由继续罚站。
        """
        entry = self._ingest_backoff.get(path)
        if entry is None:
            return False
        _fails, next_at, signature = entry
        if self._file_signature(path) != signature:
            del self._ingest_backoff[path]
            return False
        return now < next_at

    def _note_ingest_failure(self, path: Path, exc: LectureAIError) -> None:
        text = str(exc)
        incomplete = any(m in text for m in _INCOMPLETE_MARKERS)
        table = _INCOMPLETE_BACKOFF_SEC if incomplete else _INGEST_BACKOFF_SEC

        fails = self._ingest_backoff.get(path, (0, 0.0, None))[0] + 1
        delay = table[min(fails, len(table)) - 1]
        self._ingest_backoff[path] = (
            fails, time.time() + delay, self._file_signature(path),
        )

        if incomplete:
            # 录音还没收尾，等就是了。不刷 ERROR，也不打整段 ffmpeg 输出。
            log.info("%s 尚未写完（第 %d 次），%d 秒后再试", path.name, fails, delay)
        else:
            log.error("处理文件失败：%s（%s），%d 秒后重试", path.name, exc, delay)

    def ingest_file(self, item: DiscoveredFile) -> SessionMeta | None:
        """把一个音频文件收进新 session。"""
        tools = tools_from_config(self.config)
        try:
            probe = probe_audio(item.path, tools)
        except AudioError as exc:
            raise IngestError(f"无法解析音频 {item.path.name}：{exc}") from exc

        min_sec = self.config.processing.min_audio_seconds
        if probe.duration_sec < min_sec:
            log.warning(
                "音频 %s 仅 %.0f 秒（阈值 %d 秒），疑似误触录音，跳过",
                item.path.name, probe.duration_sec, min_sec,
            )
            return None

        guess = guess_start_time(
            item.path, duration_sec=probe.duration_sec, creation_time=probe.creation_time
        )
        course = self.courses.match(guess.dt, self.config.course.match_tolerance_minutes)
        log.info(
            "发现录音 %s：时长 %s，起始 %s（来源 %s / %s 置信），匹配课程「%s」",
            item.path.name, hhmmss(probe.duration_sec),
            guess.dt.strftime("%Y-%m-%d %H:%M"), guess.source, guess.confidence, course.name,
        )

        meta = self.sessions.create(
            course,
            guess.dt,
            end_time=guess.dt + timedelta(seconds=probe.duration_sec),
            start_time_source=guess.source,
            start_time_confidence=guess.confidence,
        )
        session_dir = self.sessions.session_dir(meta.session_id)

        started = time.monotonic()
        self.sessions.mark_step(meta, STEP_INGEST, "running")
        try:
            archived = safe_move(
                item.path, session_dir / "raw", copy=self.config.processing.keep_incoming
            )
        except OSError as exc:
            self.sessions.fail(meta, f"归档音频失败：{exc}")
            raise IngestError(f"归档音频失败：{exc}") from exc

        meta.audio.raw = rel_to(archived, session_dir)
        meta.audio.orig_name = item.path.name
        meta.audio.sha256 = item.sha256
        meta.audio.duration_sec = round(probe.duration_sec, 3)

        self.db.insert_file(
            sha256=item.sha256,
            path=str(archived),
            file_type="audio",
            size=item.size,
            orig_name=item.path.name,
            timestamp=to_iso(guess.dt),
            session_id=meta.session_id,
        )
        self.sessions.mark_step(
            meta, STEP_INGEST, "done", elapsed_sec=time.monotonic() - started
        )
        self.sessions.transition(meta, SessionState.AUDIO_READY)
        return meta

    # ------------------------------------------------------------------ process

    def process_session(
        self, session_id: str, *, force: tuple[str, ...] = ()
    ) -> ProcessOutcome:
        """推进单个 session 到 TRANSCRIBED。可重复调用（幂等）。"""
        meta = self.sessions.load(session_id)
        session_dir = self.sessions.session_dir(session_id)
        handler = attach_session_log(session_dir, session_id)
        slog = get_logger(__name__, session_id)
        started = time.monotonic()

        try:
            if meta.state == SessionState.FAILED:
                self.sessions.clear_failure(meta)

            if meta.state == SessionState.NEW:
                raise IngestError(
                    f"session {session_id} 尚未关联音频（状态 NEW），无法处理"
                )

            # ---------------- 1. 预处理
            self._run_preprocess(meta, session_dir, slog, force=STEP_PREPROCESS in force)

            # ---------------- 2. 转录
            reused = self._run_transcribe(
                meta, session_dir, slog, force=STEP_TRANSCRIBE in force
            )

            elapsed = time.monotonic() - started
            msg = "复用已有转录" if reused else "转录完成"
            slog.info("session 处理完成（%s，耗时 %.1f 秒）", msg, elapsed)
            return ProcessOutcome(session_id, meta.state, True, msg, elapsed)

        except LectureAIError as exc:
            self.sessions.fail(meta, str(exc))
            return ProcessOutcome(
                session_id, SessionState.FAILED, False, str(exc), time.monotonic() - started
            )
        finally:
            detach_session_log(handler)

    def _run_preprocess(self, meta: SessionMeta, session_dir: Path, slog, *, force: bool) -> None:
        if not meta.audio.raw:
            raise IngestError(f"session {meta.session_id} 的 metadata 中没有原始音频路径")
        raw_path = _resolve(session_dir, meta.audio.raw)
        if not raw_path.exists():
            raise IngestError(f"原始音频不存在：{raw_path}")

        step = meta.step(STEP_PREPROCESS)
        processed = session_dir / "audio" / PROCESSED_NAME
        if not force and step.status == "done" and processed.exists():
            slog.debug("预处理已完成，跳过")
            return

        started = time.monotonic()
        self.sessions.mark_step(meta, STEP_PREPROCESS, "running")
        try:
            result = preprocess_audio(
                raw_path, session_dir, self.config,
                force=force, session_id=meta.session_id,
            )
        except LectureAIError as exc:
            self.sessions.mark_step(meta, STEP_PREPROCESS, "failed", error=str(exc))
            raise

        meta.audio.processed = rel_to(result.processed_path, session_dir)
        if not meta.audio.duration_sec:
            meta.audio.duration_sec = round(result.probe.duration_sec, 3)
        self.sessions.mark_step(
            meta, STEP_PREPROCESS, "done", elapsed_sec=time.monotonic() - started
        )

    def _run_transcribe(
        self, meta: SessionMeta, session_dir: Path, slog, *, force: bool
    ) -> bool:
        """执行转录。返回是否复用了已有结果。"""
        json_path = session_dir / "transcript" / TRANSCRIPT_JSON

        # 缓存优先：ASR 是最贵的一步，只要有合法结果就绝不重跑
        if not force and is_valid_transcript(json_path):
            slog.info("已存在合法转录结果，跳过 ASR：%s", json_path.name)
            if meta.state == SessionState.AUDIO_READY:
                self.sessions.transition(meta, SessionState.TRANSCRIBING)
            # 只把「还没到 TRANSCRIBED」的 session 往前推。
            # Phase 3 之后 session 会走到 IMAGES_READY 等更靠后的状态，
            # 那时再往回设 TRANSCRIBED 属于非法倒退。
            if meta.state not in PHASE1_DONE_STATES:
                self.sessions.transition(meta, SessionState.TRANSCRIBED)
            if meta.step(STEP_TRANSCRIBE).status != "done":
                self.sessions.mark_step(meta, STEP_TRANSCRIBE, "done")
            return True

        audio_path = _resolve(session_dir, meta.audio.processed or f"audio/{PROCESSED_NAME}")
        if not audio_path.exists():
            raise AudioError(f"预处理音频不存在：{audio_path}")

        transcriber = self._get_transcriber()
        course = self.courses.get(meta.course.key)
        # ASR 只用课程专属词表。真实录音 A/B 证明，跨学科 common
        # hotwords 会在弱语音段制造高置信幻觉（例如数字电路课出现哈密顿量）。
        glossary = load_glossary(
            self.config.glossary_dir, course.glossary, include_common=False
        )
        lw = self.config.transcription.local_whisper
        hotwords = glossary.as_hotwords() if lw.use_hotwords else None
        options = TranscribeOptions(
            language=lw.language,
            hotwords=hotwords,
            initial_prompt=None,
            vad_filter=lw.vad_filter,
            beam_size=lw.beam_size,
            condition_on_previous_text=lw.condition_on_previous_text,
            repetition_penalty=lw.repetition_penalty,
            no_repeat_ngram_size=lw.no_repeat_ngram_size,
        )
        if hotwords:
            slog.info("已载入 %d 条专业术语用于 ASR 提示", len(glossary))
        elif glossary.terms:
            slog.info(
                "已载入 %d 条专业术语供下游纠错；ASR hotwords 已按实录 A/B 关闭",
                len(glossary),
            )

        if meta.state == SessionState.AUDIO_READY:
            self.sessions.transition(meta, SessionState.TRANSCRIBING)

        started = time.monotonic()
        self.sessions.mark_step(
            meta, STEP_TRANSCRIBE, "running",
            provider=transcriber.name, model=transcriber.model_name,
        )

        progress = ProgressWriter(session_dir)
        progress.update(0.0, meta.audio.duration_sec or 0.0, force=True)

        def on_progress(done: float, total: float) -> None:
            if total > 0:
                slog.info("转录进度 %s / %s（%.0f%%）",
                          hhmmss(done), hhmmss(total), done / total * 100)
            progress.update(done, total)

        try:
            chunk_dir = session_dir / "audio" / "chunks"
            chunk_files = sorted(chunk_dir.glob("chunk_*.wav")) if chunk_dir.exists() else []
            if chunk_files:
                result = self._transcribe_chunks(
                    transcriber, chunk_files, options, on_progress, slog,
                    total_sec=meta.audio.duration_sec or 0.0,
                )
            else:
                result = transcriber.transcribe(audio_path, options, on_progress)
        except LectureAIError as exc:
            self.sessions.mark_step(meta, STEP_TRANSCRIBE, "failed", error=str(exc))
            raise
        finally:
            # 成功路径下面还要写 transcript，进度文件到这里就没用了；
            # 留着只会让面板显示一个停在 100% 的残影。
            progress.clear()

        if not result.segments:
            raise AudioError(
                "转录结果为空。可能原因：录音无人声、音量过低、或 VAD 过滤过强。"
            )

        write_transcript(
            result,
            session_dir / "transcript",
            session_id=meta.session_id,
            course_name=meta.course.name,
            date=meta.date,
            audio_start_iso=meta.start_time,
        )
        elapsed = time.monotonic() - started
        self.sessions.mark_step(
            meta, STEP_TRANSCRIBE, "done", elapsed_sec=elapsed,
            provider=result.provider, model=result.model,
        )
        self.sessions.transition(meta, SessionState.TRANSCRIBED)
        slog.info("转录产出 %d 个片段，耗时 %.1f 分钟", len(result.segments), elapsed / 60)
        return False

    def _transcribe_chunks(self, transcriber, chunk_files, options, on_progress, slog,
                           *, total_sec: float = 0.0):
        """切片模式：逐片转录后按偏移合并时间轴。"""
        chunk_sec = self.config.audio.chunking.chunk_minutes * 60
        overlap = self.config.audio.chunking.overlap_seconds
        step = max(1, chunk_sec - overlap)
        slog.info("使用切片模式转录，共 %d 片", len(chunk_files))
        results = []
        for i, chunk in enumerate(chunk_files):
            slog.info("转录切片 %d/%d：%s", i + 1, len(chunk_files), chunk.name)
            offset = i * step
            # 每片的进度都是从 0 开始的，直接往上报会让整体进度每片归零一次。
            # 加上片偏移、并把总长换成整段录音的时长，才是「整节课的进度」。
            def chunk_progress(done: float, total: float, _off: float = offset) -> None:
                on_progress(_off + done, total_sec or (len(chunk_files) * step))

            results.append((transcriber.transcribe(chunk, options, chunk_progress), offset))
        return merge_chunk_results(results, overlap_sec=overlap)

    def _get_transcriber(self):
        if self._transcriber is None:
            self._transcriber = build_transcriber(self.config)
        return self._transcriber

    # ------------------------------------------------------------------ 批量

    def process_pending(self) -> list[ProcessOutcome]:
        """处理所有未完成的 session（AUDIO_READY / TRANSCRIBING）。

        刻意不包含 FAILED：watch 是长驻进程，自动重试会让一个永久性错误
        （比如模型文件损坏）变成无限循环刷日志。失败的 session 交给用户
        显式 `lecture-ai retry`。
        """
        outcomes: list[ProcessOutcome] = []
        for session_id in self.sessions.list_ids():
            try:
                meta = self.sessions.load(session_id)
            except LectureAIError as exc:
                log.warning("跳过 %s：%s", session_id, exc)
                continue
            if meta.state in (SessionState.AUDIO_READY, SessionState.TRANSCRIBING):
                outcomes.append(self.process_session(session_id))
        return outcomes

    def run_once(self) -> list[ProcessOutcome]:
        """watch 的单轮动作：扫描 + 处理。"""
        self.ingest_new_audio()
        if not self.config.processing.auto_process:
            return []
        return self.process_pending()

    def close(self) -> None:
        if self._transcriber is not None:
            self._transcriber.close()
            self._transcriber = None


def _resolve(session_dir: Path, rel_or_abs: str) -> Path:
    p = Path(rel_or_abs)
    return p if p.is_absolute() else session_dir / p
