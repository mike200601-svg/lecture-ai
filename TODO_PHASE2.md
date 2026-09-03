# Phase 1.5 / Phase 2 TODO

> 当前边界：Phase 1.5 已完成；Phase 2A 工程实现与 GPT 网页 Canary 已通过；
> Phase 2B/2C/2D 工程均已实现，但等待 Gold 上游产物与逐阶段真实验收。

## Phase 1.5 Selective Transcript Repair

- [x] 标准化复读/低多样性异常指标与 `SuspiciousRegion`
- [x] 超长低文字密度 (`low_text_density`) 与 glossary 串入 (`prompt_echo`) 检测
- [x] 跨多个短 segment 的完全重复检测与 CLEANED 端审计去重
- [x] 稀疏窗口 no-hotwords/no-VAD 恢复、短占位复读清理与新增异常拒绝门
- [x] 15 秒可配置上下文扩窗与相邻窗口合并
- [x] 复用 medium CPU/int8、课程专属 glossary 和 anti-repetition 参数
- [x] ORIGINAL/REPAIRED 质量门，差结果拒绝
- [x] 单调、无大范围重叠的时间窗 merge
- [x] RAW JSON/Markdown 前后 SHA-256 硬校验
- [x] `repair` / `--dry-run` / `--region` / `--force`
- [x] 窗口级缓存、配置 fingerprint 与幂等复用
- [x] 真实 94 分钟数字电子技术课堂验收
- [x] 真实 100 分钟量子力学口音课堂扫描：4 个异常 segments / 3 个窗口 / 3 accepted
- [ ] Gold 正式 CLEANED 完成后再升级开头 REPAIRED；当前保持 Canary 的 source SHA 不变

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
- [x] 显式照片/图像/板书引用确定性标注，避免模型漏填 `visual_references`
- [x] 网页 prompt 更新时旧 response/cache/CLEANED 可恢复封存
- [x] 人工回填并验收 Gold Session chunk 02/05/09 三段 Canary（全部 PASS）
- [ ] Canary PASS 后对 Gold Session 真实运行 12 个 chunks + 条件边界协调
- [ ] 按 RAW/REPAIRED/CLEANED 抽查 10 个指定类型窗口

当前停止点：三段 Canary 全部 PASS。下一步对 Gold Session 运行 12 个真实 GPT 网页清洗块
及条件式边界协调；禁止用 FakeLLM 生成 Gold Session 的 `transcript_clean.json`。

## Phase 2B Lecture Structure Detection — ENGINEERING COMPLETE

未来输入：CLEANED。未来输出：`analysis/outline.json`。

- [x] 定稿 `prompts/chapter_detection.md`
- [x] 定义严格 outline schema 与 `StructurePipeline` 接口
- [x] 保留 topics/subtopics/time ranges/source ids/definitions/derivations/examples/emphasis/exam tips
- [x] FakeLLM schema、越界时间、未知 source id、章节覆盖率与丢失推导测试
- [x] CLEANED 硬输入门、SHA/prompt/schema fingerprint、缓存与 source-change invalidation
- [x] GPT 网页任务接入手机双向整包交换、坏结果隔离与 watcher 自动续跑
- [ ] Gold Session 人工检查章节边界

## Phase 2C Structured Knowledge Extraction — ENGINEERING COMPLETE

未来输入：CLEANED + outline。未来输出：`analysis/knowledge.json` 和
`analysis/unresolved_visual.json`。

- [x] 定稿 `prompts/concept_extraction.md`
- [x] 定义 `KnowledgePipeline` / knowledge / unresolved visual schemas
- [x] 所有知识项带 source ids；不完整公式进入 uncertain/unresolved，不得补写
- [x] visual reference 包含 timestamp/context/reference_type/confidence
- [x] CLEANED + outline 双输入 SHA/fingerprint 硬门与缓存失效
- [x] GPT 网页任务接入手机双向整包交换、坏结果隔离与 watcher 自动续跑
- [x] FakeLLM 的 provenance、虚构公式、视觉引用路由测试
- [ ] Gold Session 人工检查知识证据与未决视觉队列

## Phase 2D Audio-only Lecture Draft — ENGINEERING COMPLETE

未来输入：outline + knowledge。未来输出：`note/lecture_audio_draft.md`。

- [x] 定稿 `prompts/lecture_note.md`，明确 audio-only / not final
- [x] 定义 `AudioDraftPipeline` 接口和带 provenance 的 note block schema
- [x] knowledge item 必须逐 topic 恰好编排一次，禁止丢失、重复与跨 topic
- [x] 待板书、听辨疑点和残缺公式用 `[!question]`，不得伪装成已解决
- [x] 由严格 JSON 确定性渲染 Markdown，禁止自由 Markdown、WikiLink 与 Vault 写入
- [x] GPT 网页任务接入手机双向整包交换、坏结果隔离与 watcher 自动续跑
- [x] 快照、引用覆盖率、信息保留与禁止 Obsidian 写入测试
- [ ] Gold Session 人工检查草稿忠实度与所有 question callout

## 本轮明确不做

- Phase 3 板书融合
- VaultWriter、WikiLinks、Concept Notes、Course Index、知识图谱
- 音频强降噪、新 DSP pipeline、外置麦克风方案
