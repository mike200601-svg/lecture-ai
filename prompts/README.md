# Prompts

AI prompt 一律放在这个目录，**禁止硬编码在 Python 里**（总 Prompt 第十四条）。
Python 侧只做变量替换与调用，改 prompt 不需要改代码。

当前状态：Phase 2 尚未开始，以下文件是**草稿骨架**，记录已经确定的约束，
真正实现 Phase 2 时再逐个打磨。

| 文件 | 阶段 | 用途 |
|---|---|---|
| `transcript_clean.md` | Phase 2A | 忠实 ASR 纠错、分块清洗与边界协调，不做总结 |
| `chapter_detection.md` | Phase 2 | 识别章节结构 |
| `lecture_note.md` | Phase 2 | 生成最终课堂笔记 |
| `concept_extraction.md` | Phase 2 | 抽取概念（受重要性阈值约束） |
| `board_analysis.md` | Phase 3 | 板书照片理解 |
| `audio_board_fusion.md` | Phase 3 | 语音 × 板书联合理解 |

变量占位统一用 `{{name}}` 形式。
