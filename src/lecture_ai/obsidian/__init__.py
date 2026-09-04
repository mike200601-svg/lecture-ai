"""Obsidian Vault 对接 —— 设计见 docs/DESIGN_OBSIDIAN.md，当前未实现。

Obsidian 的 Vault 就是一个普通文件夹，把 Markdown 放进去即入库，
没有需要调用的 API。所以本模块的职责不是「生成知识库」，而只是搬运与命名：

    vault-import <session_id> --note <网页会话产出的 md>
        校验笔记与 session 对应 → 补 YAML front-matter → 按 courses.yaml 的
        obsidian_folder 放到正确目录 → 附件复制进 _附件 并改写引用 →
        在 metadata.json 的 steps.obsidian 记录目标路径与 SHA（幂等）

    vault-status
        列出哪些 session 已入库、哪些还没有。

明确不做（见 docs/DESIGN_OBSIDIAN.md 第 6 节）：不自动建概念页、不自动建 WikiLink、
不建课程索引、不建知识图谱、不做 tag 推断、不做双向同步。
早期规划里的 ensure_concept_stub / update_course_index 已按 A/B 对照结论废弃。

vault_path 只从 config.paths.obsidian_vault 读，任何位置禁止硬编码。
"""
