"""Watch 服务测试：单实例锁、轮询处理、异常不中断循环。"""

from __future__ import annotations

import os
import time

import pytest

from lecture_ai.errors import LectureAIError
from lecture_ai.pipeline import Phase1Pipeline, Watcher
from lecture_ai.pipeline.watcher import SingleInstanceLock
from tests.conftest import make_wav


@pytest.fixture
def pipeline(config, db, monkeypatch, has_ffmpeg):
    from tests.test_pipeline_e2e import _stub_audio_stack

    if not has_ffmpeg:
        _stub_audio_stack(monkeypatch)
    return Phase1Pipeline(config, db)


def _drop_audio(config, name="录音_20260902_140000.wav", seconds=12.0):
    path = make_wav(config.paths.incoming_audio / name, seconds=seconds)
    old = time.time() - 300
    os.utime(path, (old, old))
    return path


# --------------------------------------------------------------------- 锁


def test_lock_acquire_and_release(tmp_path):
    lock = SingleInstanceLock(tmp_path / "watch.lock")
    assert lock.acquire() is True
    assert (tmp_path / "watch.lock").exists()
    lock.release()
    assert not (tmp_path / "watch.lock").exists()


def test_second_instance_refused(tmp_path):
    """防止两个 watch 同时抢同一批录音。"""
    first = SingleInstanceLock(tmp_path / "watch.lock")
    first.acquire()
    second = SingleInstanceLock(tmp_path / "watch.lock")
    assert second.acquire() is False
    first.release()


def test_stale_lock_is_taken_over(tmp_path):
    """上次崩溃留下的锁文件不能永久堵死 watch。"""
    lock_path = tmp_path / "watch.lock"
    lock_path.write_text("999999", encoding="utf-8")  # 几乎不可能存在的 PID
    assert SingleInstanceLock(lock_path).acquire() is True


def test_corrupt_lock_is_taken_over(tmp_path):
    lock_path = tmp_path / "watch.lock"
    lock_path.write_text("这不是PID", encoding="utf-8")
    assert SingleInstanceLock(lock_path).acquire() is True


# --------------------------------------------------------------------- 循环


def test_watch_processes_dropped_file(config, pipeline):
    _drop_audio(config)
    watcher = Watcher(config, pipeline)
    assert watcher.run(max_iterations=1) == 0

    ids = pipeline.sessions.list_ids()
    assert len(ids) == 1
    transcript = (pipeline.sessions.session_dir(ids[0])
                  / "transcript" / "transcript_raw.json")
    assert transcript.exists()


def test_watch_releases_lock_on_exit(config, pipeline):
    Watcher(config, pipeline).run(max_iterations=1)
    assert not (config.paths.cache_dir / "watch.lock").exists()


def test_watch_survives_errors(config, pipeline, monkeypatch):
    """单轮出错必须记日志后继续，不能让后台服务直接死掉。"""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise LectureAIError("模拟一次扫描失败")
        return []

    monkeypatch.setattr(pipeline, "run_once", flaky)
    watcher = Watcher(config, pipeline)
    watcher._sleep = lambda _s: None  # 测试里不真等
    assert watcher.run(max_iterations=2) == 0
    assert calls["n"] == 2  # 第一轮抛错后仍然跑了第二轮


def test_watch_stops_on_request(config, pipeline):
    watcher = Watcher(config, pipeline)
    watcher.request_stop()
    assert watcher.run(max_iterations=10) == 0
    assert pipeline.sessions.list_ids() == []  # 立刻停止，什么都没处理


def test_watch_runs_web_batch_maintenance(config, pipeline):
    class StubWebBatches:
        calls = 0

        def run_once(self):
            self.calls += 1
            return []

    batches = StubWebBatches()
    watcher = Watcher(config, pipeline, web_batches=batches)
    assert watcher.run(max_iterations=1) == 0
    assert batches.calls == 1
