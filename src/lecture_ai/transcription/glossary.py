"""专业术语词典。

用途一（Phase 1）：作为 ASR 的 hotwords / initial_prompt，降低专业名词识别错误。
用途二（Phase 2）：AI 转录纠错时作为参考词表。
两处复用同一个 loader，避免词表出现两份。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from lecture_ai.logging_setup import get_logger

log = get_logger(__name__)

COMMON_FILE = "common.txt"
#: hotwords 过长会挤占 Whisper 的 prompt window，反而降低质量
DEFAULT_MAX_TERMS = 200


@dataclass
class Glossary:
    terms: list[str]
    sources: list[str]

    def __len__(self) -> int:
        return len(self.terms)

    def as_hotwords(self, max_terms: int = DEFAULT_MAX_TERMS) -> str | None:
        """拼成 faster-whisper 的 hotwords 字符串。"""
        if not self.terms:
            return None
        return " ".join(self.terms[:max_terms])

    def as_initial_prompt(self, max_terms: int = DEFAULT_MAX_TERMS) -> str | None:
        """云端 API 不支持 hotwords 时的降级路径：包装成一句自然语言提示。"""
        if not self.terms:
            return None
        return "本段录音是理工科课堂讲授，可能出现以下术语：" + "、".join(self.terms[:max_terms])


def load_glossary(glossary_dir: Path, course_glossary: str | None) -> Glossary:
    """加载 common.txt + 课程专属词表。文件缺失不报错，只是词表为空。"""
    terms: list[str] = []
    sources: list[str] = []
    seen: set[str] = set()

    for filename in (COMMON_FILE, course_glossary):
        if not filename:
            continue
        path = glossary_dir / filename
        if not path.exists():
            if filename != COMMON_FILE:
                log.warning("课程词表不存在，已跳过：%s", path)
            continue
        count = 0
        for line in path.read_text(encoding="utf-8").splitlines():
            term = line.strip()
            if not term or term.startswith("#"):
                continue
            if term in seen:
                continue
            seen.add(term)
            terms.append(term)
            count += 1
        sources.append(f"{filename}({count})")

    if terms:
        log.debug("载入术语 %d 条：%s", len(terms), ", ".join(sources))
    return Glossary(terms=terms, sources=sources)
