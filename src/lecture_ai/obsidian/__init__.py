"""Obsidian Vault 写入 —— Phase 4 实现，当前仅占位。

规划接口：
    class VaultWriter(ABC):
        def write_lecture_note(self, session, markdown) -> Path
        def ensure_concept_stub(self, concept, source) -> Path
        def update_course_index(self, course_key) -> Path

vault_path 只从 config 读，任何位置禁止硬编码。
"""
