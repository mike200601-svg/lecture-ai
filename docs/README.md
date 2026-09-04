# 文档索引

这里放的是**开发过程记录**，不是使用说明。想跑起来看 [根目录 README](../README.md)。

之所以留着它们：这个项目做过一次否定自己的产品级 A/B 对照，然后砍掉了默认路线上
的大部分流水线。下面这些文档是那个过程的证据，也是"为什么不做某件事"的出处。
读代码时遇到"这里为什么要留一道闸门"，答案基本都在里面。

## 当前权威

| 文档 | 内容 |
|---|---|
| [AB_EVALUATION.md](AB_EVALUATION.md) | **先读这个。** 那次否定自己的 A/B 产品级对照，脱敏公开版。项目为什么长成现在这样，答案在这里。 |
| [../ROADMAP.md](../ROADMAP.md) | **唯一路线权威**。与其他文档冲突时以它为准。 |
| [STATUS.md](STATUS.md) | 各阶段实现状态、已冻结的里程碑、明确不做的事。 |

## 设计文档

| 文档 | 内容 | 状态 |
|---|---|---|
| [DESIGN_EXPORT_PACKAGE.md](DESIGN_EXPORT_PACKAGE.md) | GPT 网页投喂包的输入归属规则、manifest、幂等边界 | 已实现 |
| [DESIGN_OBSIDIAN.md](DESIGN_OBSIDIAN.md) | Obsidian 入库的设计与**刻意不做的清单**（不建概念页 / WikiLink / 知识图谱） | 设计，未实现 |

## 架构

| 文档 | 内容 |
|---|---|
| [ARCHITECTURE_V1.md](ARCHITECTURE_V1.md) | Phase 1 / 1.5：录音发现、转录、选择性重转录的分层与依赖约束 |
| [ARCHITECTURE_PHASE2.md](ARCHITECTURE_PHASE2.md) | Phase 2A–2D：忠实清洗、章节检出、知识抽取、草稿编排 |

## 历史记录

| 文档 | 内容 |
|---|---|
| [TODO_PHASE1.md](TODO_PHASE1.md) | Phase 1 / 1.5 的逐项验收记录 |
| [TODO_PHASE2.md](TODO_PHASE2.md) | Phase 2 的逐项验收记录 |
| [PHASE_PROGRESS_REPORT_2026-09-02.md](PHASE_PROGRESS_REPORT_2026-09-02.md) | 2026-09-02 的阶段进展快照 |
| [V1_RELEASE_NOTES.md](V1_RELEASE_NOTES.md) | v1.0 收口说明 |
| [项目总Prompt.md](项目总Prompt.md) | 项目最初的完整需求描述。后续很多决定都偏离了它 —— 偏离的理由见 ROADMAP。 |
