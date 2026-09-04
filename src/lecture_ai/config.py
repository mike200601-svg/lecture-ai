"""配置加载：config/config.yaml + .env -> 类型化配置对象。

设计要点：
  1. 单一入口 load_config()，其他模块不许自己读 yaml；
  2. 路径全部解析成绝对 Path（相对路径以项目根为基准）；
  3. ${ENV_VAR} 插值；
  4. API key 只从环境变量读 —— yaml 里出现疑似密钥直接报错，防止误提交；
  5. 字段缺失一律走默认值，不因为配置不全而崩溃。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lecture_ai.errors import ConfigError

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# yaml 里出现这些键名即视为密钥泄漏风险
_SECRET_KEYS = {"api_key", "apikey", "secret", "token", "password", "access_key"}


# --------------------------------------------------------------------------- 子配置


@dataclass
class PathsConfig:
    project_root: Path
    incoming_audio: Path
    incoming_images: Path
    web_exchange: Path
    export_dir: Path
    session_dir: Path
    processed_dir: Path
    cache_dir: Path
    log_dir: Path
    database: Path
    obsidian_vault: Path | None = None


@dataclass
class LocalWhisperConfig:
    model: str = "large-v3-turbo"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: int = 0
    beam_size: int = 5
    vad_filter: bool = True
    language: str | None = "zh"
    use_hotwords: bool = False
    condition_on_previous_text: bool = False
    repetition_penalty: float = 1.1
    no_repeat_ngram_size: int = 3


@dataclass
class OpenAIASRConfig:
    model: str = "whisper-1"


@dataclass
class TranscriptionConfig:
    provider: str = "local_whisper"
    local_whisper: LocalWhisperConfig = field(default_factory=LocalWhisperConfig)
    openai: OpenAIASRConfig = field(default_factory=OpenAIASRConfig)


@dataclass
class ChunkingConfig:
    enabled: bool = False
    chunk_minutes: int = 30
    overlap_seconds: int = 5
    auto_threshold_minutes: int = 180


@dataclass
class AudioConfig:
    ffmpeg_path: str = ""
    ffprobe_path: str = ""
    target_sample_rate: int = 16000
    target_channels: int = 1
    normalize: bool = False
    extensions: tuple[str, ...] = (".mp3", ".m4a", ".wav", ".flac", ".aac", ".ogg", ".opus")
    chunking: ChunkingConfig = field(default_factory=ChunkingConfig)


@dataclass
class ProcessingConfig:
    auto_process: bool = True
    auto_advance_phase2: bool = False
    poll_interval: int = 15
    stable_checks: int = 2
    quiet_seconds: int = 10
    keep_incoming: bool = False
    min_audio_seconds: int = 60


@dataclass
class RepairConfig:
    """Phase 1.5 选择性重转录。阈值均可由 config.yaml 调整。"""

    padding_seconds: float = 15.0
    min_text_bytes: int = 60
    compression_ratio_threshold: float = 2.6
    unique_char_ratio_threshold: float = 0.16
    repeated_ngram_ratio_threshold: float = 0.55
    longest_run_threshold: int = 6
    sparse_segment_min_seconds: float = 45.0
    sparse_segment_max_chars_per_second: float = 0.60
    prompt_echo_min_terms: int = 5
    prompt_echo_min_coverage: float = 0.55
    min_improvement_ratio: float = 0.20
    min_length_ratio: float = 0.05


@dataclass
class CleanConfig:
    """Phase 2A 忠实清洗的分块、缓存与重试参数。"""

    chunk_minutes: int = 8
    overlap_seconds: int = 30
    max_retries: int = 2
    retry_base_seconds: float = 1.0
    min_retention_ratio: float = 0.45
    max_expansion_ratio: float = 1.35
    cross_segment_repetition_threshold: int = 6


@dataclass
class CourseMatchConfig:
    match_tolerance_minutes: int = 30
    default_course_key: str = "unknown"


@dataclass
class LLMConfig:
    provider: str = "chatgpt_web"
    model: str = "chatgpt-web-high"
    max_tokens: int = 8000
    temperature: float = 0.2


@dataclass
class VisionConfig:
    provider: str = "anthropic"
    model: str = "claude-sonnet-5"


@dataclass
class NoteConfig:
    """API 路线成稿（`lecture-ai note`）。

    整篇课堂笔记动辄三五万字，输出上限必须比分块处理的其他步骤大得多 ——
    沿用 llm.max_tokens（默认 8000）会让成稿被静默截断。
    """

    max_output_tokens: int = 32000
    temperature: float = 0.3


@dataclass
class ObsidianConfig:
    create_concepts: bool = False
    concept_threshold: float = 0.8


@dataclass
class PrivacyConfig:
    """隐私开关是硬闸门，不是建议 —— 见 transcription/registry.py。"""

    allow_cloud_audio: bool = False
    allow_cloud_images: bool = False
    allow_cloud_transcript: bool = True


@dataclass
class LoggingConfig:
    level: str = "INFO"
    console_level: str = "INFO"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


@dataclass
class Config:
    paths: PathsConfig
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    repair: RepairConfig = field(default_factory=RepairConfig)
    clean: CleanConfig = field(default_factory=CleanConfig)
    course: CourseMatchConfig = field(default_factory=CourseMatchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    note: NoteConfig = field(default_factory=NoteConfig)
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    privacy: PrivacyConfig = field(default_factory=PrivacyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    config_path: Path | None = None

    @property
    def courses_path(self) -> Path:
        """courses.yaml 与 config.yaml 同目录。"""
        base = self.config_path.parent if self.config_path else self.paths.project_root / "config"
        return base / "courses.yaml"

    @property
    def glossary_dir(self) -> Path:
        base = self.config_path.parent if self.config_path else self.paths.project_root / "config"
        return base / "glossary"

    def ensure_dirs(self) -> None:
        """创建所有工作目录。幂等。"""
        for p in (
            self.paths.incoming_audio,
            self.paths.incoming_images,
            self.paths.web_exchange,
            self.paths.export_dir,
            self.paths.session_dir,
            self.paths.processed_dir,
            self.paths.cache_dir,
            self.paths.log_dir,
            self.paths.database.parent,
        ):
            p.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- 加载


def find_project_root(start: Path | None = None) -> Path:
    """向上查找含 pyproject.toml 或 config/config.yaml 的目录。"""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "pyproject.toml").exists() or (candidate / "config" / "config.yaml").exists():
            return candidate
    # 找不到就退回包所在位置的上两级（src/lecture_ai -> src -> root）
    return Path(__file__).resolve().parents[2]


def _interpolate(value: Any) -> Any:
    """递归展开 ${ENV_VAR}。未定义的变量替换为空串，不抛错。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _assert_no_secrets(data: Any, path: str = "") -> None:
    """扫描 yaml，发现疑似密钥就报错。

    只在「键名像密钥 且 值非空」时报错 —— 允许写 `api_key: ""` 之类的占位注释。
    """
    if isinstance(data, dict):
        for k, v in data.items():
            here = f"{path}.{k}" if path else str(k)
            if str(k).lower() in _SECRET_KEYS and isinstance(v, str) and v.strip():
                raise ConfigError(
                    f"config.yaml 的 `{here}` 含有非空密钥值。"
                    "API key 必须放在 .env 中，绝不能写进配置文件。"
                )
            _assert_no_secrets(v, here)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            _assert_no_secrets(v, f"{path}[{i}]")


def _resolve_path(value: str, root: Path) -> Path:
    p = Path(value).expanduser()
    return p if p.is_absolute() else (root / p)


def _sub(data: dict, key: str) -> dict:
    """取子字典，缺失或类型不对时返回空字典。"""
    v = data.get(key)
    return v if isinstance(v, dict) else {}


def _dc(cls, data: dict, **overrides):
    """用字典里存在的字段构造 dataclass，未知字段忽略，缺失字段用默认值。"""
    valid = {f.name for f in cls.__dataclass_fields__.values()}
    kwargs = {k: v for k, v in data.items() if k in valid}
    kwargs.update(overrides)
    return cls(**kwargs)


def load_config(config_path: Path | None = None, project_root: Path | None = None) -> Config:
    """加载配置。config_path 为空时使用 <project_root>/config/config.yaml。"""
    root = (project_root or find_project_root()).resolve()
    cfg_path = Path(config_path).resolve() if config_path else root / "config" / "config.yaml"

    _load_dotenv(root)

    raw: dict = {}
    if cfg_path.exists():
        try:
            with open(cfg_path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"config.yaml 解析失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"config.yaml 顶层必须是映射，实际是 {type(raw).__name__}")

    _assert_no_secrets(raw)
    raw = _interpolate(raw)

    p = _sub(raw, "paths")
    vault_raw = str(p.get("obsidian_vault") or "").strip()
    paths = PathsConfig(
        project_root=root,
        incoming_audio=_resolve_path(p.get("incoming_audio", "data/incoming/audio"), root),
        incoming_images=_resolve_path(p.get("incoming_images", "data/incoming/images"), root),
        web_exchange=_resolve_path(p.get("web_exchange", "data/web_exchange"), root),
        export_dir=_resolve_path(p.get("export_dir", "exports"), root),
        session_dir=_resolve_path(p.get("session_dir", "data/sessions"), root),
        processed_dir=_resolve_path(p.get("processed_dir", "data/processed"), root),
        cache_dir=_resolve_path(p.get("cache_dir", "data/cache"), root),
        log_dir=_resolve_path(p.get("log_dir", "logs"), root),
        database=_resolve_path(p.get("database", "data/lecture_ai.db"), root),
        obsidian_vault=_resolve_path(vault_raw, root) if vault_raw else None,
    )

    t = _sub(raw, "transcription")
    transcription = TranscriptionConfig(
        provider=str(t.get("provider", "local_whisper")),
        local_whisper=_dc(LocalWhisperConfig, _sub(t, "local_whisper")),
        openai=_dc(OpenAIASRConfig, _sub(t, "openai")),
    )

    a = _sub(raw, "audio")
    exts = a.get("extensions")
    audio = AudioConfig(
        ffmpeg_path=str(a.get("ffmpeg_path", "") or ""),
        ffprobe_path=str(a.get("ffprobe_path", "") or ""),
        target_sample_rate=int(a.get("target_sample_rate", 16000)),
        target_channels=int(a.get("target_channels", 1)),
        normalize=bool(a.get("normalize", False)),
        extensions=tuple(str(e).lower() for e in exts) if isinstance(exts, list) and exts
        else AudioConfig.extensions,
        chunking=_dc(ChunkingConfig, _sub(a, "chunking")),
    )

    cfg = Config(
        paths=paths,
        transcription=transcription,
        audio=audio,
        processing=_dc(ProcessingConfig, _sub(raw, "processing")),
        repair=_dc(RepairConfig, _sub(raw, "repair")),
        clean=_dc(CleanConfig, _sub(raw, "clean")),
        course=_dc(CourseMatchConfig, _sub(raw, "course")),
        llm=_dc(LLMConfig, _sub(raw, "llm")),
        vision=_dc(VisionConfig, _sub(raw, "vision")),
        note=_dc(NoteConfig, _sub(raw, "note")),
        obsidian=_dc(ObsidianConfig, _sub(raw, "obsidian")),
        privacy=_dc(PrivacyConfig, _sub(raw, "privacy")),
        logging=_dc(LoggingConfig, _sub(raw, "logging")),
        config_path=cfg_path if cfg_path.exists() else None,
    )
    return cfg


def _load_dotenv(root: Path) -> None:
    """加载 .env。python-dotenv 缺失时退化为手工解析，不让它成为硬依赖。"""
    env_file = root / ".env"
    if not env_file.exists():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file, override=False)
        return
    except ImportError:
        pass
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
