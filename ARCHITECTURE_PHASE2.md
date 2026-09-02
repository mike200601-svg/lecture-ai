# Phase 1.5 / Phase 2 架构

本文在 `ARCHITECTURE_V1.md` 的 Phase 1 基线上扩展，不改变已经通过真实课堂验收的
录音接入、预处理和全量 ASR 链路。

## 1. 数据分层与不可变约束

流水线固定为：

```text
RAW -> REPAIRED -> CLEANED -> STRUCTURED -> NOTE
```

| 层 | 产物 | 职责 | 本轮状态 |
|---|---|---|---|
| RAW | `transcript/transcript_raw.{json,md}` | Phase 1 原始 ASR，证据层，只读 | 已实现、不可覆盖 |
| REPAIRED | `transcript/transcript_repaired.{json,md}` | 只重转录异常时间窗 | Phase 1.5 实现 |
| CLEANED | `analysis/transcript_clean.{json,md}` | 忠实纠错、断句、标点与不确定性标记 | Phase 2A 实现 |
| STRUCTURED | `analysis/outline.json` | 课堂结构与时间范围 | Phase 2B，未实现 |
| KNOWLEDGE | `analysis/knowledge.json`、`analysis/unresolved_visual.json` | 知识项与待板书解析引用 | Phase 2C，未实现 |
| NOTE | `note/lecture_audio_draft.md` | 仅基于音频的课堂草稿 | Phase 2D，未实现 |

任何下游步骤都不得回写上游文件。
Phase 1.5 每次执行前后都校验 RAW JSON 与 Markdown 的 SHA-256；发生变化立即失败。

## 2. 顶层状态与处理步骤

Session 顶层状态继续表示跨媒介主流程。纯文本增强不新增顶层状态，已转录 Session
保持 `TRANSCRIBED`，避免与未来 `IMAGES_READY -> FUSING -> GENERATING_NOTE` 产生倒退或
笛卡尔积状态。

细粒度进度写入 `metadata.json.steps` 和 SQLite `processing`：

- `repair`: `pending | running | done | failed | skipped`
- `clean`: `pending | running | done | failed | skipped`

步骤失败只使该步骤失败，不破坏 Phase 1 已完成状态和已有产物。重试从缓存和最近成功
产物继续。

## 3. Phase 1.5：选择性重转录

### 3.1 异常检测

检测器只读取 RAW segments，统一输出 `SuspiciousRegion`：

```text
id, start, end, segment_ids, reasons, original_metrics, text_preview
```

指标至少包括：文本压缩比、字符/词片段多样性、重复 n-gram 比例、最长连续重复、
no-speech 概率和时长。单项越界形成原因，时间相邻或扩窗后重叠的候选合并为一个区域。

### 3.2 扩窗、重转录与门控

候选区域两侧默认扩展 15 秒（配置范围建议 10–20 秒），截断到音频边界。只对合并后的
窗口调用当前 ASR provider；默认仍是项目内 faster-whisper medium、CPU、int8，沿用
课程专属 glossary、`condition_on_previous_text=false`、`repetition_penalty=1.1` 与
`no_repeat_ngram_size=3`。

窗口新旧文本使用同一组指标评分。自动接受必须同时满足：

1. 新文本非空、时间轴合法；
2. 综合重复异常分数下降达到阈值，或原窗口异常而新窗口降到安全阈值；
3. 新文本没有明显退化（极端缩短、no-speech 激增或异常分数反升）。

未满足时保留原窗口并记录拒绝原因。`--force` 只忽略产物缓存，不绕过质量门控；如需
人工单窗运行，用 `--region ID` 选择 dry-run 中的 region，或用 `START-END` 限定范围。

### 3.3 合并与产物

接受窗口按时间排序，先删除其替换区间内的原 segments，再插入重转录 segments；对扩窗
边界采用中点裁切，防止相邻窗口重复。最终强校验：`start <= end`、单调递增、无重叠、
时间不越界。输出：

- `transcript/transcript_repaired.json`
- `transcript/transcript_repaired.md`
- `analysis/repair_cache.json`（窗口级缓存）

JSON 保存 RAW SHA、配置与模型指纹、原始/修复指标、每窗接受决定、merge 统计和完整
repair history。相同输入及配置重复执行直接复用；变更 RAW SHA、检测阈值、padding、
ASR provider/model/options 或 glossary 都会使缓存失效。

## 4. Phase 2A：忠实转录清洗

### 4.1 输入与语义红线

优先读取 REPAIRED；不存在时回退 RAW。清洗只做 ASR 纠错、标点、断句、明显无意义口癖
整理，禁止总结、压缩、扩写和注入外部知识。例子、推导、强调、考试提示、常见错误和
板书/视觉引用必须保留。不确定内容保留原文并结构化标记，不能猜。

### 4.2 分块与边界协调

按时间轴切成默认 8 分钟块、30 秒重叠（可配置为 5–10 分钟）。处理分两阶段：

1. `clean_chunk`：每块独立清洗，返回与输入 segment id 对齐的结构化 JSON；
2. 本地先比较相邻块重叠区；文本内容相同或仅标点/空白等价时确定性合并；
3. 只有重叠文本实质冲突时才运行 `reconcile_boundary`，解决重复、断句和术语不一致，
   不得改写非重叠区。

组装器按稳定 segment id 合并，保留原始 start/end、source layer、source file SHA、
chunk id、uncertainty 和 visual references。课程 glossary 与 `common.txt` 同时提供给清洗，
但仅作为拼写候选，不是事实来源。

### 4.3 LLM 接口、缓存与失败恢复

`LLMClient` 是 provider-neutral 接口，输入 prompt/messages 与 JSON schema，返回统一的
文本、provider/model、usage 和 request id。默认真实实现为 `ChatGPTWebClient`：本地输出
`prompt.md/schema.json/request.json`，人工在 GPT 网页处理后回填 `response.json`；可选
OpenAI API provider 保留，测试实现为 `FakeLLMClient`。隐私硬闸门
`privacy.allow_cloud_transcript=false` 时拒绝真实云处理。

Prompt 位于 `prompts/transcript_clean.md`，Python 不硬编码业务提示。每块缓存指纹包含：
输入层及 SHA、segment ids/text、prompt SHA、glossary SHA、chunk/reconcile 配置、provider、
model 和 schema 版本。失败按可配置次数指数退避；成功块不会因其他块失败而重跑。

产物：

- `analysis/transcript_clean.json`
- `analysis/transcript_clean.md`
- `analysis/clean_cache/*.json`

JSON 记录逐块来源、缓存命中、重试次数、可用的 token/字符/网页轮次 usage、边界决定和
provenance。网页版不暴露 token 用量，禁止伪造，改记 input/output chars 与 web turns。
`--dry-run` 只输出计划和预计块数，不调用 provider、不写业务产物。

Canary 使用 `analysis/canary/chunk_NNN/`，每段隔离保存 `raw.md`、`repaired.md`、
`cleaned.json`、`cleaned.md` 及网页交换文件；不写正式 `transcript_clean.*`。

## 5. CLI

```text
lecture-ai repair SESSION [--dry-run] [--region ID|START-END] [--force]
lecture-ai clean  SESSION [--dry-run] [--chunk INDEX] [--force]
```

所有命令可重复执行。`repair` 默认自动检测全部候选；`clean --chunk` 供诊断单块，但只有
完整运行才组装最终 cleaned 产物。

## 6. 后续阶段接口、Prompt 与测试计划（只设计，不实现）

### Phase 2B：课堂结构识别

输入 CLEANED，使用 `prompts/chapter_detection.md`，输出 `analysis/outline.json`。每个结构项
必须带 `start/end` 和 `source_segment_ids`。计划 schema 包含：`lecture_topics`、`subtopics`、
`definitions`、`derivations`、`examples`、`teacher_emphasis`、`exam_tips`、`transitions`。
不得直接读取 RAW 绕过 provenance。计划接口：
`StructurePipeline.run(session_id) -> LectureOutline`。

测试计划：固定 FakeLLM 响应做 schema/时间范围/source id 校验；故意返回越界时间、未知 id、
重叠章节和丢失推导，确认质量门拒绝；真实课堂抽查结构边界但不以标题漂亮作为验收依据。

### Phase 2C：结构化知识抽取

输入 CLEANED + `outline.json`，使用 `prompts/concept_extraction.md`，输出
`analysis/knowledge.json` 与 `analysis/unresolved_visual.json`。计划 schema 包含：`concepts`、
`equations`、`examples`、`teacher_emphasis`、`exam_tips`、`common_errors`、`open_questions`、
`visual_references`、`uncertain_items`。所有知识项必须引用 source segment ids；音频无法恢复的
公式或图不得由模型补写，而是进入 unresolved visual 队列。

计划接口：`KnowledgePipeline.run(session_id) -> (LectureKnowledge, UnresolvedVisuals)`。
Phase 3 预留接口：`VisualResolver.resolve(reference, images_by_exif_time)`，用 reference 的
`timestamp/context/reference_type/confidence` 与照片 EXIF 时间对齐，再生成一份新的融合层，
不得回写 CLEANED/KNOWLEDGE。

测试计划：FakeLLM 覆盖公式缺失、板书引用、同一概念多处来源、不确定项；拒绝无 source id
的知识、模型凭空补出的公式和未进入 unresolved queue 的视觉依赖。

### Phase 2D：Audio-only Lecture Draft

输入 `outline.json` + `knowledge.json`，使用 `prompts/lecture_note.md`，输出
`note/lecture_audio_draft.md`。它明确是 audio draft，不是最终笔记；板书、教材和课件尚未融合。
计划章节：本节框架、核心概念、推导、课堂例子、老师强调、易错点、待板书补充、本节小结。
每段保留 source ids 或时间链接。

计划接口：`AudioDraftPipeline.run(session_id) -> AudioLectureDraft`。
测试计划：快照测试 note 结构；检查所有正文块有 provenance；视觉未决项必须呈现为
`[!question]`，不得伪装成已解决；禁止生成 WikiLinks、Concept Notes 或写入 Obsidian Vault。

以上 Phase 2B / 2C / 2D 均为接口与测试计划，**本轮不实现**。
