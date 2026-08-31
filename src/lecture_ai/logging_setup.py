"""日志配置。总 Prompt 第十二条：必须用正式 logging，不要 print 满天飞。

Windows 特别处理：控制台默认 GBK，直接打中文/特殊符号会 UnicodeEncodeError，
因此显式把 stream handler 重绑到 UTF-8。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from lecture_ai.config import LoggingConfig

_FILE_FORMAT = "%(asctime)s | %(levelname)-7s | %(name)-28s | %(session)-38s | %(message)s"
_CONSOLE_FORMAT = "%(levelname)-7s | %(message)s"

_configured = False


class _SessionFilter(logging.Filter):
    """给没有绑定 session 的日志补一个占位，避免格式化报错。"""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "session"):
            record.session = "-"
        return True


def setup_logging(log_dir: Path, cfg: LoggingConfig | None = None, *, verbose: bool = False) -> None:
    """初始化根 logger。重复调用是幂等的（只配置一次）。"""
    global _configured
    if _configured:
        return
    cfg = cfg or LoggingConfig()
    log_dir.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger("lecture_ai")
    root.setLevel(logging.DEBUG)
    root.propagate = False
    root.handlers.clear()

    session_filter = _SessionFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "lecture-ai.log",
        maxBytes=cfg.max_bytes,
        backupCount=cfg.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))
    file_handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    file_handler.addFilter(session_filter)
    root.addHandler(file_handler)

    console = logging.StreamHandler(_utf8_stream())
    console.setLevel(logging.DEBUG if verbose else getattr(logging, cfg.console_level.upper(), logging.INFO))
    console.setFormatter(logging.Formatter(_CONSOLE_FORMAT))
    console.addFilter(session_filter)
    root.addHandler(console)

    _configured = True


def _utf8_stream():
    """尽量把 stderr 切成 UTF-8；不支持则原样返回（旧 Python / 被重定向的流）。"""
    stream = sys.stderr
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    return stream


def get_logger(name: str, session_id: str | None = None) -> logging.LoggerAdapter:
    """获取带 session 上下文的 logger。

    用法：log = get_logger(__name__, session.session_id)
    """
    logger = logging.getLogger(name if name.startswith("lecture_ai") else f"lecture_ai.{name}")
    return logging.LoggerAdapter(logger, {"session": session_id or "-"})


def attach_session_log(session_dir: Path, session_id: str) -> logging.Handler:
    """为单个 session 额外挂一个日志文件，返回 handler 以便结束后 detach。"""
    log_path = session_dir / "logs" / "session.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(_FILE_FORMAT))
    handler.addFilter(_SessionFilter())
    handler.addFilter(lambda r: getattr(r, "session", "-") == session_id)

    logger = logging.getLogger("lecture_ai")
    # 库被当作模块调用（未走 setup_logging）时，logger 级别仍是 NOTSET，
    # INFO 记录会在 logger 层就被丢掉，session.log 因此为空。这里兜底放开。
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    return handler


def detach_session_log(handler: logging.Handler) -> None:
    logging.getLogger("lecture_ai").removeHandler(handler)
    handler.close()
