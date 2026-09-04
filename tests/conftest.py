"""pytest 公共 fixture。

原则：测试不依赖 GPU、模型、网络。ffmpeg 若存在就用真的，不存在则用 wave 模块替身，
保证在任何机器上 `pytest -q` 都能跑通。
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

from lecture_ai.config import load_config

# 含中文的目录名 —— 真实项目路径就是「D:\原创项目\课堂自动笔记项目」，
# 编码问题必须在测试里就暴露出来。
CJK_DIRNAME = "课堂测试项目"

MINIMAL_CONFIG = """
paths:
  incoming_audio: data/incoming/audio
  incoming_images: data/incoming/images
  web_exchange: data/web_exchange
  session_dir: data/sessions
  processed_dir: data/processed
  cache_dir: data/cache
  log_dir: logs
  database: data/test.db

transcription:
  provider: fake

audio:
  extensions: [".wav", ".mp3", ".m4a"]

processing:
  poll_interval: 1
  stable_checks: 1
  quiet_seconds: 0
  min_audio_seconds: 1

course:
  match_tolerance_minutes: 30

privacy:
  allow_cloud_audio: false
"""

COURSES_YAML = """
courses:
  quantum_mechanics:
    name: 量子力学
    teacher: 张老师
    semester: 2026-秋
    glossary: quantum_mechanics.txt
    schedule:
      - weekday: 3
        start: "14:00"
        end: "15:40"
  electrodynamics:
    name: 电动力学
    glossary: electrodynamics.txt
    schedule:
      - weekday: 2
        start: "08:00"
        end: "09:40"
  unknown:
    name: 未归类
    schedule: []
"""

GLOSSARY_COMMON = "# 注释行\n本征值\n算符\n\n本征值\n哈密顿量\n"
GLOSSARY_QM = "# 量子力学\n薛定谔方程\nSchrödinger\n波函数\n"


def make_wav(path: Path, seconds: float = 3.0, sample_rate: int = 16000,
             freq: float = 440.0, channels: int = 1) -> Path:
    """用标准库生成一段正弦波 wav。不需要 ffmpeg。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    n_frames = int(seconds * sample_rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        frames = bytearray()
        for i in range(n_frames):
            value = int(16000 * math.sin(2 * math.pi * freq * i / sample_rate))
            frames += struct.pack("<h", value) * channels
        w.writeframes(bytes(frames))
    return path


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """一个完整的临时项目：含 config、courses、glossary。目录名带中文。"""
    root = tmp_path / CJK_DIRNAME
    (root / "config" / "glossary").mkdir(parents=True)
    (root / "prompts").mkdir(parents=True)
    (root / "config" / "config.yaml").write_text(MINIMAL_CONFIG, encoding="utf-8")
    (root / "config" / "courses.yaml").write_text(COURSES_YAML, encoding="utf-8")
    (root / "config" / "glossary" / "common.txt").write_text(GLOSSARY_COMMON, encoding="utf-8")
    (root / "config" / "glossary" / "quantum_mechanics.txt").write_text(
        GLOSSARY_QM, encoding="utf-8"
    )
    prompt_source = Path(__file__).resolve().parents[1] / "prompts" / "transcript_clean.md"
    (root / "prompts" / "transcript_clean.md").write_text(
        prompt_source.read_text(encoding="utf-8"), encoding="utf-8"
    )
    structure_prompt = Path(__file__).resolve().parents[1] / "prompts" / "chapter_detection.md"
    (root / "prompts" / "chapter_detection.md").write_text(
        structure_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )
    knowledge_prompt = Path(__file__).resolve().parents[1] / "prompts" / "concept_extraction.md"
    (root / "prompts" / "concept_extraction.md").write_text(
        knowledge_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )
    draft_prompt = Path(__file__).resolve().parents[1] / "prompts" / "lecture_note.md"
    (root / "prompts" / "lecture_note.md").write_text(
        draft_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )
    export_prompt = Path(__file__).resolve().parents[1] / "prompts" / "export_session.md"
    (root / "prompts" / "export_session.md").write_text(
        export_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )
    api_note_prompt = Path(__file__).resolve().parents[1] / "prompts" / "api_note.md"
    (root / "prompts" / "api_note.md").write_text(
        api_note_prompt.read_text(encoding="utf-8"), encoding="utf-8"
    )
    (root / "pyproject.toml").write_text("[project]\nname='t'\n", encoding="utf-8")
    return root


@pytest.fixture
def config(project_root: Path):
    cfg = load_config(project_root / "config" / "config.yaml", project_root=project_root)
    cfg.ensure_dirs()
    return cfg


@pytest.fixture
def db(config):
    from lecture_ai.database import Database

    return Database(config.paths.database)


@pytest.fixture
def has_ffmpeg() -> bool:
    from lecture_ai.audio import get_tools
    from lecture_ai.errors import DependencyMissing

    try:
        get_tools()
        return True
    except DependencyMissing:
        return False
