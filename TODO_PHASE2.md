# Phase 1.5 / Phase 2 TODO

> 当前边界：Phase 1.5 已完成；Phase 2A 工程实现完成，正在做 GPT 网页 Canary；
> Phase 2B/C/D 只设计。

## Phase 1.5 Selective Transcript Repair

- [x] 标准化复读/低多样性异常指标与 `SuspiciousRegion`
- [x] 15 秒可配置上下文扩窗与相邻窗口合并
- [x] 复用 medium CPU/int8、课程专属 glossary 和 anti-repetition 参数
- [x] ORIGINAL/REPAIRED 质量门，差结果拒绝
- [x] 单调、无大范围重叠的时间窗 merge
- [x] RAW JSON/Markdown 前后 SHA-256 硬校验
- [x] `repair` / `--dry-run` / `--region` / `--force`
- [x] 窗口级缓存、配置 fingerprint 与幂等复用
- [x] 真实 94 分钟数字电子技术课堂验收

金样本：19 个异常 segments 合并为 16 个 repair windows；16 accepted / 0 rejected；
可疑时长 582.86s → 0s；最大压缩率 29.7333 → 1.7419；额外 ASR 715.87s。

## Phase 2A Transcript Cleaning

- [x] REPAIRED 优先、RAW fallback 与 source layer/SHA provenance
- [x] 8 分钟核心块 + 前后 30 秒 overlap（配置限制 5–10 分钟）
- [x] Stage 1 清洗 + 条件式 Stage 2 boundary reconciliation
- [x] 外部 `prompts/transcript_clean.md`
- [x] common + course glossary 注入
- [x] provider-neutral `LLMClient`
- [x] OpenAI Responses API 真实 provider 与结构化 JSON Schema
- [x] 默认 GPT 网页任务包/response 导入 provider（无需 SDK/API key）
- [x] FakeLLM、拓扑/长度质量门、时间戳与不确定性保留
- [x] 每块独立缓存、指数退避、只重跑失败块
- [x] provider/model/token/elapsed/retry/request id 审计
- [x] `clean` / `--dry-run` / `--chunk` / `--force`
- [x] `clean-canary` 隔离 RAW/REPAIRED/CLEANED 与网页交换文件
- [x] 自然边界确定性跳过，只有重叠文本冲突才调用 LLM
- [ ] 人工回填并验收 Gold Session chunk 02/05/09 三段 Canary
- [ ] Canary PASS 后对 Gold Session 真实运行 12 个 chunks + 条件边界协调
- [ ] 按 RAW/REPAIRED/CLEANED 抽查 10 个指定类型窗口

当前停止点：三段 Canary `prompt.md` 已生成，等待 GPT 网页 `response.json`。禁止用
FakeLLM 生成 Gold Session 的 `transcript_clean.json`。

## Phase 2B Lecture Structure Detection — NOT IMPLEMENTED

未来输入：CLEANED。未来输出：`analysis/outline.json`。

- [ ] 定稿 `prompts/chapter_detection.md`
- [ ] 定义 `LectureOutline` schema 与 `StructurePipeline` 接口
- [ ] 保留 topics/subtopics/time ranges/source ids/definitions/derivations/examples/emphasis/exam tips
- [ ] FakeLLM schema、越界时间、未知 source id、章节覆盖率测试
- [ ] Gold Session 人工检查章节边界

## Phase 2C Structured Knowledge Extraction — NOT IMPLEMENTED

未来输入：CLEANED + outline。未来输出：`analysis/knowledge.json` 和
`analysis/unresolved_visual.json`。

- [ ] 定稿 `prompts/concept_extraction.md`
- [ ] 定义 `KnowledgePipeline` / knowledge / unresolved visual schemas
- [ ] 所有知识项带 source ids；不完整公式进入 uncertain/unresolved，不得补写
- [ ] visual reference 包含 timestamp/context/reference_type/confidence
- [ ] FakeLLM 的 provenance、虚构公式、视觉引用路由测试

## Phase 2D Audio-only Lecture Draft — NOT IMPLEMENTED

未来输入：outline + knowledge。未来输出：`note/lecture_audio_draft.md`。

- [ ] 定稿 `prompts/lecture_note.md`，明确 audio-only / not final
- [ ] 定义 `AudioDraftPipeline` 接口和带 provenance 的 note block schema
- [ ] 待板书内容用 `[!question]`，不得伪装成已解决
- [ ] 快照、引用覆盖率、信息保留与禁止 Obsidian 写入测试

## 本轮明确不做

- Phase 2B / 2C / 2D 代码
- Phase 3 板书融合
- VaultWriter、WikiLinks、Concept Notes、Course Index、知识图谱
- 音频强降噪、新 DSP pipeline、外置麦克风方案
