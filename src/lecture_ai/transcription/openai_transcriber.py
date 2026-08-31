"""云端 ASR（OpenAI Whisper API）。

默认不启用。启用需要同时满足：
  1. config.transcription.provider = openai
  2. config.privacy.allow_cloud_audio = true   <- 硬闸门，在 registry 里检查
  3. 环境变量 OPENAI_API_KEY 已设置

课堂录音是私人学习资料，默认本地处理。上云必须是用户的显式选择。
"""

from __future__ import annotations

import os
from pathlib import Path

from lecture_ai.errors import ConfigError, DependencyMissing, TranscriptionError
from lecture_ai.logging_setup import get_logger
from lecture_ai.transcription.base import (
    ProgressCallback,
    TranscribeOptions,
    Transcriber,
    TranscriptResult,
    TranscriptSegment,
)

log = get_logger(__name__)

#: OpenAI 音频接口的单文件上限是 25MB
_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class OpenAITranscriber(Transcriber):
    name = "openai"

    def __init__(self, model: str = "whisper-1") -> None:
        self.model_name = model
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ConfigError(
                "使用云端 ASR 需要设置环境变量 OPENAI_API_KEY（写进项目根目录的 .env）"
            )
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise DependencyMissing(
                '未安装 openai 包。请运行：pip install "lecture-ai[cloud]"'
            ) from exc
        self._client = OpenAI(api_key=api_key)
        return self._client

    def transcribe(
        self,
        audio_path: Path,
        options: TranscribeOptions | None = None,
        progress: ProgressCallback | None = None,
    ) -> TranscriptResult:
        opts = options or TranscribeOptions()
        client = self._get_client()

        size = audio_path.stat().st_size
        if size > _MAX_UPLOAD_BYTES:
            raise TranscriptionError(
                f"文件 {audio_path.name} 为 {size / 1e6:.1f} MB，超过云端接口 25 MB 上限。"
                "请在 config 中开启 audio.chunking 后重试，或改用本地 ASR。"
            )

        log.info("上传云端转录：%s（%.1f MB）", audio_path.name, size / 1e6)
        try:
            with open(audio_path, "rb") as f:
                resp = client.audio.transcriptions.create(
                    model=self.model_name,
                    file=f,
                    response_format="verbose_json",
                    timestamp_granularities=["segment"],
                    language=opts.language or None,
                    prompt=opts.initial_prompt or opts.hotwords or None,
                )
        except Exception as exc:
            raise TranscriptionError(f"云端转录失败：{exc}") from exc

        raw_segments = getattr(resp, "segments", None) or []
        segments = [
            TranscriptSegment(
                start=float(_attr(s, "start", 0.0)),
                end=float(_attr(s, "end", 0.0)),
                text=str(_attr(s, "text", "")).strip(),
                no_speech_prob=_attr(s, "no_speech_prob", None),
                avg_logprob=_attr(s, "avg_logprob", None),
            )
            for s in raw_segments
            if str(_attr(s, "text", "")).strip()
        ]
        if not segments:
            # 极少数情况接口只回纯文本；不能丢数据，但要明确标注时间戳不可靠
            text = str(getattr(resp, "text", "") or "").strip()
            if text:
                log.warning("云端返回无分段结果，时间戳不可用")
                segments = [TranscriptSegment(start=0.0, end=0.0, text=text)]

        duration = float(getattr(resp, "duration", 0.0) or 0.0)
        if progress and duration:
            progress(duration, duration)

        return TranscriptResult(
            segments=segments,
            language=getattr(resp, "language", None),
            duration_sec=duration or None,
            provider=self.name,
            model=self.model_name,
            extra={"cloud": True},
        )


def _attr(obj, name: str, default=None):
    """兼容 SDK 返回对象与 dict 两种形态。"""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)
