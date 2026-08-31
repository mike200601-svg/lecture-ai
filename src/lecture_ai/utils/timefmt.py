"""时间格式化与解析。

约定：
  - 内部一律用带时区的 datetime（本地时区），序列化成 ISO8601
  - 音频内偏移一律用秒（float），显示时才转 HH:MM:SS
"""

from __future__ import annotations

from datetime import datetime, timezone


def now_local() -> datetime:
    """当前时间，带本地时区信息。"""
    return datetime.now(timezone.utc).astimezone()


def to_iso(dt: datetime | None) -> str | None:
    """datetime -> ISO8601 字符串。naive 时间视为本地时区。"""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.isoformat()


def parse_iso(text: str | None) -> datetime | None:
    """ISO8601 字符串 -> datetime。空值返回 None。"""
    if not text:
        return None
    return datetime.fromisoformat(text)


def hhmmss(seconds: float) -> str:
    """秒 -> HH:MM:SS。用于 transcript 里的时间戳显示。

    负数按 0 处理（避免探测失败时产生诡异的时间戳）。
    """
    total = max(0, int(round(seconds)))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def hhmmss_ms(seconds: float) -> str:
    """秒 -> HH:MM:SS.mmm。Phase 3 做照片对齐时需要更高精度。"""
    total = max(0.0, float(seconds))
    h, rem = divmod(int(total), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((total - int(total)) * 1000))
    if ms == 1000:  # 浮点进位边界
        ms = 0
        s += 1
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
