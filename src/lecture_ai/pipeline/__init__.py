"""Pipeline：按 Session 状态机编排各处理步骤，以及长驻的 watch 服务。"""

from lecture_ai.pipeline.diagnostics import AudioProbeReport, probe_audio_metadata
from lecture_ai.pipeline.phase1 import Phase1Pipeline, ProcessOutcome
from lecture_ai.pipeline.watcher import Watcher
from lecture_ai.repair.pipeline import RepairPipeline
from lecture_ai.cleaning.pipeline import CleanPipeline
from lecture_ai.structure.pipeline import StructurePipeline

__all__ = [
    "AudioProbeReport",
    "probe_audio_metadata",
    "Phase1Pipeline",
    "ProcessOutcome",
    "Watcher",
    "RepairPipeline",
    "CleanPipeline",
    "StructurePipeline",
]
