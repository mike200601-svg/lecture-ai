"""Transcriber 工厂。上层代码构造 ASR 的唯一入口。

这里是隐私硬闸门的位置：privacy.allow_cloud_audio 为 false 时，
任何云端 provider 都会被直接拒绝，而不是「警告后照样上传」。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from lecture_ai.config import Config
from lecture_ai.errors import ConfigError
from lecture_ai.transcription.base import Transcriber

#: 会把音频发往外部服务的 provider
CLOUD_PROVIDERS = {"openai"}

KNOWN_PROVIDERS = {"local_whisper", "openai", "fake"}

LOCAL_MODEL_REQUIRED_FILES = (
    "model.bin",
    "config.json",
    "tokenizer.json",
    "vocabulary.txt",
)


@dataclass(frozen=True)
class ModelCacheStatus:
    model: str
    state: str  # ready | partial | missing
    path: Path
    source: str  # local | cache
    size_bytes: int = 0
    missing_files: tuple[str, ...] = ()


def build_transcriber(config: Config) -> Transcriber:
    provider = (config.transcription.provider or "local_whisper").strip().lower()

    if provider not in KNOWN_PROVIDERS:
        raise ConfigError(
            f"未知的 transcription.provider：{provider!r}。"
            f"可选：{', '.join(sorted(KNOWN_PROVIDERS))}"
        )

    if provider in CLOUD_PROVIDERS and not config.privacy.allow_cloud_audio:
        raise ConfigError(
            f"provider={provider} 会把课堂录音上传到云端，"
            "但 privacy.allow_cloud_audio 为 false。\n"
            "若确实要用云端 ASR，请在 config.yaml 中显式设置 "
            "privacy.allow_cloud_audio: true。"
        )

    if provider == "local_whisper":
        lw = config.transcription.local_whisper
        from lecture_ai.transcription.faster_whisper_transcriber import (
            FasterWhisperTranscriber,
        )

        return FasterWhisperTranscriber(
            model=resolve_model_reference(lw.model, config.paths.project_root),
            device=_resolve_device(lw.device),
            compute_type=lw.compute_type,
            cpu_threads=lw.cpu_threads,
            download_root=config.paths.cache_dir / "models",
            default_language=lw.language,
            default_beam_size=lw.beam_size,
            default_vad_filter=lw.vad_filter,
            condition_on_previous_text=lw.condition_on_previous_text,
        )

    if provider == "openai":
        from lecture_ai.transcription.openai_transcriber import OpenAITranscriber

        return OpenAITranscriber(model=config.transcription.openai.model)

    from lecture_ai.transcription.fake import FakeTranscriber

    return FakeTranscriber()


def resolve_model_reference(model: str, project_root: Path) -> str:
    """把项目内相对模型目录解析成绝对路径；HF 模型名保持原样。"""
    candidate = Path(model).expanduser()
    if candidate.is_absolute():
        return str(candidate.resolve())
    project_local = (Path(project_root) / candidate).resolve()
    return str(project_local) if project_local.exists() else model


def inspect_model_cache(model: str, cache_dir) -> ModelCacheStatus:
    """检查模型在项目统一缓存中的状态，不触发网络访问。"""
    local = Path(model).expanduser()
    if local.is_absolute():
        if not local.exists():
            return ModelCacheStatus(model, "missing", local, "local")
        missing = tuple(name for name in LOCAL_MODEL_REQUIRED_FILES if not (local / name).is_file())
        state = "ready" if local.is_dir() and not missing else "partial"
        return ModelCacheStatus(
            model,
            state,
            local,
            "local",
            _tree_size(local),
            missing,
        )

    root = Path(cache_dir) / "models"
    matches = sorted(root.glob(f"models--*--faster-whisper-{model}")) if root.exists() else []
    if not matches:
        return ModelCacheStatus(model, "missing", root, "cache")

    repo_dir = matches[0]
    size = _tree_size(repo_dir)
    state = "ready" if any(repo_dir.rglob("model.bin")) else "partial"
    return ModelCacheStatus(model, state, repo_dir, "cache", size)


def validate_local_model(
    model_dir: Path,
    *,
    device: str,
    compute_type: str,
    cpu_threads: int = 0,
) -> float:
    """实际初始化本地 CTranslate2 模型，成功时返回加载秒数。"""
    from faster_whisper import WhisperModel

    started = time.monotonic()
    model = WhisperModel(
        str(model_dir),
        device=_resolve_device(device),
        compute_type=compute_type,
        cpu_threads=cpu_threads or (os.cpu_count() or 4),
    )
    elapsed = time.monotonic() - started
    del model
    return elapsed


def find_cached_model(model: str, cache_dir) -> Path | None:
    """查配置里的模型是否已经下载到本地。

    首次转录才发现模型没下下来（尤其是网络受限时会卡住十几分钟），
    体验很差 —— doctor 要能提前告诉用户。
    """
    status = inspect_model_cache(model, cache_dir)
    return status.path if status.state == "ready" else None


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                continue
    return total


def _resolve_device(device: str) -> str:
    """device: auto 时探测 CUDA，探测不到就退回 cpu。"""
    if device != "auto":
        return device
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0:
            return "cuda"
    except (ImportError, AttributeError, RuntimeError):
        pass
    return "cpu"
