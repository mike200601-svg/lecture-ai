"""LLM 接入层 —— Phase 2（AI 课堂笔记）实现，当前仅占位。

规划接口：
    class LLMClient(ABC):
        def complete(self, prompt, *, system=None, max_tokens, temperature) -> str

Prompt 一律放 prompts/*.md，禁止硬编码在 Python 里。
"""
