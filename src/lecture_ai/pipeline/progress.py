"""转录进度的跨进程通道。

watch 和 serve 是两个独立进程：进度产生在 watch 里，想看它的人在 serve 那边。
最省事的通道就是 session 目录下的一个小 JSON —— 项目本来就把每个 session 的
产物都放在那儿，不必为了一个进度条引入 IPC，也不必往数据库里写高频行。

两条硬要求：

- **写失败绝不能打断转录。** 转录是整条流水线最贵的一步（一节课 45–65 分钟），
  为了一个展示用的百分比让它抛异常是本末倒置，所以这里所有 IO 异常都吞掉。
- **读的人不能看到半截 JSON。** 先写 .tmp 再 ``replace``，同目录内的替换在
  Windows 和 POSIX 上都是原子的。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from lecture_ai.logging_setup import get_logger

log = get_logger(__name__)

PROGRESS_FILENAME = "progress.json"

#: 两次落盘之间的最小间隔（秒）。faster-whisper 每 30 秒音频回调一次，
#: 一节 100 分钟的课约 200 次，节流后基本只在真正有变化时写。
MIN_WRITE_INTERVAL = 2.0

#: 超过这个秒数没更新，就认为写进度的那个进程已经不在了。
#: 取值要大于回调间隔（30 秒音频，CPU int8 上实际约 15–25 秒真实时间）的数倍，
#: 否则正常转录中途会被误判成僵死。
STALE_AFTER_SEC = 180.0


def progress_path(session_dir: Path) -> Path:
    return session_dir / "transcript" / PROGRESS_FILENAME


class ProgressWriter:
    """把 (已处理秒数, 总秒数) 落到 session 目录，供 WebUI 读取。"""

    def __init__(
        self, session_dir: Path, *, min_interval: float = MIN_WRITE_INTERVAL
    ) -> None:
        self.path = progress_path(session_dir)
        self.min_interval = min_interval
        self._last_write = 0.0

    def update(self, done_sec: float, total_sec: float, *, force: bool = False) -> None:
        """记录一次进度。``force`` 用于起止两端，保证首尾一定落盘。"""
        now = time.monotonic()
        if not force and now - self._last_write < self.min_interval:
            return
        self._last_write = now

        payload = {
            "done_sec": round(max(0.0, done_sec), 1),
            "total_sec": round(max(0.0, total_sec), 1),
            "updated_at": time.time(),   # 墙钟：读的人要算「多久没动了」
            "pid": os.getpid(),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            tmp.replace(self.path)
        except OSError as exc:
            log.debug("写转录进度失败（忽略）：%s", exc)

    def clear(self) -> None:
        """转录结束（成功或失败）后删掉，避免面板显示一个停在 87% 的幽灵。"""
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("清理转录进度失败（忽略）：%s", exc)


def read_progress(session_dir: Path) -> dict | None:
    """读进度。文件不存在或内容不可用时返回 None，绝不抛异常。"""
    try:
        raw = progress_path(session_dir).read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    try:
        done = float(data.get("done_sec") or 0.0)
        total = float(data.get("total_sec") or 0.0)
        updated_at = float(data.get("updated_at") or 0.0)
    except (TypeError, ValueError):
        return None

    age = max(0.0, time.time() - updated_at) if updated_at else None
    return {
        "done_sec": done,
        "total_sec": total,
        "percent": round(min(done / total, 1.0) * 100, 1) if total > 0 else None,
        "updated_at": updated_at or None,
        "age_sec": round(age, 1) if age is not None else None,
        # 长时间不动 = 写它的进程多半已经没了。宁可显示「疑似中断」，
        # 也不要让面板挂着一个永远不变的百分比让人以为还在跑。
        "stale": age is not None and age > STALE_AFTER_SEC,
        "pid": data.get("pid"),
    }
