"""配置加载测试。重点：密钥拦截、路径解析、缺字段容错。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lecture_ai.config import find_project_root, load_config
from lecture_ai.errors import ConfigError


def test_load_minimal_config(config, project_root):
    assert config.paths.project_root == project_root
    assert config.paths.incoming_audio.is_absolute()
    assert config.paths.incoming_audio == project_root / "data" / "incoming" / "audio"
    assert config.paths.web_exchange == project_root / "data" / "web_exchange"
    assert config.transcription.provider == "fake"


def test_defaults_applied_for_missing_sections(project_root):
    """配置只写了一部分时，其余走默认值而不是崩溃。"""
    (project_root / "config" / "config.yaml").write_text(
        "paths:\n  session_dir: data/sessions\n", encoding="utf-8"
    )
    cfg = load_config(project_root / "config" / "config.yaml", project_root=project_root)
    assert cfg.transcription.provider == "local_whisper"
    assert cfg.transcription.local_whisper.use_hotwords is False
    assert cfg.audio.target_sample_rate == 16000
    assert cfg.privacy.allow_cloud_audio is False


def test_missing_config_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml", project_root=tmp_path)
    assert cfg.config_path is None
    assert cfg.transcription.provider == "local_whisper"


def test_secret_in_yaml_is_rejected(project_root):
    """API key 写进 config.yaml 必须直接报错 —— 防止误提交到版本库。"""
    (project_root / "config" / "config.yaml").write_text(
        "transcription:\n  openai:\n    api_key: sk-real-key-should-not-be-here\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="api_key"):
        load_config(project_root / "config" / "config.yaml", project_root=project_root)


def test_empty_secret_placeholder_is_allowed(project_root):
    """空占位不算泄漏，允许保留作为注释用途。"""
    (project_root / "config" / "config.yaml").write_text(
        'transcription:\n  openai:\n    api_key: ""\n', encoding="utf-8"
    )
    cfg = load_config(project_root / "config" / "config.yaml", project_root=project_root)
    assert cfg is not None


def test_env_interpolation(project_root, monkeypatch):
    monkeypatch.setenv("MY_VAULT", "D:/SomeVault")
    (project_root / "config" / "config.yaml").write_text(
        "paths:\n  obsidian_vault: ${MY_VAULT}\n", encoding="utf-8"
    )
    cfg = load_config(project_root / "config" / "config.yaml", project_root=project_root)
    assert cfg.paths.obsidian_vault == Path("D:/SomeVault")


def test_absolute_path_preserved(project_root, tmp_path):
    absolute = (tmp_path / "elsewhere").as_posix()
    (project_root / "config" / "config.yaml").write_text(
        f"paths:\n  session_dir: {absolute}\n", encoding="utf-8"
    )
    cfg = load_config(project_root / "config" / "config.yaml", project_root=project_root)
    assert cfg.paths.session_dir == Path(absolute)


def test_invalid_yaml_raises_config_error(project_root):
    (project_root / "config" / "config.yaml").write_text("paths: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(project_root / "config" / "config.yaml", project_root=project_root)


def test_ensure_dirs_creates_everything(config):
    config.ensure_dirs()
    for p in (config.paths.incoming_audio, config.paths.session_dir, config.paths.log_dir):
        assert p.exists(), p


def test_find_project_root_walks_up(project_root):
    nested = project_root / "data" / "sessions" / "deep"
    nested.mkdir(parents=True)
    assert find_project_root(nested) == project_root


def test_cjk_path_handled(config):
    """项目真实路径含中文，配置里的路径也必须原样保留。"""
    assert "课堂测试项目" in str(config.paths.session_dir)
