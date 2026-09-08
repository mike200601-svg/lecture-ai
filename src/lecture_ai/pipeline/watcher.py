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
from lecture_ai.cleaning.web_batch import CleanWebBatchService
from lecture_ai.errors import LectureAIError
from lecture_ai.logging_setup import get_logger
from lecture_ai.pipeline.autopilot import AutopilotService
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

    def heartbeat(self) -> None:
        """每轮刷新锁文件，让它的 mtime 成为「watch 还活着」的证据。

        没有心跳的话，探活只能靠 :func:`_pid_alive`，而它在 Windows 上要起一个
        tasklist 子进程 —— WebUI 每几秒轮询一次就会不停 fork。改成看 mtime 后
        探活是一次 stat，零子进程。
        """
        if not self._acquired:
            return
        try:
            self.lock_path.write_text(str(os.getpid()), encoding="utf-8")
        except OSError as exc:
            log.debug("刷新 watch 锁文件失败（忽略）：%s", exc)

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
            # pythonw.exe 下不加这个会闪一个黑框，见 audio/ffmpeg.py:_run
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return str(pid) in (result.stdout or "")
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def watch_status(config: Config) -> dict:
    """watch 长驻进程是否在跑。给 WebUI 回答「它到底有没有在工作」。

    优先看锁文件的 mtime（一次 stat）：心跳比 poll_interval 的几倍还旧才退回去
    查 PID。这样正常情况下探活不起任何子进程。
    """
    lock_path = config.paths.cache_dir / LOCK_FILENAME
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
        age = max(0.0, time.time() - lock_path.stat().st_mtime)
    except OSError:
        return {"running": False, "pid": None, "heartbeat_age_sec": None}

    try:
        pid = int(raw or 0)
    except ValueError:
        pid = 0

    # 容忍两轮丢拍：一轮在处理长任务（转录）时可能远超 poll_interval，
    # 所以下限拉到 90 秒，避免转录期间把自己判成死了。
    tolerance = max(90.0, config.processing.poll_interval * 3.0)
    alive = age <= tolerance or (bool(pid) and _pid_alive(pid))
    return {
        "running": alive,
        "pid": pid or None,
        "heartbeat_age_sec": round(age, 1),
        # 锁还在但进程没了 —— 下次 watch 启动会自动接管，这里只是如实报告
        "stale_lock": bool(pid) and not alive,
    }


class Watcher:
    def __init__(
        self,
        config: Config,
        pipeline: Phase1Pipeline | None = None,
        web_batches: CleanWebBatchService | None = None,
        autopilot: AutopilotService | None = None,
    ) -> None:
        self.config = config
        self.pipeline = pipeline or Phase1Pipeline(config)
        self.web_batches = web_batches or CleanWebBatchService(config)
        self.autopilot = autopilot or AutopilotService(config)
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
                lock.heartbeat()   # 每轮盖一次时间戳，WebUI 靠它判断死活
                try:
                    outcomes = self.pipeline.run_once()
                    for o in outcomes:
                        if o.ok:
                            log.info("✔ %s 处理完成（%s）", o.session_id, o.message)
                        else:
                            log.error("✘ %s 处理失败：%s", o.session_id, o.message)
                    # Phase 1 收尾：自动补 repair 并出投喂包
                    for step in self.autopilot.run_once():
                        if step.ok:
                            log.info("✔ %s %s", step.session_id, step.message)
                            if step.output_dir:
                                log.info("   投喂包目录：%s", step.output_dir)
                        else:
                            log.error("✘ %s %s", step.session_id, step.message)
                    for batch in self.web_batches.run_once():
                        log.info("GPT 网页批处理 · %s · %s", batch.session_id, batch.message)
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
