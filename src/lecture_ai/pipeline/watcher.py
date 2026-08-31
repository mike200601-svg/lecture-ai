"""Watch 服务：长驻轮询 incoming 目录。

刻意用轮询而不是 watchdog 的文件系统事件：
  - Windows 上同步软件（网盘/手机助手）会产生大量中间事件，噪声大；
  - 我们本来就需要「文件稳定性」判定，轮询天然契合；
  - 少一个依赖。
课堂录音是按小时计的任务，15 秒的发现延迟毫无影响。
"""

from __future__ import annotations

import os
import signal
import time
from pathlib import Path

from lecture_ai.config import Config
from lecture_ai.errors import LectureAIError
from lecture_ai.logging_setup import get_logger
from lecture_ai.pipeline.phase1 import Phase1Pipeline

log = get_logger(__name__)

LOCK_FILENAME = "watch.lock"


class SingleInstanceLock:
    """防止同时跑两个 watch 进程重复处理同一批文件。"""

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._acquired = False

    def acquire(self) -> bool:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text(encoding="utf-8").strip() or 0)
            except (ValueError, OSError):
                pid = 0
            if pid and _pid_alive(pid):
                log.error("已有 watch 进程在运行（PID %d）", pid)
                return False
            log.warning("发现残留锁文件（PID %s 已不存在），接管", pid or "未知")
        self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
        self._acquired = True
        return True

    def release(self) -> None:
        if self._acquired:
            self.lock_path.unlink(missing_ok=True)
            self._acquired = False

    def __enter__(self) -> "SingleInstanceLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, check=False,
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


class Watcher:
    def __init__(self, config: Config, pipeline: Phase1Pipeline | None = None) -> None:
        self.config = config
        self.pipeline = pipeline or Phase1Pipeline(config)
        self._stop = False

    def request_stop(self, *_args) -> None:
        if not self._stop:
            log.info("收到停止信号，本轮结束后退出…")
        self._stop = True

    def run(self, max_iterations: int | None = None) -> int:
        """主循环。max_iterations 仅供测试使用。"""
        lock = SingleInstanceLock(self.config.paths.cache_dir / LOCK_FILENAME)
        if not lock.acquire():
            return 1

        _install_signal_handlers(self.request_stop)
        interval = max(1, self.config.processing.poll_interval)
        log.info(
            "watch 已启动：监听 %s（每 %d 秒扫描一次，Ctrl+C 停止）",
            self.config.paths.incoming_audio, interval,
        )

        iterations = 0
        try:
            while not self._stop:
                try:
                    outcomes = self.pipeline.run_once()
                    for o in outcomes:
                        if o.ok:
                            log.info("✔ %s 处理完成（%s）", o.session_id, o.message)
                        else:
                            log.error("✘ %s 处理失败：%s", o.session_id, o.message)
                except LectureAIError as exc:
                    # 业务异常不能打断长驻循环，记录后继续
                    log.error("本轮处理出错（将继续运行）：%s", exc)
                except Exception:
                    log.exception("本轮处理发生未预期错误（将继续运行）")

                iterations += 1
                if max_iterations is not None and iterations >= max_iterations:
                    break
                self._sleep(interval)
        except KeyboardInterrupt:
            log.info("收到 Ctrl+C，正在退出…")
        finally:
            self.pipeline.close()
            lock.release()
            log.info("watch 已停止")
        return 0

    def _sleep(self, seconds: int) -> None:
        """分片睡眠，保证 Ctrl+C 能及时响应。"""
        for _ in range(seconds * 2):
            if self._stop:
                return
            time.sleep(0.5)


def _install_signal_handlers(handler) -> None:
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            pass  # 非主线程时无法注册，忽略
