"""路径操作。全部考虑 Windows：跨盘移动、中文路径、原子写、不覆盖。"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def ensure_dir(path: Path) -> Path:
    """确保目录存在，返回该目录。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def unique_path(target: Path) -> Path:
    """若目标已存在，追加 _1 / _2 ... 直到不冲突。

    存在即改名，绝不覆盖 —— 原始录音和板书不允许被任何操作覆盖。
    """
    if not target.exists():
        return target
    stem, suffix, parent = target.stem, target.suffix, target.parent
    i = 1
    while True:
        candidate = parent / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


def safe_move(src: Path, dst_dir: Path, *, copy: bool = False) -> Path:
    """把 src 移动（或复制）到 dst_dir，保留原文件名，不覆盖已有文件。

    shutil.move / copy2 本身会处理跨盘（D: -> C:）的情况。
    """
    ensure_dir(dst_dir)
    target = unique_path(dst_dir / src.name)
    if copy:
        shutil.copy2(src, target)
    else:
        shutil.move(str(src), str(target))
    return target


def atomic_write_text(path: Path, text: str) -> None:
    """原子写文本：先写 .tmp 再 os.replace。

    metadata.json 和 transcript 都用它 —— 中途崩溃不能留下半个损坏文件。
    统一 UTF-8 + LF，避免 Windows 下的 GBK / CRLF 问题。
    """
    ensure_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def rel_to(path: Path, base: Path) -> str:
    """尽量返回相对路径（存进 metadata 便于整个项目目录搬家），失败则返回绝对路径。"""
    try:
        return path.relative_to(base).as_posix()
    except ValueError:
        return path.as_posix()
