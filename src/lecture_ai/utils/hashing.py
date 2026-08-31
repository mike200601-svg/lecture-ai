"""文件哈希。用于「同一个文件永不重复处理」。"""

from __future__ import annotations

import hashlib
from pathlib import Path

_CHUNK = 1024 * 1024  # 1MB，兼顾大录音文件与内存


def sha256_file(path: Path, chunk_size: int = _CHUNK) -> str:
    """分块计算文件 SHA256。录音动辄几百 MB，不能一次性读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk_size)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
