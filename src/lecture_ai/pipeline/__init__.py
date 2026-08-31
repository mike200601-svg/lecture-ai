"""Pipeline：按 Session 状态机编排各处理步骤，以及长驻的 watch 服务。"""

from lecture_ai.pipeline.diagnostics import AudioProbeReport, probe_audio_metadata
from lecture_ai.pipeline.phase1 import Phase1Pipeline, ProcessOutcome
from lecture_ai.pipeline.watcher import Watcher

__all__ = [
    "AudioProbeReport",
    "probe_audio_metadata",
    "Phase1Pipeline",
    "ProcessOutcome",
    "Watcher",
]
