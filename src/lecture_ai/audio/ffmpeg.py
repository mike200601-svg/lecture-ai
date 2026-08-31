"""ffmpeg / ffprobe 的定位、探测与调用。

Windows 上 ffmpeg 经常没装，因此做三级降级：
    config 指定 -> PATH -> imageio-ffmpeg 自带的静态二进制
注意 imageio-ffmpeg 只带 ffmpeg 不带 ffprobe，所以 probe 必须有不依赖 ffprobe 的退路。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from lecture_ai.errors import AudioError, DependencyMissing
from lecture_ai.logging_setup import get_logger

log = get_logger(__name__)

_INSTALL_HINT = (
    "未找到 ffmpeg。任选一种方式安装：\n"
    "  1) winget install Gyan.FFmpeg            （推荐，装完重开终端）\n"
    "  2) pip install imageio-ffmpeg            （无需管理员权限，自带静态二进制）\n"
    "  3) 在 config/config.yaml 里设置 audio.ffmpeg_path 指向 ffmpeg.exe"
)

_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d\d):(\d\d\.\d+)")
_STREAM_RE = re.compile(r"Audio:.*?,\s*(\d+)\s*Hz,\s*([^,]+)")


@dataclass(frozen=True)
class FFmpegTools:
    ffmpeg: str
    ffprobe: str | None
    source: str  # config | path | imageio

    @property
    def has_ffprobe(self) -> bool:
        return self.ffprobe is not None


@dataclass(frozen=True)
class AudioInfoProbe:
    duration_sec: float
    sample_rate: int | None = None
    channels: int | None = None
    codec: str | None = None
    creation_time: datetime | None = None
    bit_rate: int | None = None


@lru_cache(maxsize=4)
def _locate(ffmpeg_path: str, ffprobe_path: str) -> FFmpegTools:
    # 1) 配置显式指定
    if ffmpeg_path:
        exe = Path(ffmpeg_path)
        if not exe.exists():
            raise DependencyMissing(f"config 中的 audio.ffmpeg_path 不存在：{exe}")
        probe = ffprobe_path or _sibling_ffprobe(exe)
        return FFmpegTools(str(exe), probe, "config")

    # 2) PATH
    found = shutil.which("ffmpeg")
    if found:
        probe = ffprobe_path or shutil.which("ffprobe")
        return FFmpegTools(found, probe, "path")

    # 3) imageio-ffmpeg 兜底（注意：无 ffprobe）
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        return FFmpegTools(exe, ffprobe_path or shutil.which("ffprobe"), "imageio")
    except (ImportError, RuntimeError):
        pass

    raise DependencyMissing(_INSTALL_HINT)


def _sibling_ffprobe(ffmpeg_exe: Path) -> str | None:
    """ffmpeg 同目录下通常就有 ffprobe。"""
    for name in ("ffprobe.exe", "ffprobe"):
        candidate = ffmpeg_exe.parent / name
        if candidate.exists():
            return str(candidate)
    return shutil.which("ffprobe")


def get_tools(ffmpeg_path: str = "", ffprobe_path: str = "") -> FFmpegTools:
    """定位 ffmpeg 工具链。结果被缓存，不会每次都去扫 PATH。"""
    return _locate(ffmpeg_path or "", ffprobe_path or "")


def ffmpeg_version(tools: FFmpegTools) -> str:
    try:
        out = _run([tools.ffmpeg, "-version"], timeout=15)
        return out.stdout.splitlines()[0] if out.stdout else "unknown"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"调用失败：{exc}"


def _run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    """统一的子进程调用。传 list 而非字符串，避免中文路径被 shell 拆坏。"""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


# --------------------------------------------------------------------------- probe


def probe_audio(path: Path, tools: FFmpegTools) -> AudioInfoProbe:
    """探测音频时长/采样率/声道/录制时间。

    优先 ffprobe（信息全）；没有 ffprobe 时退化为解析 `ffmpeg -i` 的 stderr。
    """
    if tools.has_ffprobe:
        try:
            return _probe_with_ffprobe(path, tools)
        except (AudioError, json.JSONDecodeError, OSError) as exc:
            log.warning("ffprobe 探测失败，回退到 ffmpeg 解析：%s", exc)
    return _probe_with_ffmpeg(path, tools)


def _probe_with_ffprobe(path: Path, tools: FFmpegTools) -> AudioInfoProbe:
    cmd = [
        str(tools.ffprobe), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(path),
    ]
    result = _run(cmd, timeout=120)
    if result.returncode != 0:
        raise AudioError(f"ffprobe 失败（{path.name}）：{result.stderr.strip()[:300]}")

    data = json.loads(result.stdout or "{}")
    fmt = data.get("format", {})
    streams = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    stream = streams[0] if streams else {}

    duration = _to_float(fmt.get("duration")) or _to_float(stream.get("duration")) or 0.0
    if duration <= 0:
        raise AudioError(f"无法获取音频时长：{path.name}")

    return AudioInfoProbe(
        duration_sec=duration,
        sample_rate=_to_int(stream.get("sample_rate")),
        channels=_to_int(stream.get("channels")),
        codec=stream.get("codec_name"),
        creation_time=_parse_creation_time(fmt.get("tags") or {}, stream.get("tags") or {}),
        bit_rate=_to_int(fmt.get("bit_rate")),
    )


def _probe_with_ffmpeg(path: Path, tools: FFmpegTools) -> AudioInfoProbe:
    """无 ffprobe 时的退路：ffmpeg -i 会把媒体信息打到 stderr。"""
    result = _run([tools.ffmpeg, "-hide_banner", "-i", str(path)], timeout=120)
    text = result.stderr or ""

    m = _DURATION_RE.search(text)
    if not m:
        raise AudioError(
            f"无法解析音频信息：{path.name}。ffmpeg 输出：{text.strip()[:300]}"
        )
    duration = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    sample_rate = channels = None
    sm = _STREAM_RE.search(text)
    if sm:
        sample_rate = int(sm.group(1))
        layout = sm.group(2).strip()
        channels = 1 if layout == "mono" else 2 if layout == "stereo" else None

    return AudioInfoProbe(duration_sec=duration, sample_rate=sample_rate, channels=channels)


def _parse_creation_time(*tag_dicts: dict) -> datetime | None:
    """从容器 tag 里找录制时间。手机录音机通常会写 creation_time。"""
    for tags in tag_dicts:
        for key in ("creation_time", "date", "com.apple.quicktime.creationdate"):
            value = tags.get(key)
            if not value:
                continue
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- 转码


def convert_to_wav(
    src: Path,
    dst: Path,
    tools: FFmpegTools,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
    normalize: bool = False,
    timeout: int = 3600,
) -> Path:
    """转成 ASR 友好的 wav。src 只读，绝不覆盖原始录音。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.name + ".tmp.wav")

    cmd = [
        tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
        "-y",                      # 只覆盖我们自己的 tmp 文件
        "-i", str(src),
        "-vn",                     # 丢弃可能存在的封面图
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", "pcm_s16le",
    ]
    if normalize:
        cmd += ["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"]
    cmd.append(str(tmp))

    log.debug("ffmpeg 转码：%s -> %s", src.name, dst.name)
    try:
        result = _run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        tmp.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg 转码超时（{timeout}s）：{src.name}") from exc

    if result.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        raise AudioError(f"ffmpeg 转码失败（{src.name}）：{result.stderr.strip()[:500]}")

    tmp.replace(dst)
    return dst


def split_audio(
    src: Path,
    out_dir: Path,
    tools: FFmpegTools,
    *,
    chunk_sec: int,
    overlap_sec: int = 0,
    duration_sec: float | None = None,
    timeout: int = 3600,
) -> list[tuple[Path, float]]:
    """按固定长度切片，返回 [(片段路径, 起始偏移秒), ...]。

    默认不用（faster-whisper 自己会流式处理），只在超长录音或配置开启时启用。
    切片带 overlap 是为了避免正好切在一句话中间。
    """
    if chunk_sec <= 0:
        raise AudioError("chunk_sec 必须为正数")
    out_dir.mkdir(parents=True, exist_ok=True)

    if duration_sec is None:
        duration_sec = probe_audio(src, tools).duration_sec

    step = max(1, chunk_sec - overlap_sec)
    chunks: list[tuple[Path, float]] = []
    index = 0
    offset = 0.0
    while offset < duration_sec:
        target = out_dir / f"chunk_{index:03d}.wav"
        cmd = [
            tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-ss", f"{offset:.3f}",
            "-t", str(chunk_sec),
            "-i", str(src),
            "-vn", "-c:a", "pcm_s16le",
            str(target),
        ]
        result = _run(cmd, timeout=timeout)
        if result.returncode != 0 or not target.exists():
            raise AudioError(f"切片失败（offset={offset}）：{result.stderr.strip()[:300]}")
        # ffmpeg 在超出总时长时会产出空文件，遇到就停
        if target.stat().st_size <= 44:  # 只有 wav 头
            target.unlink(missing_ok=True)
            break
        chunks.append((target, offset))
        index += 1
        offset += step

    return chunks
