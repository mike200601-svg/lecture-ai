# ROADMAP

> 最后更新：2026-09-03
> 本文是当前唯一的路线权威。与 README / ARCHITECTURE / TODO 冲突时以本文为准。

## 0. 这一版路线是被数据推翻后重写的

2026-09-03 做完 Gold Session 的 A/B 产品级对照（脱敏公开版见
[docs/AB_EVALUATION.md](docs/AB_EVALUATION.md)，含真实课堂内容的原始报告留在本机
`data/sessions/<session>/comparison/AB_EVALUATION.md`，不进版本库）。同模型（gpt-5.6-sol）、
同音频、同起点，唯一变量是流水线，结论是：

- **B 组（REPAIRED 直接交 GPT 网页，1 轮）没有丢失 A 组抽取到的任何一条知识项**
  （例子 21/21、老师强调 12/12、考试信息 2/2、易错点 6/6）。
- **B 组保留了 A 组丢掉的推导过程与大段课堂叙事**；A 的绪论 17 分钟只剩 322 字正文。
- A 全文 50,726 字符里，`[!question]` 占 52.5%、溯源标注占 30.2%，**真正的笔记正文
  只有 17.4%**。
- 两组幻觉都是 0；两组都没有编造 173 的二进制答案。
- A 的 245 个疑点块**漏掉了唯一一处实质性推断**（把「毕业考试」读成「闭卷考试」，
  `uncertain` 为空），并不比 B 更安全。

**结论：9 轮网页换来的东西，不足以让完整流水线继续当默认路径。**

已经写下的 308 个测试不是沉没成本 —— 它们正是让这个结论可以被证明的原因。
它们验证出的是「哪些复杂度不值得成为默认路径」，这比「流水线终于跑通了」更重要。

## 1. 默认生产路线：Direct GPT Web

```text
phone recording → Syncthing → watcher → local faster-whisper
→ selective repair → transcript_repaired → export-package → GPT Web → final note
```

- **默认路线由本项目负责到 export-package**，之后交给一次网页会话。
- REPAIRED 是本项目不可替代的部分：Whisper 转录、选择性重转录、时间轴、
  segment 切分、课程与时间归档。这一层 GPT 网页做不了。
- 整理提示词以 `comparison/B_prompt.md` 为基线维护。
- 适用：绝大多数课堂。

## 2. High Integrity / Audit Mode：完整 Phase 2

```text
REPAIRED → CLEANED → OUTLINE → KNOWLEDGE → AUDIO DRAFT
```

- **不再是默认路径，改为按需开启的高完整性模式。**
- 代码、测试、校验闸门全部保留，不删。
- 它独有且直接整理给不了的能力只有两项：
  1. **segment 级溯源** —— 每条内容可回到录音的具体位置；
  2. **可审计的修复/删除决策记录** —— 每处改动都有理由和证据。
- 值得开启的场景：
  - 公式/推导密集、需要逐句核对的课；
  - 打算长期进知识库、要求可追溯的课；
  - 对某段内容有争议、需要回到原始录音举证时。
- 不值得开启的场景：日常课堂复习笔记。

## 3. 本轮已完成的修复

### 3.1 derivations 通路（真实 bug，已修）

A 推理丢失的根因不是模型发挥，是 schema 缺口：

- `outline.json`（2B）检出 `derivations`，但只有 label + segment 范围，没有内容；
- `knowledge.json`（2C）**没有** `derivations` 键；
- `audio_draft.json`（2D）section **没有** `derivation_ids`。

2B 检出的推导在 2B→2C 边界被整体丢弃，只有结论以 `equations` 形式幸存。

修复内容：

- 2C 新增 `derivations` 类别：`steps`（≥2 步）、`conclusion`、`status`、来源与 uncertainty；
- outline 的 derivations 现在必须由 `knowledge.derivations` 承接，
  不能再用 equations 的证据区间顶替；
- 2D 新增 `derivation_ids`，走与其他知识项相同的「恰好编排一次」闸门；
- 渲染器新增 `### 推导过程`，逐步输出；不完整推导渲染为 `[!question] 推导尚未核验`;
- 两个 prompt 相应重写。
- schema 版本：knowledge 1→2，draft 1→2。

**向后兼容**：v1 的 `knowledge.json`（没有 `derivations`）读取时按 legacy 处理，
已用冻结的 Gold 实测通过 —— 30 concepts / 8 equations / 201 uncertain / 43 visual
全部照常加载，`derivations` 补空数组。

v1 的 `audio_draft.json` 则会被判为需要重跑（section 缺 `derivation_ids`，
且 schema 变更后 fingerprint 已不同）。这是**干净的拒绝**（`LLMError`，不崩溃），
磁盘上的 Gold 产物不受影响 —— 只有真去跑 `lecture-ai draft` 才会重新生成。
按里程碑约定，不重跑。

### 3.2 产物命名（已修）

- 新 session 目录：`日期_时间_课程_序号`（如 `2026-09-01_0943_digital-electronics_001`），
  光看目录名就知道是哪节课；旧格式目录继续可读。
- 笔记文件名带 session 身份：`<session_id>_audio_draft.md`；
  已存在的 `lecture_audio_draft.md` 沿用旧名，不触发重跑。
- 新增 `lecture-ai relabel <session_id>`：课程识别失败落到 `unknown` 时，
  把课程改对之后让目录名跟上。**已有 Phase 2 产物的 session 拒绝改名** ——
  analysis 产物内部写着 session_id 且彼此用 SHA 串成链，改名会让 provenance 失效。

## 4. 冻结的里程碑

**Gold Pipeline v1**（Phase 1 / 1.5 / 2A / 2B / 2C / 2D 全部生产实跑通过）
连同 Gold Session `2026-09-01_unknown_001` 的七个正式产物一并冻结，作为
A/B 对照的基准。不因后续改动重跑或覆盖。

> 该 session 的目录名保留 `unknown`：它已有完整 Phase 2 产物，改名会让
> provenance 链失效。这是有意保留的历史，不是待办。

## 5. v1.0 收口

### 5.1 Session Export Package（已实现）

- **Session Export Package**（设计见 `DESIGN_EXPORT_PACKAGE.md`）：
  `lecture-ai export-package <session_id>` 把正式 REPAIRED、已明确归属的板书与
  明确提供的课件复制成 GPT Web 投喂包。
- 正式输出目录为
  `paths.export_dir`（见 config.example.yaml）；每节课目录与
  包内文件统一带日期、开始时间、课程名和序号。
- REPAIRED 缺失会硬失败；不回退 RAW。不做 OCR / EXIF / 图片识别 / 自动上传。
- Session `images/`、`slides/` 和 CLI 显式路径才算已归属；其余板书候选只列
  `unassigned` warning，不猜。

### 5.2 唯一非阻塞后续方向

- **Obsidian import**（设计见 `DESIGN_OBSIDIAN.md`）：待 Export Package 经几周真实
  课堂使用后，再评估把已定稿笔记和附件导入 Vault。当前不实现。

### 5.3 已降级 / 未实现

- **Phase 3 visual resolver：NOT IMPLEMENTED。** 不再作为完整 Phase 2 的下一步。板书的价值改由
  Export Package 交给网页会话实现；只有 High Integrity Mode 需要程序化融合时
  才重新评估。
- **Phase 4 Obsidian：NOT IMPLEMENTED。** 降级为远期设计题，先解决「材料怎么进库」，
  不预先建 WikiLink / 概念页 / 知识图谱。

## 6. 明确不做

- 不为了让完整 Phase 2 显得有价值而保留它作为默认路径。
- 不在 Export Package 之前实现任何视觉模型 / EXIF / 图像流水线。
- 不预先构建概念页、课程索引、知识图谱。
