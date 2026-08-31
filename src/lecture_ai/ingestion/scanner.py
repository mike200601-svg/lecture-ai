"""发现 incoming 目录里的新录音。

两个必须做对的地方：
  1. 文件写入完成判定 —— 手机同步到一半就处理会得到损坏音频；
  2. 内容级去重 —— 同一份录音换个名字复制进来，不能再处理一遍。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from lecture_ai.config import Config
from lecture_ai.database import Database
from lecture_ai.logging_setup import get_logger
from lecture_ai.utils.hashing import sha256_file

log = get_logger(__name__)


@dataclass
class DiscoveredFile:
    path: Path
    sha256: str
    size: int
    mtime: datetime


@dataclass(frozen=True)
class StartTimeGuess:
    """录音起始时间的推断结果。

    confidence 会写进 metadata —— Phase 3 用照片 EXIF 对齐时间轴时，
    低置信度的 session 需要人工确认，否则照片会挂到错误的时间点上。
    """

    dt: datetime
    source: str        # ffprobe | filename | mtime-duration | ctime
    confidence: str    # high | medium | low


# 常见录音文件名里的时间戳形态：
#   录音_20260903_140000 / 20260903-140000 / REC 2026-09-03 14.00.00 / 2026_09_03 14_00
_FILENAME_PATTERNS = [
    re.compile(r"(?P<y>\d{4})[-_.]?(?P<mo>\d{2})[-_.]?(?P<d>\d{2})"
               r"[ _-]+(?P<h>\d{2})[-_.:]?(?P<mi>\d{2})(?:[-_.:]?(?P<s>\d{2}))?"),
    re.compile(r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})T?(?P<h>\d{2})(?P<mi>\d{2})(?P<s>\d{2})"),
]


def is_stable(
    path: Path,
    *,
    stable_checks: int = 2,
    quiet_seconds: int = 10,
    _samples: dict[Path, list[tuple[int, float]]] | None = None,
    resample_delay: float = 0.0,
) -> bool:
    """判断文件是否写入完成。

    三重条件：
      1. 距最后修改时间已超过 quiet_seconds；
      2. 连续 stable_checks 次采样 (size, mtime) 完全一致；
      3. 能以读方式打开（Windows 上被同步软件独占时会失败）。

    采样历史怎么攒起来，取决于调用方：
      - watch 长驻模式：`_samples` 跨轮次复用，两次轮询之间天然隔了 poll_interval，
        无需等待；
      - 一次性 scan：进程活不到下一轮，只能靠 `resample_delay` 就地补采样，
        否则永远凑不满 stable_checks，扫描结果永远为空。
    """
    try:
        st = path.stat()
    except OSError:
        return False

    age = datetime.now().timestamp() - st.st_mtime
    if age < quiet_seconds:
        return False

    if _samples is not None and stable_checks > 1:
        history = _samples.setdefault(path, [])
        history.append((st.st_size, st.st_mtime))
        del history[:-stable_checks]

        while len(history) < stable_checks and resample_delay > 0:
            time.sleep(resample_delay)
            try:
                st = path.stat()
            except OSError:
                return False
            history.append((st.st_size, st.st_mtime))

        if len(history) < stable_checks or len(set(history)) != 1:
            return False

    try:
        with open(path, "rb") as f:
            f.read(1)
    except OSError:
        log.debug("文件暂时无法读取（可能仍在同步）：%s", path.name)
        return False

    return True


class AudioScanner:
    """扫描 incoming/audio，产出待处理的新文件。

    实例持有采样历史，因此 watch 模式下应复用同一个实例。

    one_shot=True 用于 `scan` / `process --scan` 这类跑完就退出的命令：
    进程等不到下一轮轮询，只能就地多采样一次。
    """

    def __init__(self, config: Config, db: Database, *, one_shot: bool = False) -> None:
        self.config = config
        self.db = db
        self.resample_delay = 1.0 if one_shot else 0.0
        self._samples: dict[Path, list[tuple[int, float]]] = {}
        self._reported_dupes: set[str] = set()

    def scan(self) -> list[DiscoveredFile]:
        incoming = self.config.paths.incoming_audio
        if not incoming.exists():
            return []

        exts = set(self.config.audio.extensions)
        found: list[DiscoveredFile] = []

        for path in sorted(incoming.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if path.suffix.lower() not in exts:
                log.debug("忽略非音频文件：%s", path.name)
                continue
            if not is_stable(
                path,
                stable_checks=self.config.processing.stable_checks,
                quiet_seconds=self.config.processing.quiet_seconds,
                _samples=self._samples,
                resample_delay=self.resample_delay,
            ):
                log.debug("文件尚未稳定，跳过本轮：%s", path.name)
                continue

            try:
                st = path.stat()
                digest = sha256_file(path)
            except OSError as exc:
                log.warning("读取文件失败，跳过：%s（%s）", path.name, exc)
                continue

            existing = self.db.file_exists(digest)
            if existing is not None:
                if digest not in self._reported_dupes:
                    self._reported_dupes.add(digest)
                    log.info(
                        "跳过重复文件 %s（内容已处理过，session=%s）",
                        path.name, existing["session_id"],
                    )
                continue

            found.append(
                DiscoveredFile(
                    path=path,
                    sha256=digest,
                    size=st.st_size,
                    mtime=datetime.fromtimestamp(st.st_mtime),
                )
            )

        return found


def guess_start_time(
    path: Path,
    *,
    duration_sec: float | None = None,
    creation_time: datetime | None = None,
) -> StartTimeGuess:
    """推断录音起始时间。按可靠性从高到低尝试。"""
    # 1) 容器元数据（手机录音机通常会写）
    if creation_time is not None:
        dt = creation_time.astimezone() if creation_time.tzinfo else creation_time
        return StartTimeGuess(dt, "ffprobe", "high")

    # 2) 文件名里的时间戳
    parsed = _parse_filename_time(path.name)
    if parsed is not None:
        return StartTimeGuess(parsed, "filename", "high")

    # 3) mtime 减时长：多数录音 App 在停止录音时才落盘，故 mtime≈结束时间
    st = path.stat()
    mtime = datetime.fromtimestamp(st.st_mtime)
    if duration_sec:
        return StartTimeGuess(
            mtime - timedelta(seconds=duration_sec), "mtime-duration", "medium"
        )

    # 4) ctime 兜底。Windows 上 st_ctime 是文件创建时间；它可能是同步到电脑的
    # 时间而非录制时间，所以只能给 low 置信度，但仍比把 mtime 伪装成录制起点明确。
    ctime = datetime.fromtimestamp(st.st_ctime)
    return StartTimeGuess(ctime, "ctime", "low")


def _parse_filename_time(name: str) -> datetime | None:
    for pattern in _FILENAME_PATTERNS:
        m = pattern.search(name)
        if not m:
            continue
        g = m.groupdict()
        try:
            return datetime(
                int(g["y"]), int(g["mo"]), int(g["d"]),
                int(g["h"]), int(g["mi"]), int(g.get("s") or 0),
            )
        except (ValueError, TypeError):
            continue
    return None
