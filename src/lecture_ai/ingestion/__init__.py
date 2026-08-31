"""Ingestion：发现新录音、判断写入完成、去重、归档。

注意：Watch 服务不在这里 —— 它需要编排「扫描 + 处理」，属于 pipeline 层。
本层只做「发现文件」，不依赖任何上层模块。
"""

from lecture_ai.ingestion.scanner import (
    AudioScanner,
    DiscoveredFile,
    StartTimeGuess,
    guess_start_time,
    is_stable,
)

__all__ = [
    "AudioScanner",
    "DiscoveredFile",
    "StartTimeGuess",
    "guess_start_time",
    "is_stable",
]
