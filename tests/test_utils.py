"""工具函数测试。"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest

from lecture_ai.utils.hashing import sha256_file
from lecture_ai.utils.paths import atomic_write_text, safe_move, unique_path
from lecture_ai.utils.slug import slugify
from lecture_ai.utils.timefmt import hhmmss, hhmmss_ms, parse_iso, to_iso


def test_sha256_matches_hashlib(tmp_path):
    content = b"lecture audio bytes" * 1000
    f = tmp_path / "a.bin"
    f.write_bytes(content)
    assert sha256_file(f) == hashlib.sha256(content).hexdigest()


def test_sha256_chunking_consistent(tmp_path):
    """分块大小不能影响结果 —— 大录音文件走的就是多块路径。"""
    f = tmp_path / "a.bin"
    f.write_bytes(b"x" * 5000)
    assert sha256_file(f, chunk_size=64) == sha256_file(f, chunk_size=1 << 20)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Quantum Mechanics", "quantum-mechanics"),
        ("quantum_mechanics", "quantum-mechanics"),
        ("a/b\\c:d*e", "a-b-c-d-e"),
        ("  spaced  out  ", "spaced-out"),
        ("", "untitled"),
        ("...", "untitled"),
    ],
)
def test_slugify(raw, expected):
    assert slugify(raw) == expected


def test_slugify_keeps_cjk():
    assert slugify("量子力学") == "量子力学"


def test_slugify_truncates():
    assert len(slugify("a" * 100)) == 40


def test_hhmmss():
    assert hhmmss(0) == "00:00:00"
    assert hhmmss(61) == "00:01:01"
    assert hhmmss(3661) == "01:01:01"
    assert hhmmss(5531.4) == "01:32:11"
    assert hhmmss(-5) == "00:00:00"  # 探测失败时不产生诡异时间戳


def test_hhmmss_ms():
    assert hhmmss_ms(0) == "00:00:00.000"
    assert hhmmss_ms(1452.25) == "00:24:12.250"


def test_iso_roundtrip():
    dt = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)
    assert parse_iso(to_iso(dt)) == dt
    assert to_iso(None) is None
    assert parse_iso(None) is None


def test_naive_datetime_gets_timezone():
    assert to_iso(datetime(2026, 9, 3, 14, 0)).count(":") >= 2


def test_unique_path_avoids_overwrite(tmp_path):
    f = tmp_path / "录音.m4a"
    f.write_text("original", encoding="utf-8")
    assert unique_path(f).name == "录音_1.m4a"
    (tmp_path / "录音_1.m4a").write_text("x", encoding="utf-8")
    assert unique_path(f).name == "录音_2.m4a"


def test_safe_move_does_not_overwrite(tmp_path):
    """原始录音绝不能被覆盖 —— 这是总 Prompt 的硬红线。"""
    src_dir, dst_dir = tmp_path / "in", tmp_path / "out"
    src_dir.mkdir()
    dst_dir.mkdir()
    (dst_dir / "rec.m4a").write_text("existing", encoding="utf-8")

    src = src_dir / "rec.m4a"
    src.write_text("new", encoding="utf-8")
    moved = safe_move(src, dst_dir)

    assert moved.name == "rec_1.m4a"
    assert (dst_dir / "rec.m4a").read_text(encoding="utf-8") == "existing"
    assert moved.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_safe_move_copy_keeps_source(tmp_path):
    src_dir, dst_dir = tmp_path / "in", tmp_path / "out"
    src_dir.mkdir()
    src = src_dir / "rec.m4a"
    src.write_text("data", encoding="utf-8")
    safe_move(src, dst_dir, copy=True)
    assert src.exists()


def test_atomic_write_leaves_no_temp(tmp_path):
    target = tmp_path / "meta.json"
    atomic_write_text(target, '{"课程": "量子力学"}')
    assert target.read_text(encoding="utf-8") == '{"课程": "量子力学"}'
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_uses_lf(tmp_path):
    """统一 LF，避免 Windows 上写出 CRLF 污染 Obsidian 笔记。"""
    target = tmp_path / "a.md"
    atomic_write_text(target, "line1\nline2")
    assert b"\r\n" not in target.read_bytes()
