"""统一异常层次。

pipeline 靠这些类型区分「配置/环境问题（重试无用）」与「运行时问题（可重试）」。
所有自定义异常都继承 LectureAIError，CLI 顶层只捕获它，其他异常照常抛出堆栈。
"""

from __future__ import annotations


class LectureAIError(Exception):
    """所有本项目异常的基类。"""


class ConfigError(LectureAIError):
    """配置文件缺失、格式错误、或含有不该出现的密钥。重试无用。"""


class DependencyMissing(LectureAIError):
    """外部依赖（ffmpeg、faster-whisper 等）不可用。重试无用，需用户安装。"""


class IngestError(LectureAIError):
    """文件发现 / 归档阶段出错。"""


class AudioError(LectureAIError):
    """音频探测、转码、切片出错。"""


class TranscriptionError(LectureAIError):
    """ASR 阶段出错。通常可重试。"""


class SessionNotFound(LectureAIError):
    """指定的 session 不存在。"""


class InvalidTransition(LectureAIError):
    """非法的状态迁移。属于程序 bug，不应在正常流程中出现。"""

    def __init__(self, current: str, target: str) -> None:
        super().__init__(f"非法状态迁移：{current} -> {target}")
        self.current = current
        self.target = target
