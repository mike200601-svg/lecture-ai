"""音频层测试：ffmpeg 定位、探测、转码、切片。

需要 ffmpeg 的用例会在缺失时自动跳过，保证没装 ffmpeg 的机器也能跑完整个测试套。
"""

from __future__ import annotations

import pytest

from lecture_ai.audio import (
    convert_to_wav,
    get_tools,
    preprocess_audio,
    probe_audio,
    split_audio,
)
from lecture_ai.audio.ffmpeg import _probe_with_ffmpeg, _parse_creation_time
from lecture_ai.errors import DependencyMissing
from tests.conftest import make_wav

@pytest.fixture
def tools(has_ffmpeg):
    """需要真实 ffmpeg 的用例统一通过这个 fixture 取工具链，缺失时自动跳过。"""
    if not has_ffmpeg:
        pytest.skip("未安装 ffmpeg")
    return get_tools()


# --------------------------------------------------------------------- 定位


def test_missing_ffmpeg_gives_install_hint(monkeypatch):
    """找不到 ffmpeg 时，错误信息必须能直接告诉用户怎么装。"""
    import lecture_ai.audio.ffmpeg as ff

    ff._locate.cache_clear()
    monkeypatch.setattr(ff.shutil, "which", lambda name: None)
    monkeypatch.setitem(__import__("sys").modules, "imageio_ffmpeg", None)

    with pytest.raises(DependencyMissing) as exc:
        ff.get_tools()
    assert "winget" in str(exc.value)
    ff._locate.cache_clear()


def test_explicit_path_that_does_not_exist(monkeypatch, tmp_path):
    import lecture_ai.audio.ffmpeg as ff

    ff._locate.cache_clear()
    with pytest.raises(DependencyMissing, match="ffmpeg_path"):
        ff.get_tools(str(tmp_path / "nope.exe"))
    ff._locate.cache_clear()


# --------------------------------------------------------------------- 探测


def test_probe_duration(tools, tmp_path):
    wav = make_wav(tmp_path / "a.wav", seconds=3.0, sample_rate=16000)
    info = probe_audio(wav, tools)
    assert info.duration_sec == pytest.approx(3.0, abs=0.1)
    assert info.sample_rate == 16000
    assert info.channels == 1


def test_probe_fallback_without_ffprobe(tools, tmp_path):
    """无 ffprobe 的退路（解析 ffmpeg stderr）也必须能拿到时长。"""
    wav = make_wav(tmp_path / "a.wav", seconds=3.0)
    info = _probe_with_ffmpeg(wav, tools)
    assert info.duration_sec == pytest.approx(3.0, abs=0.15)


def test_parse_creation_time():
    dt = _parse_creation_time({"creation_time": "2026-09-03T06:00:00.000000Z"})
    assert dt is not None and dt.year == 2026
    assert _parse_creation_time({}, {"nothing": "x"}) is None
    assert _parse_creation_time({"creation_time": "不是时间"}) is None


# --------------------------------------------------------------------- 转码


def test_convert_to_16k_mono(tools, tmp_path):
    src = make_wav(tmp_path / "src.wav", seconds=2.0, sample_rate=44100, channels=2)
    dst = tmp_path / "out" / "audio_16k.wav"
    convert_to_wav(src, dst, tools, sample_rate=16000, channels=1)

    info = probe_audio(dst, tools)
    assert info.sample_rate == 16000
    assert info.channels == 1
    assert list((tmp_path / "out").glob("*.tmp.wav")) == []  # 临时文件已清理


def test_convert_does_not_touch_source(tools, tmp_path):
    """转码绝不能改动原始录音。"""
    src = make_wav(tmp_path / "src.wav", seconds=2.0, sample_rate=44100)
    before_bytes = src.read_bytes()
    before_mtime = src.stat().st_mtime

    convert_to_wav(src, tmp_path / "out.wav", tools)

    assert src.read_bytes() == before_bytes
    assert src.stat().st_mtime == before_mtime


def test_preprocess_is_idempotent(tools, config, tmp_path):
    """第二次调用应直接复用，不重新转码。"""
    session_dir = tmp_path / "session"
    raw = make_wav(session_dir / "raw" / "rec.wav", seconds=2.0, sample_rate=44100)

    first = preprocess_audio(raw, session_dir, config)
    assert first.reused is False

    second = preprocess_audio(raw, session_dir, config)
    assert second.reused is True
    assert second.processed_path == first.processed_path


def test_preprocess_force_reruns(tools, config, tmp_path):
    session_dir = tmp_path / "session"
    raw = make_wav(session_dir / "raw" / "rec.wav", seconds=2.0)
    preprocess_audio(raw, session_dir, config)
    assert preprocess_audio(raw, session_dir, config, force=True).reused is False


def test_cjk_path_conversion(tools, tmp_path):
    """中文路径必须能正常转码 —— 真实项目路径就是中文的。"""
    src = make_wav(tmp_path / "课堂录音" / "第一讲 录音.wav", seconds=1.0)
    dst = tmp_path / "输出目录" / "audio_16k.wav"
    convert_to_wav(src, dst, tools)
    assert dst.exists() and dst.stat().st_size > 1000


# --------------------------------------------------------------------- 切片


def test_split_audio_offsets(tools, tmp_path):
    src = make_wav(tmp_path / "long.wav", seconds=10.0)
    chunks = split_audio(src, tmp_path / "chunks", tools, chunk_sec=4, overlap_sec=1)

    assert len(chunks) >= 3
    offsets = [off for _, off in chunks]
    assert offsets[0] == 0.0
    assert offsets == sorted(offsets)
    assert offsets[1] == 3.0  # chunk_sec 4 - overlap 1
    for path, _ in chunks:
        assert path.exists()


def test_split_rejects_bad_chunk_size(tools, tmp_path):
    from lecture_ai.errors import AudioError

    src = make_wav(tmp_path / "a.wav", seconds=2.0)
    with pytest.raises(AudioError):
        split_audio(src, tmp_path / "c", tools, chunk_sec=0)
