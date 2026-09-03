# 课堂自动笔记项目阶段进度汇报

> 日期：2026-09-02  
> 当前主线：Phase 2A Transcript Cleaning  
> 总结：Phase 0、Phase 1 核心链路、Phase 1.5 与 Phase 2A Canary 已完成；正式 Gold
> CLEANED 尚未生成；Phase 1 连续真实课堂验收现已完成 3/3。

## 一、当前结论

项目已经从“录音能不能自动进来并转成文字”推进到“真实长课堂转录能否由 GPT 网页忠实
清洗”的阶段。手机录音、Syncthing 入站、本地 Whisper、选择性修复、GPT 网页交换包、
严格 JSON 校验、缓存和审计链路都已经跑通。

Gold Session 的三个 Canary 样本（chunk 002 / 005 / 009，约 24 分钟、450 个 segment）
全部 PASS。当前可以进入 Phase 2A 的 Gold 全量清洗，但还没有生成正式
`transcript_clean.json/.md`，也没有进入 Phase 2B/C/D。

## 二、阶段状态

| 阶段 | 状态 | 结果 |
|---|---|---|
| Phase 0 工程骨架 | 完成 | 配置、数据库、Session、日志、CLI、测试与 Git 回滚点齐全 |
| Phase 1 自动录音转录 | 核心完成 | 手机到电脑再到本地 Whisper 的真实长课链路已跑通 |
| Phase 1 连续课堂验收 | 3/3 | 数字电路、量子力学、大数据三节真实长课均完成自动链路 |
| Phase 1.5 选择性修复 | 完成 | 数字电路与量子力学真实长课均完成异常检测/修复 |
| Phase 2A 工程实现 | 完成 | 分块、网页任务包、严格 schema、缓存、边界协调和 provenance 齐全 |
| Phase 2A Canary | PASS | chunk 002 / 005 / 009 全部通过 |
| Phase 2A Gold 全量 | 进行中 | 000、002、005、009 已接受；余下 8 块转为整包网页交换 |
| Phase 2B/C/D | 未实现 | 只有架构、Prompt 与测试计划 |
| Phase 3 板书融合 | 未开始 | 仅有占位目录/设计 |
| Phase 4 Obsidian | 未开始 | 尚未写入 Vault |

## 三、真实数据验收

### 数字电子技术基础（Gold Session）

- Session：`2026-09-01_unknown_001`；目录名保留 `unknown`，metadata 课程已经正确。
- 音频：01:34:42，AAC 48 kHz 双声道。
- 原始 ASR：1291 段；选择性修复后 1447 段。
- Phase 1.5：16 个 repair window，16 accepted / 0 rejected；可疑时长 582.86 秒降至 0。
- Phase 2A Canary：3 个块、450 段、6694 → 6966 字，总体字符比 104.06%。
- Canary 审计：345 条 correction、48 个 uncertainty segment、12 个视觉引用、15 个
  审计空段；三块均通过 schema、拓扑、长度与人工质量检查。
- 已知遗留：录音前约 146 秒主要是候课环境声，原 ASR 的首句时间戳提前并漏掉两句正式
  开课语。为保持 Canary 的 source SHA，当前暂未覆写正式 REPAIRED。

### 量子力学（口音场景）

- Session：`2026-09-02_unknown_001`；metadata 课程为量子力学。
- 音频：01:40:28，AAC 48 kHz 双声道。
- 本地 medium/CPU：1625 段，耗时 2636.34 秒（2.286x 实时）。
- Phase 1.5：4 个异常 segment 合并为 3 个窗口，3 accepted / 0 rejected；修复后 1639 段，
  229.67 秒可疑时长降至 0，选择性 ASR 额外耗时 94.27 秒。
- A/B 结论：ASR hotwords 会在弱语音处背诵术语表，因此本地 Whisper 默认已关闭
  hotwords；glossary 继续用于 Phase 2 文本纠错。
- 尚未运行量子力学 Phase 2A Canary/全量清洗。

## 四、Phase 2A Canary 结果

| 块 | Segment | 源字符 → 清洗字符 | 状态 | 重点 |
|---|---:|---:|---|---|
| 002 | 171 | 2446 → 2394 | PASS | 清除 prompt 串入与长重复；恢复 3 条照片引用 |
| 005 | 132 | 2048 → 2180 | PASS | 例子和推理保留；6 个 uncertainty segment |
| 009 | 147 | 2200 → 2392 | PASS | 数制转换与公式上下文恢复；9 条公式/板书引用 |

chunk 009 的公式恢复经过额外说明和约 311 秒 no-VAD 原声复核。用户明确选择接受 GPT 5.6
的原始保守清洗，不采用会阻止正常上下文纠错的公式 token 硬锁。`173 ÷ 2` 的商由 ASR
写成 80、GPT 恢复为 86 的差异已经披露并记录。

## 五、工程质量与运行环境

- Python 3.13.14，项目根 `.venv`。
- 本地 `faster-whisper-medium` READY，CPU/int8 实际加载成功；无 NVIDIA/CUDA 是已知条件。
- Hugging Face medium / large-v3-turbo 缓存 partial 不影响当前本地 medium 正式链路。
- 完整测试：259 passed。
- `doctor`：关键项 OK；`pip check`：无依赖冲突；`git diff --check`：通过。
- 隐私：云端音频 false、云端图片 false、云端文本 true。
- 正式数据未提交 Git；模型、录音、数据库和 Session 数据继续由 `.gitignore` 排除。

当前关键 Git 回滚点：

- `9d93f86`：GPT 网页清洗工作流
- `af699d4`：ASR 恢复与 Canary 校验加固
- `e8c84cd`：按用户决策接受 GPT 公式上下文恢复并完成 Canary 收口

## 六、当前没有做的内容

- 没有正式 Gold `transcript_clean.json/.md`；整包返回并通过后由 watcher 自动组装。
- 没有 Phase 2B 章节结构产物。
- 没有 Phase 2C 知识抽取或未解决视觉引用产物。
- 没有 Phase 2D audio-only 笔记草稿。
- 没有板书图片融合、Obsidian 写入、WikiLinks、概念卡片或知识图谱。

## 七、下一步选择

### A. 完成 Gold Phase 2A 全量清洗（推荐）

由 watcher 汇总全部未完成块并处理必要的冲突边界，最终生成正式 CLEANED。Canary 的
chunk 002 / 005 / 009 已按完整指纹复用，chunk 000 也已正式接受；余下 8 块统一装入一个
手机可见的 ZIP，整包交给 GPT 网页后把返回 ZIP 放回即可，不再逐段搬运。

完成标准：

- `analysis/transcript_clean.json/.md` 正式生成；
- 1447 个源 segment 全覆盖，时间戳与 provenance 完整；
- 抽查公式、重复、prompt echo、视觉引用、长推理等 10 类窗口；
- 不使用 FakeLLM，不跳过网页回复校验。

### B. Phase 1 连续 3/3 验收（已完成）

第三节大数据录音已完成同步、自动 Session 创建、本地转录与选择性修复。老师未使用扩音器、
课堂椅子杂音较多，但整条 Phase 1 链路稳定完成。

### C. 对量子力学做 Phase 2A Canary

适合验证“教师口音 + 专业术语 + 无 hotwords”情况下 GPT 清洗能否可靠修复文本。建议在
Gold 正式 CLEANED 产出后进行，避免同时维护两条人工网页任务队列。

### D. 进入 Phase 2B 章节结构识别

现在技术上可以开始实现，但不建议抢跑。Phase 2B 的输入应当是经过正式验收的 CLEANED；
否则上游文本仍变动，章节边界和后续知识抽取会一起返工。（返工当然也能做，主要是人会
逐渐失去表情。）

## 八、推荐顺序

1. Gold Session 全量 12 块清洗并生成正式 CLEANED。
2. 完成 Gold 的 10 类人工抽查与首段漏识别的增量处理方案。
3. Phase 1 连续 3/3 已完成，不再占用主线。
4. 对量子力学做口音场景 Phase 2A Canary。
5. 再进入 Phase 2B 章节结构识别；Phase 2C/D 与 Obsidian 后移。

当前最值得做的是第 1 项：基础设施和 Canary 都已经通过，再不跑全量，多少有点像火箭
验收完毕以后拿来晾衣服。
