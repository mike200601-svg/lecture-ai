# 课堂自动笔记与 Obsidian 知识库系统 —— Claude Code 项目总 Prompt

> **Historical Snapshot / 原始立项 Prompt。** 本文保留早期需求，不代表当前实现状态或
> 默认路线；凡与 [ROADMAP.md](ROADMAP.md) 冲突，均以 ROADMAP 为准。

我希望你协助我从零开发一个「大学课堂自动记录 → AI整理 → Obsidian知识库」系统。

请把它当作一个长期可维护的软件工程，而不是一次性脚本。

但必须遵循一个原则：

> 优先做最小可用版本，不要一开始过度工程化。

项目最终目标是：

手机上课时只负责：

1. 开始录音；
2. 上课过程中偶尔拍重要板书；
3. 下课停止录音。

之后尽可能全部自动完成：

```text
手机课堂录音
        +
板书照片
        ↓
自动同步到电脑
        ↓
识别新的课堂 Session
        ↓
音频转文字
        ↓
板书照片处理
        ↓
按照时间戳融合语音和板书
        ↓
AI理解、纠错和知识结构整理
        ↓
生成 Markdown + LaTeX 课堂笔记
        ↓
自动写入 Obsidian Vault
        ↓
维护课程知识索引与知识关联
```

---

# 一、项目背景

主要使用场景是大学理工科课程，尤其是：

- 物理；
- 数学；
- AI / 机器人；
- 其他理工科课程。

因此系统必须特别考虑以下课堂特点：

1. 大量数学公式；
2. 老师会引用板书，例如：
   - “这个式子”
   - “这里”
   - “把这个代进去”
   - “上面的结果”
3. 老师可能只口头解释而不写板书；
4. 老师可能在板书上画：
   - 坐标系；
   - 示意图；
   - 箭头；
   - 电路；
   - 光路；
   - 几何图；
5. 自动语音识别很容易把专业术语识别错误。

因此：

> 不能只依赖语音。

最终系统应该采用：

**Audio + Board Photo + AI Fusion**

的架构。

---

# 二、开发原则

请严格遵循以下原则。

## 2.1 不做实时音频传输

第一版不要实现：

```text
手机实时音频
→ 网络
→ PC
→ 实时ASR
```

原因：

- 没有实际必要；
- 会增加断线、缓冲、网络、后台运行等问题；
- 用户并不需要实时字幕。

正确方式：

```text
手机本地录音
↓
课程结束
↓
同步完整录音文件
↓
PC后台处理
```

---

## 2.2 第一阶段不开发手机 App

手机端优先使用：

- 系统录音机；
- 系统相机。

然后通过文件同步方式将数据传输到电脑。

软件首先只实现 PC 后台。

---

## 2.3 所有功能模块化

禁止写成一个巨型 Python 脚本。

至少拆分为：

```text
ingestion
session
audio
image
transcription
fusion
llm
obsidian
database
config
```

后续应当方便替换：

- ASR模型；
- LLM模型；
- OCR/视觉模型；
- 同步方式；
- Obsidian Vault；
- 笔记模板。

---

# 三、阶段开发路线

整个项目按照以下 Phase 开发。

每一个 Phase：

1. 先检查现有代码；
2. 制定实现方案；
3. 实现；
4. 编写测试；
5. 实际运行验证；
6. 更新 STATUS.md；
7. 确认稳定后再进入下一阶段。

不要一次性实现全部 Phase。

---

# Phase 0 —— 项目骨架

首先创建标准工程结构。

建议：

```text
lecture-ai/
│
├─ src/
│   ├─ ingestion/
│   ├─ session/
│   ├─ transcription/
│   ├─ image_processing/
│   ├─ fusion/
│   ├─ llm/
│   ├─ obsidian/
│   ├─ database/
│   └─ utils/
│
├─ config/
│   ├─ config.yaml
│   └─ courses.yaml
│
├─ prompts/
│   ├─ clean_transcript.md
│   ├─ lecture_note.md
│   └─ fusion.md
│
├─ data/
│   ├─ incoming/
│   ├─ sessions/
│   ├─ processed/
│   └─ cache/
│
├─ tests/
│
├─ scripts/
│
├─ logs/
│
├─ STATUS.md
├─ ARCHITECTURE.md
├─ README.md
└─ requirements.txt / pyproject.toml
```

使用：

Python 3.11+

优先保证：

Windows 能直接运行。

---

# Phase 1 —— 音频自动转录 MVP

这是第一个真正可用版本。

目标：

```text
录音文件
↓
自动发现
↓
转录
↓
生成 Markdown
```

暂时不处理照片。

---

## 1.1 Incoming目录监听

建立：

```text
data/incoming/audio/
```

程序检测新增：

```text
.mp3
.m4a
.wav
.flac
```

文件。

必须判断文件已经写入完成，而不是同步到一半就处理。

可以通过：

- 文件大小稳定；
- 修改时间；
- 延迟检测；

实现。

---

## 1.2 Session机制

每堂课必须建立 Session。

例如：

```text
2026-09-03_quantum-mechanics_001
```

Session metadata：

```yaml
session_id:
course:
date:
start_time:
end_time:
audio:
images:
transcription_status:
fusion_status:
note_status:
obsidian_status:
```

保存：

```text
sessions/<session_id>/metadata.json
```

或 yaml。

---

## 1.3 音频预处理

至少支持：

- FFmpeg 转码；
- 单声道；
- 标准采样率；
- 必要时音量归一化；
- 长录音切片。

保留原始音频。

不得覆盖。

---

## 1.4 ASR接口抽象

必须设计统一接口：

```python
class Transcriber:
    def transcribe(audio_path):
        ...
```

后续可以实现：

```text
LocalWhisperTranscriber
OpenAITranscriber
OtherTranscriber
```

第一优先级：

如果本机 NVIDIA GPU 性能足够，优先支持：

```text
faster-whisper
```

避免每堂课持续产生 API 转录费用。

但必须允许配置切换到云端 ASR。

config.yaml：

```yaml
transcription:
  provider: local_whisper

  local_whisper:
    model: large-v3
    device: cuda

  cloud:
    provider: openai
```

具体模型不要硬编码。

---

## 1.5 专业术语词典

建立每门课程 glossary：

```text
config/glossary/
```

例如：

```text
quantum_mechanics.txt
electrodynamics.txt
```

词典包括：

```text
Schrödinger
薛定谔
Hamiltonian
哈密顿量
Hermitian
厄米
Dirac
狄拉克
```

后续用于：

- ASR提示；
- AI转录纠错。

---

## 1.6 转录输出

至少保存：

```text
transcript_raw.json
transcript_raw.md
```

JSON必须有 timestamp：

```json
{
  "segments": [
    {
      "start": 10.2,
      "end": 18.4,
      "text": "我们这里考虑..."
    }
  ]
}
```

绝对不能只保存纯文本。

时间轴以后需要和板书照片融合。

---

# Phase 1 验收标准

真实使用一次 60～120 分钟课堂录音。

做到：

```text
手机录音
↓
复制到 incoming
↓
自动转录
↓
得到带时间戳 transcript
```

全流程正常。

只有 Phase 1 稳定之后才进入 Phase 2。

---

# Phase 2 —— AI课堂笔记

目标：

```text
原始ASR
↓
专业纠错
↓
知识结构理解
↓
课堂笔记
```

---

# 2.1 禁止直接总结整个 transcript

90分钟 transcript 很长。

必须设计分层处理流程。

例如：

```text
完整 transcript
↓
分块
↓
局部理解
↓
章节识别
↓
章节内容融合
↓
全局整理
```

避免：

```text
90分钟 transcript
↓
一个prompt
↓
祈祷
```

---

# 2.2 Transcript Cleaning

第一层 AI 任务：

只负责：

- 修复明显ASR错误；
- 专业名词；
- 标点；
- 去掉大量无意义口癖；
- 保留老师有意义的话。

禁止这个阶段过度总结。

必须保留：

- 例子；
- 老师强调；
- 考试提示；
- 推导逻辑；
- 常见错误；
- 直觉解释。

---

# 2.3 Lecture Structure Detection

自动识别：

```text
主题
子主题
概念
公式
推导
例题
老师强调
考试提示
```

得到类似：

```json
[
  {
    "title": "波函数的统计解释",
    "start": "00:14:23",
    "end": "00:32:50"
  }
]
```

---

# 2.4 最终课堂笔记格式

生成 Markdown。

必须适用于 Obsidian。

风格应当偏：

**大学理工科结构化讲义**

而不是简单会议纪要。

推荐：

```markdown
---
course: 量子力学
date: 2026-09-03
lecture: 3
tags:
  - 量子力学
---

# 第3讲 波函数

## 本节框架

- 波函数
- Born概率解释
- 归一化
- 期望值

---

## 1. 波函数

...

### 定义

...

### 物理意义

...

### 数学表达

$$
\psi(x,t)
$$

---

## 2. Born概率解释

...

$$
P(x)dx = |\psi(x,t)|^2dx
$$

> [!important]
> 老师特别强调：概率密度是 $|\psi|^2$，不是 $\psi$。

---

### 推导

...

---

### 课堂例题

...

---

> [!warning]
> 常见错误：
> ...

---

## 老师额外补充

...

## 考试相关

...

## 本节知识联系

- [[波函数]]
- [[Born概率解释]]
- [[归一化]]
```

数学公式：

必须使用 LaTeX。

---

# Phase 3 —— 板书照片融合

这是项目重要升级。

手机上课过程中拍照。

照片自动同步：

```text
data/incoming/images/
```

---

# 3.1 时间匹配

照片必须保留：

```text
EXIF时间
```

课堂 Session：

```text
14:00～15:30
```

如果：

```text
IMG_001.jpg 14:24
```

自动加入该 Session：

```text
timestamp = 00:24:00
```

---

# 3.2 图像预处理

可选执行：

- 透视校正；
- 裁剪；
- 对比度增强；
- 去阴影；
- 黑板区域检测。

但第一版不要为了视觉美化投入过多时间。

优先保证原图可用。

---

# 3.3 AI理解板书

不要依赖传统 OCR 识别数学公式作为唯一方案。

板书可能包括：

```text
文字
数学公式
坐标系
箭头
物理示意图
```

因此使用视觉模型理解图片。

对每张照片生成：

```json
{
  "timestamp": "00:24:12",
  "topics": [],
  "equations": [],
  "description": "",
  "possible_context": ""
}
```

---

# 3.4 Audio × Board Fusion

核心逻辑：

例如照片时间：

```text
00:24:12
```

取语音上下文：

```text
00:20:00 ～ 00:28:00
```

交给 AI 联合理解：

```text
Audio Context
+
Board Photo
+
Course Glossary
```

判断：

老师说：

> “代进去就是上面这个结果。”

到底指哪个式子。

---

# 3.5 图像引用

原始板书照片保存至：

```text
ObsidianVault/
Assets/Lectures/
```

Markdown中允许：

```markdown
![[2026-09-03-QM-03-board-01.jpg]]
```

但不要无脑把每张照片塞进笔记。

只有：

- 图很重要；
- AI无法完整重建；
- 示意图；
- 复杂推导；

才嵌入原图。

简单公式优先转成 LaTeX。

---

# Phase 4 —— Obsidian 自动集成

目标：

AI笔记自动成为长期知识库。

配置：

```yaml
obsidian:
  vault_path: D:/Obsidian/MyVault
```

禁止硬编码路径。

---

# 4.1 Vault目录结构

建议：

```text
ObsidianVault/

University/

    量子力学/

        Lectures/
            2026-09-03 第03讲.md

        Concepts/
            波函数.md
            Born概率解释.md
            哈密顿量.md

        Course.md

    电动力学/

        Lectures/
        Concepts/
        Course.md

Assets/

    Lectures/
```

---

# 4.2 Lecture Note 和 Concept Note 分离

非常重要。

课堂笔记：

```text
Lectures/
```

记录：

> 老师这节课讲了什么。

知识笔记：

```text
Concepts/
```

记录：

> 这个知识点本身是什么。

不能混为一谈。

---

# 4.3 自动生成 WikiLinks

例如课堂笔记：

```markdown
[[波函数]]

[[哈密顿算符]]

[[本征态]]
```

程序检查 Concept 是否存在。

如果不存在：

建立 stub：

```markdown
# 哈密顿算符

## 来源

- [[2026-09-03 第03讲]]

## 内容

待整理
```

第一版不要让 AI 自动无限扩展 Concept。

防止：

```text
一堂课
↓
生成147个概念
↓
Obsidian坟场
```

必须设重要性阈值。

---

# Phase 5 —— 课程知识库

逐渐将单节课转化成整门课程知识结构。

建立：

```text
Course.md
```

例如：

```markdown
# 量子力学

## 课程进度

- [[2026-09-01 第01讲]]
- [[2026-09-03 第02讲]]
- [[2026-09-08 第03讲]]

## 知识体系

### 第一章 波函数

- [[波函数]]
- [[Born概率解释]]
- [[归一化]]

### 第二章 算符

...
```

自动更新。

---

# Phase 6 —— 搜索 / AI课程问答

这部分最后实现。

目标：

以后可以查询：

```text
老师什么时候讲过绝热近似？
```

系统找到：

```text
电动力学第11讲
01:03:24
```

并可以返回：

- 原始语音文本；
- 整理后笔记；
- 对应板书；
- 音频时间点。

---

# 六、数据库

建议使用：

```text
SQLite
```

存储：

### Courses

```text
id
name
teacher
semester
```

### Sessions

```text
id
course
date
start
end
```

### Files

```text
path
type
timestamp
hash
session
```

### Processing

```text
transcription
image_analysis
fusion
note_generation
obsidian_export
```

必须避免重复处理同一个文件。

建议使用 SHA256。

---

# 七、状态机

Session使用明确状态：

```text
NEW

↓

AUDIO_READY

↓

TRANSCRIBING

↓

TRANSCRIBED

↓

IMAGES_READY

↓

FUSING

↓

GENERATING_NOTE

↓

EXPORTED

↓

DONE
```

错误：

```text
FAILED
```

支持：

```text
retry
```

防止一次 API 错误整个 Session 报废。

---

# 八、CLI

第一阶段无需 GUI。

提供类似：

```bash
lecture-ai watch

lecture-ai process <session>

lecture-ai status

lecture-ai retry <session>

lecture-ai export <session>
```

后续需要再开发 GUI。

---

# 九、Watch Service

最终希望：

电脑开机后：

```text
lecture-ai watch
```

后台运行。

发现：

```text
新录音
新照片
```

自动处理。

但开发前期应同时支持：

```text
手动触发
```

方便 debug。

---

# 十、配置管理

config.yaml：

```yaml
paths:

  incoming_audio:
  incoming_images:
  session_dir:
  obsidian_vault:

transcription:

  provider:

llm:

  provider:
  model:

vision:

  provider:
  model:

processing:

  auto_process: true

obsidian:

  create_concepts: true
  concept_threshold: 0.8
```

API key：

不得写进 config。

使用：

```text
.env
```

---

# 十一、隐私

课堂录音属于私人学习资料。

默认：

优先本地处理。

尤其：

```text
ASR
```

尽可能本地。

上传云端AI前：

必须保证配置可控制。

未来支持：

```yaml
privacy:

  allow_cloud_audio: false

  allow_cloud_images: true

  allow_cloud_transcript: true
```

---

# 十二、日志

必须使用正式 logging。

不要 print 满天飞。

例如：

```text
logs/lecture-ai.log
```

记录：

```text
session
module
status
elapsed_time
error
```

---

# 十三、可恢复设计

假设已经完成：

```text
ASR
```

但是 AI 整理失败。

retry 时：

禁止重新跑 ASR。

必须从缓存继续。

所有中间结果保存：

```text
transcript
clean transcript
image analysis
fusion
outline
final note
```

---

# 十四、Prompt 独立管理

AI prompts：

禁止硬编码在 Python。

全部放：

```text
prompts/
```

方便后续调整。

例如：

```text
prompts/
clean_transcript.md

prompts/
chapter_detection.md

prompts/
board_analysis.md

prompts/
audio_board_fusion.md

prompts/
lecture_note.md

prompts/
concept_extraction.md
```

---

# 十五、质量优先级

整个系统的优先级：

```text
1 笔记准确
2 不遗漏课堂信息
3 公式正确
4 自动化
5 速度
6 UI美观
```

不要为了：

```text
实时
动画
GUI
```

牺牲内容质量。

---

# 十六、特别禁止事项

不要：

### 1

第一版开发手机 App。

### 2

实现实时语音传输。

### 3

做复杂前端。

### 4

过早 Docker 化全部东西。

### 5

过早引入：

```text
Redis
RabbitMQ
Kafka
PostgreSQL
```

SQLite完全够。

### 6

让一个 LLM prompt 直接处理90分钟全文。

### 7

自动删除原始录音。

### 8

覆盖原始板书。

### 9

把 AI 输出当事实。

必须保留原始 source。

---

# 十七、项目长期目标

整个项目最终应该实现：

```text
上课前：

无需操作系统

↓

进入教室：

手机点录音

↓

课堂：

重要板书随手拍

↓

下课：

停止录音

↓

回宿舍：

无需额外整理

↓

PC：

自动同步
自动识别
自动转录
自动理解
自动融合
自动生成笔记

↓

Obsidian：

课堂笔记已经出现
```

最终用户操作时间：

```text
开始录音
+
停止录音
+
必要时拍照片
```

其余流程尽量自动化。

---

# 十八、最终知识体系

系统最终不是：

```text
录音转文字软件
```

而是：

```text
Personal Academic Memory System
```

也就是：

```text
课堂
│
├─ Audio
├─ Blackboard
└─ Slides
      ↓
Lecture Session
      ↓
Structured Knowledge
      ↓
Obsidian
      ↓
Personal Knowledge Graph
      ↓
AI Tutor
```

未来我应该能够问系统：

```text
老师本学期讲过哪些近似方法？
```

或者：

```text
整理老师讲过的所有关于波函数归一化的内容。
```

或者：

```text
根据老师真正讲过的知识，给我出一份期中考试卷。
```

系统能够从真实课堂数据中回答。

---

# 十九、现在的任务

不要立即实现整个项目。

首先：

## Step 1

阅读本文档。

## Step 2

给出：

```text
ARCHITECTURE_V1.md
```

内容包括：

1. Phase 0～2 的具体架构；
2. 文件结构；
3. 数据流；
4. Session模型；
5. SQLite schema；
6. ASR接口；
7. Obsidian未来接口预留；
8. Windows环境依赖；
9. 第一版实施步骤；
10. 风险点。

## Step 3

生成：

```text
TODO_PHASE1.md
```

将 Phase 1 拆成细粒度任务。

每个任务必须有：

```text
目标
实现
测试方法
验收标准
```

## Step 4

然后再开始编码。

第一阶段唯一核心目标：

> 用真实课堂录音稳定完成：

```text
Audio
↓
Session
↓
ASR
↓
Timestamp Transcript
```

在这个功能稳定之前，不进入板书识别和 Obsidian 自动知识图谱阶段。
