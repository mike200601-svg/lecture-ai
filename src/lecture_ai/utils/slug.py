"""生成用于目录名 / session_id 的 slug。

刻意不做中文转拼音：
  - 引入 pypinyin 是额外依赖，收益低；
  - 课程 key 本来就在 courses.yaml 里由用户写成英文（quantum_mechanics）。
中文只在极端情况（用户直接用中文 key）出现，此时保留中文即可 —— NTFS 支持。
"""

from __future__ import annotations

import re

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')  # Windows 文件名非法字符
_SPACES = re.compile(r"[\s_]+")
_DASHES = re.compile(r"-{2,}")

MAX_SLUG_LEN = 40  # 控制 session 目录名长度，规避 Windows 260 路径上限


def slugify(text: str, max_len: int = MAX_SLUG_LEN) -> str:
    """转成安全的目录名片段。

    >>> slugify("Quantum Mechanics")
    'quantum-mechanics'
    >>> slugify("量子力学 / 第一章")
    '量子力学-第一章'
    """
    s = _UNSAFE.sub("-", text.strip())
    s = _SPACES.sub("-", s)
    s = _DASHES.sub("-", s).strip("-.")
    s = s.lower()
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "untitled"
