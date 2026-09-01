"""faster-whisper 实现 —— Phase 1 的主力 ASR。

本机是 Intel Core Ultra（无 CUDA），所以默认 device=cpu / compute_type=int8，
模型用 large-v3-turbo 而非 large-v3（后者在 CPU 上慢到不可用）。
换成 N 卡机器只需改 config.yaml，本文件无需改动。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from lecture_ai.errors import DependencyMissing, TranscriptionError
from lecture_ai.logging_setup import get_logger
from lecture_ai.transcription.base import (
    ProgressCallback,
    TranscribeOptions,
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
)

log = get_logger(__name__)

_INSTALL_HINT = (
    "未安装 faster-whisper。请运行：\n"
    '  pip install "lecture-ai[asr]"   或   pip install faster-whisper'
)

# 首次使用要从 HuggingFace 下载模型（turbo 约 1.6GB）。
# 国内网络经常卡在 LFS 大文件上：小文件能下、model.bin 停在几 MB 不动。
_DOWNLOAD_HINT = (
    "常见原因与对策：\n"
    "  1) 首次使用需要联网下载模型。若下载卡住不动（常见于国内网络），\n"
    "     设置镜像后重试：  set HF_ENDPOINT=https://hf-mirror.com\n"
    "     （也可写进项目根目录的 .env）\n"
    "  2) 也可以手动下载模型目录，放到 data/cache/models/ 下，\n"
    "     或把 config 的 model 直接指向本地目录的绝对路径。\n"
    "  3) device/compute_type 组合不被硬件支持"
    "（本机无 NVIDIA 显卡时必须是 device=cpu）。"
)


class FasterWhisperTranscriber(Transcriber):
    name = "local_whisper"

    def __init__(
        self,
        model: str = "large-v3-turbo",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        download_root: Path | None = None,
        default_language: str | None = None,
        default_beam_size: int = 5,
        default_vad_filter: bool = True,
        condition_on_previous_text: bool = False,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads or (os.cpu_count() or 4)
        self.download_root = download_root
        self.default_language = default_language
        self.default_beam_size = default_beam_size
        self.default_vad_filter = default_vad_filter
        self.condition_on_previous_text = condition_on_previous_text
        self._model = None  # 懒加载：只有真要转录时才付出加载模型的代价

    # ---------------------------------------------------------------- 模型

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise DependencyMissing(_INSTALL_HINT) from exc

        log.info(
            "加载 Whisper 模型：%s（device=%s, compute_type=%s, threads=%d）",
            self.model_name, self.device, self.compute_type, self.cpu_threads,
        )
        started = time.monotonic()
        try:
            self._model = WhisperModel(
                self.model_name,
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                download_root=str(self.download_root) if self.download_root else None,
            )
        except Exception as exc:  # ctranslate2 抛的异常类型很杂，统一包装
            raise TranscriptionError(
                f"加载模型 {self.model_name} 失败（device={self.device}, "
                f"compute_type={self.compute_type}）：{exc}\n"
                f"{_DOWNLOAD_HINT}"
            ) from exc
        log.info("模型加载完成，耗时 %.1f 秒", time.monotonic() - started)
        return self._model

    # ---------------------------------------------------------------- 转录

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> TranscriptResult:
        opts = options or TranscribeOptions()
        model = self._load()

        kwargs = {
            "beam_size": opts.beam_size or self.default_beam_size,
            "language": opts.language if opts.language is not None else self.default_language,
            "vad_filter": opts.vad_filter if options else self.default_vad_filter,
            "temperature": opts.temperature,
            "condition_on_previous_text": (
                opts.condition_on_previous_text and self.condition_on_previous_text
            ),
            "repetition_penalty": opts.repetition_penalty,
            "no_repeat_ngram_size": opts.no_repeat_ngram_size,
            "word_timestamps": False,
        }
        if opts.initial_prompt:
            kwargs["initial_prompt"] = opts.initial_prompt

        started = time.monotonic()
        try:
            # hotwords 是 faster-whisper 1.x 才有的参数，老版本会 TypeError
            if opts.hotwords:
                try:
                    seg_iter, info = model.transcribe(
                        str(audio_path), hotwords=opts.hotwords, **kwargs
                    )
                except TypeError:
                    log.warning("当前 faster-whisper 不支持 hotwords，改用 initial_prompt")
                    kwargs.setdefault("initial_prompt", opts.hotwords)
                    seg_iter, info = model.transcribe(str(audio_path), **kwargs)
            else:
                seg_iter, info = model.transcribe(str(audio_path), **kwargs)
        except Exception as exc:
            raise TranscriptionError(f"转录失败（{audio_path.name}）：{exc}") from exc

        total = float(getattr(info, "duration", 0.0) or 0.0)
        segments: list[TranscriptSegment] = []
        last_report = 0.0

        # generator 是惰性的，真正的解码发生在这个循环里
        try:
            for seg in seg_iter:
                text = (seg.text or "").strip()
                if text:
                    segments.append(
                        TranscriptSegment(
                            start=float(seg.start),
                            end=float(seg.end),
                            text=text,
                            no_speech_prob=getattr(seg, "no_speech_prob", None),
                            avg_logprob=getattr(seg, "avg_logprob", None),
                        )
                    )
                if progress and seg.end - last_report >= 30.0:
                    last_report = seg.end
                    progress(float(seg.end), total)
        except Exception as exc:
            raise TranscriptionError(f"转录过程中断（{audio_path.name}）：{exc}") from exc

        elapsed = time.monotonic() - started
        if progress and total:
            progress(total, total)

        speed = (total / elapsed) if elapsed > 0 else 0.0
        log.info(
            "转录完成：%d 段，音频 %.1f 分钟，耗时 %.1f 分钟（%.2fx 实时）",
            len(segments), total / 60, elapsed / 60, speed,
        )

        return TranscriptResult(
            segments=segments,
            language=getattr(info, "language", None),
            duration_sec=total,
            provider=self.name,
            model=self.model_name,
            extra={
                "language_probability": getattr(info, "language_probability", None),
                "device": self.device,
                "compute_type": self.compute_type,
                "elapsed_sec": round(elapsed, 2),
                "realtime_factor": round(speed, 3),
            },
        )

    def close(self) -> None:
        self._model = None
