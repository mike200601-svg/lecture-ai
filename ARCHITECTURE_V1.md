# ARCHITECTURE_V1 —— 课堂自动笔记系统 Phase 0～2 架构设计

> 版本：V1
> 日期：2026-08-31
> 覆盖范围：Phase 0（骨架）、Phase 1（音频转录 MVP），以及 Phase 2～4 的接口预留
> 原则：**最小可用优先**，但接口边界必须一次性划对，避免后续推倒重来。

---

## 0. 本机环境实测结论（决定了本文的选型）

| 项目 | 实测值 | 结论 |
|---|---|---|
| OS | Windows 11 Home 26200 | 主目标平台 |
| Python | 3.13.14（项目根 `.venv`） | 正式开发 / 运行解释器；避免 3.14 生态兼容风险 |
| CPU | Intel Core Ultra 5 225H，14 核 / 14 线程 | ASR 主力算力 |
| RAM | 31.5 GB | 充足 |
| GPU | Intel Arc 130T (16GB 共享) + NPU | **无 NVIDIA，无 CUDA** |
| ffmpeg / ffprobe | 9.0.1 full build（winget） | 已完整安装；`imageio-ffmpeg` 仅保留为降级方案 |
| 磁盘 | D: 剩余 294 GB | 录音归档无压力 |
| 项目路径 | `D:\原创项目\课堂自动笔记项目`（含中文） | **全链路必须强制 UTF-8** |

### 0.1 由此产生的两个重大偏离（相对总 Prompt）

**偏离 1：ASR 不能用 CUDA。**

总 Prompt 写的是「如果本机 NVIDIA GPU 性能足够，优先 faster-whisper + `device: cuda` + `large-v3`」。
本机没有 N 卡，该前提不成立。实测选型：

```yaml
transcription:
  provider: local_whisper
  local_whisper:
    model: large-v3-turbo   # 不是 large-v3
    device: cpu             # 不是 cuda
    compute_type: int8      # CPU 上的量化推理
```

理由：

- `large-v3` 在 14 核 CPU 上跑 90 分钟录音，预计 2.5～5 小时，**不可接受**；
- `large-v3-turbo`（4 层 decoder）速度约为 large-v3 的 5～8 倍，转录质量几乎持平
  （仅翻译任务退化，而我们只做转录），预计 90 分钟录音 **20～40 分钟**出结果，可接受；
- `int8` 量化在 CPU 上比 `float32` 快 2～3 倍，内存占用降到约 1.5 GB；
- Intel Arc / NPU 走 OpenVINO 理论上更快，但需要另一套 runtime 和模型转换链路，
  **属于过度工程化，V1 不做**，仅在 Transcriber 抽象层预留 provider 位置。

这条偏离不影响架构，只影响默认配置值——正因为总 Prompt 要求「具体模型不要硬编码」，
换机器（例如以后配了 N 卡）只需要改 config.yaml。

**偏离 2：包结构加一层 `lecture_ai/`。**

总 Prompt 建议 `src/ingestion/`、`src/session/` 直接平铺。V1 改为 `src/lecture_ai/ingestion/`：

- 平铺会让 `import session` 与标准库/第三方包重名，Windows 上尤其容易踩坑；
- 加一层后可以 `pip install -e .`，CLI 入口点、测试导入、后续打包全部干净；
- 模块名称与总 Prompt 完全一致，只是多了一个命名空间前缀。

---

## 1. Phase 0～2 总体架构

```text
┌──────────────────────────────────────────────────────────────┐
│                        CLI / Watch Service                   │
│ watch │ scan │ probe │ process │ status │ retry │ reindex │ doctor │
└───────────────────────────┬──────────────────────────────────┘
                            │
┌───────────────────────────▼──────────────────────────────────┐
│                        Pipeline 编排层                        │
│   按 Session 状态机驱动，每一步都可断点续跑（幂等 + 缓存）      │
└───┬─────────┬──────────┬──────────┬──────────┬───────────────┘
    │         │          │          │          │
┌───▼───┐ ┌───▼────┐ ┌───▼─────┐ ┌──▼─────┐ ┌──▼──────────┐
│ingest │ │session │ │  audio  │ │transcr.│ │ llm(Phase2) │
│       │ │        │ │         │ │        │ │ fusion(P3)  │
│稳定检测│ │状态机   │ │ffmpeg   │ │ASR抽象 │ │ obsidian(P4)│
│去重hash│ │元数据   │ │16k/mono │ │glossary│ │             │
└───┬───┘ └───┬────┘ └───┬─────┘ └──┬─────┘ └──┬──────────┘
    │         │          │          │          │
┌───▼─────────▼──────────▼──────────▼──────────▼──────────────┐
│           database (SQLite) + config + logging + utils       │
└──────────────────────────────────────────────────────────────┘
```

### 1.1 分层职责

| 层 | 职责 | 绝对禁止 |
|---|---|---|
| CLI | 参数解析、调用 pipeline、打印人类可读结果 | 写业务逻辑 |
| Pipeline | 状态机推进、错误捕获、重试、缓存判定 | 直接调 ffmpeg / whisper |
| 领域模块 | 单一职责（ingest / audio / asr / ...） | 跨模块直接调用彼此内部函数 |
| 基础设施 | config / db / log / utils | 依赖任何领域模块 |

依赖方向**严格单向向下**，不允许反向 import。

> 实现期间踩过一次：watcher 最初放在 `ingestion/`，但它要编排「扫描 + 处理」，
> 必然依赖 pipeline，于是形成 `ingestion → pipeline → ingestion` 的环。
> 已把 watcher 归到 pipeline 层（它本来就是编排职责），并加了两个测试守着这条规则：
> 逐模块独立 import、领域层不得 import 上层。

---

## 2. 文件结构（Phase 0 落地）

```text
课堂自动笔记项目/
├─ src/lecture_ai/
│  ├─ __init__.py            # 版本号
│  ├─ __main__.py            # python -m lecture_ai
│  ├─ cli.py                 # argparse 子命令
│  ├─ config.py              # config.yaml + .env 加载，dataclass 化
│  ├─ logging_setup.py       # RotatingFileHandler + UTF-8 控制台
│  ├─ errors.py              # 统一异常层次
│  │
│  ├─ ingestion/
│  │  └─ scanner.py          # 发现新文件 / 稳定性判定 / SHA256 去重
│  │
│  ├─ session/
│  │  ├─ models.py           # SessionState 枚举 + SessionMeta dataclass
│  │  ├─ manager.py          # 创建/加载/保存 session，状态迁移
│  │  └─ courses.py          # courses.yaml 解析 + 按时间表推断课程
│  │
│  ├─ audio/
│  │  ├─ ffmpeg.py           # 定位二进制 / probe / 转码 / 切片
│  │  └─ preprocess.py       # 预处理编排，产出 audio_16k.wav
│  │
│  ├─ transcription/
│  │  ├─ base.py             # Transcriber ABC + 数据模型
│  │  ├─ registry.py         # provider 名 -> 实现，按 config 构造
│  │  ├─ faster_whisper_transcriber.py
│  │  ├─ openai_transcriber.py
│  │  ├─ fake.py             # 测试用，零依赖
│  │  ├─ glossary.py         # 术语词典 -> hotwords / initial_prompt
│  │  └─ writer.py           # transcript_raw.json / .md 落盘
│  │
│  ├─ pipeline/
│  │  ├─ phase1.py           # ingest -> session -> audio -> asr -> output
│  │  ├─ diagnostics.py      # 手机录音 metadata / start_time 只读诊断
│  │  └─ watcher.py          # 长驻轮询服务 + 单实例锁
│  │
│  ├─ database/
│  │  ├─ schema.sql
│  │  └─ db.py               # 连接管理 + 仓储方法
│  │
│  ├─ llm/             (Phase 2 占位)
│  ├─ image_processing/(Phase 3 占位)
│  ├─ fusion/          (Phase 3 占位)
│  ├─ obsidian/        (Phase 4 占位)
│  └─ utils/           hashing / paths / timefmt / slug
│
├─ config/
│  ├─ config.yaml
│  ├─ courses.yaml
│  └─ glossary/
│     ├─ common.txt
│     ├─ quantum_mechanics.txt
│     └─ electrodynamics.txt
├─ prompts/            (Phase 2 用，V1 先放骨架)
├─ data/
│  ├─ incoming/audio/   incoming/images/
│  ├─ sessions/<session_id>/
│  ├─ processed/        # 已消费的原始文件归档（不删除）
│  └─ cache/            # 模型缓存等
├─ tests/
├─ scripts/
├─ logs/
├─ ARCHITECTURE_V1.md  TODO_PHASE1.md  STATUS.md  README.md
├─ pyproject.toml  .env.example  .gitignore
```

### 2.1 单个 Session 目录布局

```text
data/sessions/2026-09-03_quantum-mechanics_001/
├─ metadata.json          # 唯一权威状态文件（SQLite 是索引，不是真相源）
├─ raw/
│  └─ 录音_20260903.m4a   # 原始录音（只读，永不覆盖 / 删除）
├─ audio/
│  ├─ audio_16k.wav       # ASR 输入
│  └─ chunks/             # 可选切片
├─ transcript/
│  ├─ transcript_raw.json # 带时间戳（Phase 1 核心产物）
│  └─ transcript_raw.md   # 人类可读
├─ images/                # Phase 3
├─ analysis/              # Phase 2/3 中间产物（clean / outline / fusion）
├─ note/                  # Phase 2 最终笔记
└─ logs/session.log       # 该 session 的处理日志
```

**真相源设计**：`metadata.json` 是每个 session 的权威状态；SQLite 是全局索引与去重表。
即使 SQLite 损坏或被删除，也能通过扫描 sessions 目录重建索引——这是可恢复设计的底线。

---

## 3. 数据流

### 3.1 Phase 1 主数据流

```text
手机录音 ──同步──> data/incoming/audio/xxx.m4a
                          │
                    [1] scanner
                    ├─ 文件稳定性：连续 N 次轮询 size + mtime 不变
                    ├─ SHA256 计算
                    └─ 查 files 表：已处理则跳过
                          │
                    [2] session manager
                    ├─ 推断录音起始时间（见 3.2）
                    ├─ 按 courses.yaml 时间表匹配课程
                    ├─ 生成 session_id: 2026-09-03_quantum-mechanics_001
                    ├─ 原文件 move 至 session/raw/（保留原名）
                    └─ 状态 NEW -> AUDIO_READY
                          │
                    [3] audio preprocess
                    ├─ ffprobe 取时长 / 采样率 / 声道
                    ├─ ffmpeg -> 16kHz / mono / pcm_s16le
                    ├─ 可选 loudnorm 音量归一化
                    └─ 可选切片（默认关闭，超阈值自动启用）
                          │
                    [4] transcription      状态 -> TRANSCRIBING
                    ├─ 载入 course glossary -> hotwords
                    ├─ Transcriber.transcribe(path) -> TranscriptResult
                    └─ 失败 -> FAILED（保留已有产物，可 retry）
                          │
                    [5] writer             状态 -> TRANSCRIBED
                    ├─ transcript_raw.json（segments + 时间戳，必须）
                    └─ transcript_raw.md
```

### 3.2 录音起始时间推断（Phase 3 照片对齐的前置条件）

照片时间戳融合依赖 session 的绝对起始时间，因此 Phase 1 就必须把它算准。
按优先级依次尝试：

1. **容器元数据**：`ffprobe` 读 `format_tags.creation_time`（多数手机录音机会写）；
2. **文件名模式**：`录音_20260903_140000`、`20260903-140000`、`REC_2026-09-03 14.00.00` 等正则；
3. **mtime − duration**：多数录音 App 在停止时落盘，故 `mtime` ≈ 结束时间；
4. **ctime 兜底**，并在 metadata 标记 `start_time_confidence: low`。

置信度会写进 metadata，Phase 3 若为 `low` 则要求人工确认，避免照片错配。
可用 `lecture-ai probe <audio>` 在建 Session 前只读检查 duration、codec、采样率、声道、
creation_time、mtime、ctime 以及最终推断结果；该命令不会转码或修改原文件。

### 3.3 可恢复 / 幂等规则

| 步骤 | 缓存产物 | 已存在时的行为 |
|---|---|---|
| ingest | `files` 表 sha256 | 跳过（同一文件永不二次入库） |
| preprocess | `audio/audio_16k.wav` | 存在且 size > 0 则复用 |
| transcribe | `transcript/transcript_raw.json` | 存在且 schema 合法则复用 |
| （P2）clean | `analysis/transcript_clean.json` | 复用 |
| （P2）outline | `analysis/outline.json` | 复用 |
| （P2）note | `note/*.md` | 复用 |

`retry` 默认从**第一个失败步骤**继续；`--force <step>` 才会重跑指定步骤。
**ASR 结果永远不会因为下游失败而被重跑**——这是总 Prompt 第十三条的硬约束。

---

## 4. Session 模型

### 4.1 状态机

```text
        NEW
         │ 音频入库
         ▼
    AUDIO_READY
         │ 开始转录
         ▼
   TRANSCRIBING ──失败──> FAILED ──retry──┐
         │ 成功                           │
         ▼                                │
    TRANSCRIBED  <──────────────────────-─┘
         │  ← Phase 1 到此为止即算完成
         │ (Phase 3) 照片就绪
         ▼
    IMAGES_READY
         │
         ▼
       FUSING ──失败──> FAILED
         │
         ▼
  GENERATING_NOTE ──失败──> FAILED
         │
         ▼
      EXPORTED
         │
         ▼
        DONE
```

实现要点：

- 状态迁移由 `SessionManager.transition()` 统一把关，非法迁移直接抛 `InvalidTransition`；
- `FAILED` 额外记录 `failed_from`（失败前状态）与 `error`，retry 时恢复到 `failed_from` 再继续；
- Phase 1 阶段，`TRANSCRIBED` 即视为交付完成，后续状态留空。

### 4.2 metadata.json

```json
{
  "schema_version": 1,
  "session_id": "2026-09-03_quantum-mechanics_001",
  "course": {"key": "quantum_mechanics", "name": "量子力学", "teacher": null},
  "date": "2026-09-03",
  "start_time": "2026-09-03T14:00:00+08:00",
  "end_time": "2026-09-03T15:32:11+08:00",
  "start_time_source": "ffprobe|filename|mtime-duration|ctime",
  "start_time_confidence": "high|medium|low",
  "state": "TRANSCRIBED",
  "failed_from": null,
  "error": null,
  "audio": {
    "raw": "raw/录音_20260903.m4a",
    "sha256": "...",
    "duration_sec": 5531.4,
    "processed": "audio/audio_16k.wav"
  },
  "images": [],
  "steps": {
    "ingest":     {"status": "done", "at": "...", "elapsed_sec": 2.1},
    "preprocess": {"status": "done", "at": "...", "elapsed_sec": 41.7},
    "transcribe": {"status": "done", "at": "...", "elapsed_sec": 1633.2,
                   "provider": "local_whisper", "model": "large-v3-turbo"},
    "clean":      {"status": "pending"},
    "note":       {"status": "pending"},
    "obsidian":   {"status": "pending"}
  },
  "created_at": "...", "updated_at": "..."
}
```

字段命名保持与总 Prompt 1.2 的 `transcription_status / fusion_status / note_status /
obsidian_status` 语义一致，但收拢进 `steps` 字典，避免后续每加一步就改一次顶层 schema。

---

## 5. SQLite Schema

```sql
PRAGMA journal_mode=WAL;    -- 允许 watch 进程与 CLI 并发读

CREATE TABLE schema_version (version INTEGER NOT NULL);

CREATE TABLE courses (
  key        TEXT PRIMARY KEY,        -- quantum_mechanics
  name       TEXT NOT NULL,           -- 量子力学
  teacher    TEXT,
  semester   TEXT,
  glossary   TEXT,                    -- glossary 文件名
  created_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id           TEXT PRIMARY KEY,      -- 2026-09-03_quantum-mechanics_001
  course_key   TEXT REFERENCES courses(key),
  date         TEXT NOT NULL,
  start_time   TEXT,
  end_time     TEXT,
  state        TEXT NOT NULL,
  failed_from  TEXT,
  error        TEXT,
  dir          TEXT NOT NULL,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE INDEX idx_sessions_state ON sessions(state);
CREATE INDEX idx_sessions_date  ON sessions(date);

CREATE TABLE files (
  sha256      TEXT PRIMARY KEY,       -- 去重主键：同内容文件只处理一次
  path        TEXT NOT NULL,          -- 归档后的最终路径
  orig_name   TEXT,
  type        TEXT NOT NULL,          -- audio | image
  size        INTEGER NOT NULL,
  timestamp   TEXT,                   -- 文件自身时间（录音起始 / 拍照 EXIF）
  session_id  TEXT REFERENCES sessions(id),
  created_at  TEXT NOT NULL
);
CREATE INDEX idx_files_session ON files(session_id);

CREATE TABLE processing (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id  TEXT NOT NULL REFERENCES sessions(id),
  step        TEXT NOT NULL,          -- ingest|preprocess|transcribe|clean|note|obsidian
  status      TEXT NOT NULL,          -- pending|running|done|failed
  provider    TEXT,
  model       TEXT,
  started_at  TEXT,
  finished_at TEXT,
  elapsed_sec REAL,
  error       TEXT,
  UNIQUE(session_id, step)
);
```

设计取舍：

- `files.sha256` 作主键，天然满足「必须避免重复处理同一个文件」；
- `processing` 用 `UNIQUE(session_id, step)` + upsert，天生幂等；
- 不建 `transcripts` 表——转录文本存文件，DB 只存路径与状态。**大文本不入库**，
  否则后期 Phase 6 做检索时会与向量库职责冲突。

---

## 6. ASR 接口设计

```python
# transcription/base.py
@dataclass(frozen=True)
class TranscriptSegment:
    start: float            # 秒，相对录音起点
    end: float
    text: str
    no_speech_prob: float | None = None
    avg_logprob: float | None = None

@dataclass(frozen=True)
class TranscriptResult:
    segments: list[TranscriptSegment]
    language: str | None
    duration_sec: float | None
    provider: str
    model: str
    extra: dict                     # provider 特有信息，不污染通用字段

@dataclass(frozen=True)
class TranscribeOptions:
    language: str | None = None     # None = 自动检测
    hotwords: str | None = None     # 术语词典拼接
    initial_prompt: str | None = None
    vad_filter: bool = True
    beam_size: int = 5
    temperature: float = 0.0

class Transcriber(ABC):
    name: str
    model_name: str

    @abstractmethod
    def transcribe(self, audio_path: Path,
                   options: TranscribeOptions | None = None,
                   progress: Callable[[float, float], None] | None = None
                   ) -> TranscriptResult: ...

    def close(self) -> None: ...
```

已规划实现：

| provider | 类 | 状态 |
|---|---|---|
| `local_whisper` | `FasterWhisperTranscriber` | Phase 1 主力（CPU / int8） |
| `openai` | `OpenAITranscriber` | Phase 1 实现但默认关闭，受 privacy 开关约束 |
| `fake` | `FakeTranscriber` | 测试专用，零外部依赖 |
| `openvino` / `nvidia` | 预留 | 换机器或做 Intel NPU 加速时再加 |

`registry.build_transcriber(config)` 是唯一构造入口，
上层代码**不允许** import 任何具体实现类——换模型只改 config.yaml。

### 6.1 术语词典接入方式

`config/glossary/<course_key>.txt` + `common.txt`，每行一个词，`#` 起头为注释。

- **hotwords**：faster-whisper 1.x 支持，词表拼成一串（截断到约 200 词，
  过长会挤占 prompt window）；
- **initial_prompt**：兼容云端 API 的降级路径；
- Phase 2 的 AI 纠错会再次使用同一份词表，因此 loader 放在 `transcription/glossary.py`
  供两处复用。

---

## 7. Obsidian / Phase 2～4 接口预留

V1 不实现，但**目录与接口签名先占位**，避免后期改动核心：

```python
# llm/base.py            (Phase 2)
class LLMClient(ABC):
    def complete(self, prompt: str, *, system: str | None = None,
                 max_tokens: int, temperature: float) -> str: ...

# obsidian/base.py       (Phase 4)
class VaultWriter(ABC):
    def write_lecture_note(self, session: SessionMeta, markdown: str) -> Path: ...
    def ensure_concept_stub(self, concept: str, source: str) -> Path: ...
    def update_course_index(self, course_key: str) -> Path: ...
```

预留的关键约定：

- 笔记文本一律以 `str` 在模块间传递，落盘只在 `obsidian` 模块发生；
- `vault_path` 只从 config 读，**任何位置禁止硬编码**；
- Concept 生成受 `concept_threshold` 阈值约束（防「一堂课 147 个概念」的 Obsidian 坟场）；
- Prompt 全部放 `prompts/*.md`，Python 只做变量替换，禁止内联 prompt 字符串。

---

## 8. Windows 环境依赖

### 8.1 运行时依赖

| 依赖 | 版本 | 获取方式 | 必需性 |
|---|---|---|---|
| Python | 3.13.14（项目根 `.venv`） | `py -3.13 -m venv .venv` | 必需 |
| ffmpeg / ffprobe | 9.0.1 full build（已装） | `winget install Gyan.FFmpeg` | 必需（有兜底） |
| faster-whisper | 1.2.1 | pip | 必需 |
| ctranslate2 | 4.8.1（cp314 轮子已验证） | pip | 必需（faster-whisper 依赖） |
| PyYAML | 6.x | pip | 必需 |
| python-dotenv | 1.x | pip | 必需 |
| imageio-ffmpeg | 0.5+ | pip | 可选兜底 |
| pytest | 8.x | pip | 开发 |

### 8.2 ffmpeg 定位策略（三级降级）

1. `config.yaml` 里的 `audio.ffmpeg_path` 显式指定；
2. 系统 `PATH` 中的 `ffmpeg` / `ffprobe`；
3. `imageio_ffmpeg.get_ffmpeg_exe()` 提供的静态二进制（**注意：它不带 ffprobe**，
   因此 `probe_audio()` 必须能在无 ffprobe 时退化为解析 `ffmpeg -i` 的 stderr 输出）。

三级都失败 → 抛出带安装指引的 `DependencyMissing`，由 `lecture-ai doctor` 统一体检。

### 8.3 Windows 专属坑（已在设计中规避）

| 坑 | 规避方式 |
|---|---|
| 控制台 GBK 编码，中文 / 日志乱码 | 全链路显式 `encoding="utf-8"`，日志 handler 指定 UTF-8 |
| 项目路径含中文「原创项目」 | 所有路径用 `pathlib.Path`，子进程传 list 而非字符串拼接 |
| 路径长度 260 上限 | session 目录名受控，课程 slug 截断至 40 字符 |
| 文件被同步软件占用 | 稳定性检测 + 打开失败重试，不假设一定可读 |
| `move` 跨盘失败 | 用 `shutil.move`，自动降级为 copy + delete |
| CRLF / BOM | 写文件统一 `newline="\n"`，不写 BOM |

---

## 9. 第一版实施步骤

| # | 步骤 | 产出 | 依赖 |
|---|---|---|---|
| 1 | 工程骨架 + pyproject + 目录 | 可 `pip install -e .` | — |
| 2 | config / logging / errors / utils | `lecture-ai doctor` 可跑 | 1 |
| 3 | database schema + db.py | DB 自动建表 | 2 |
| 4 | session models + manager + courses | 可手工创建 session | 3 |
| 5 | ingestion scanner（稳定性 + hash） | `lecture-ai scan` 能发现文件 | 4 |
| 6 | audio ffmpeg + preprocess | 产出 audio_16k.wav | 2 |
| 7 | transcription base + fake + writer | 用 fake 打通全链路 | 4,6 |
| 8 | faster_whisper 实现 + glossary | 真实转录 | 7 |
| 9 | pipeline/phase1 编排 + 状态机接线 | `lecture-ai process` | 5-8 |
| 10 | CLI 全子命令 + watcher（位于 pipeline 层） | `lecture-ai watch` | 9 |
| 11 | 测试（单元 + 端到端 fake） | pytest 全绿 | 1-10 |
| 12 | 真实录音验收 + STATUS.md | Phase 1 完成 | 11 |

**关键节奏**：第 7 步用 `FakeTranscriber` 先把「ingest → session → audio → writer」整条链路
跑通，再接真实 Whisper。这样 ASR 慢（几十分钟）不会拖慢链路调试。

---

## 10. 风险点

| # | 风险 | 概率 | 影响 | 缓解措施 |
|---|---|---|---|---|
| R1 | **CPU 转录慢**，90 分钟录音需 40+ 分钟 | 高 | 中 | 默认 `large-v3-turbo` + int8；提供 `medium` 快速档；后台跑不阻塞用户；先用短样本实测再定档 |
| R2 | Python 依赖生态兼容性 | 低 | 高 | 正式锁定 Python 3.13；`.venv` 已验证，代码不使用仅 3.14 可用语法 |
| R3 | 系统 PATH 中 ffmpeg/ffprobe 丢失 | 低 | 中 | 已安装 full build；`imageio-ffmpeg` 兜底 + `doctor` 明确指引 |
| R4 | 录音起始时间推断错误 | 中 | 高（Phase 3 照片全错位） | 三级推断 + confidence 标记 + `--start-time` 手工覆盖 |
| R5 | 课程自动匹配失败（调课 / 补课） | 高 | 低 | 匹配失败落入 `unknown` 课程，不阻塞转录；提供 `assign` 命令事后归属 |
| R6 | 手机同步中途处理半个文件 | 中 | 中 | 稳定性检测（连续 N 次 size + mtime 不变 + 最小静默期） |
| R7 | 长录音内存溢出 | 低 | 中 | faster-whisper 流式解码；切片能力已实现，超阈值自动启用 |
| R8 | 专业术语识别错误率高 | 高 | 中 | glossary hotwords 缓解；根治靠 Phase 2 的 AI 纠错，**Phase 1 不追求完美文本** |
| R9 | 中文路径 / GBK 控制台导致崩溃 | 中 | 高 | 全链路 UTF-8 强制；测试用例覆盖中文路径场景 |
| R10 | 过度工程化拖慢交付 | 中 | 高 | Phase 2～4 只留接口不写实现；占位模块保持空文件 |

### 10.1 明确不做（V1）

- 手机 App、实时音频传输、任何前端；
- Docker、Redis / MQ / PostgreSQL；
- Intel NPU / OpenVINO 加速；
- 说话人分离（diarization）——单人讲课场景收益低；
- 自动删除任何原始录音或板书。

---

## 11. 验收定义（Phase 1）

```text
真实 60～120 分钟课堂录音
  → 复制进 data/incoming/audio/
  → lecture-ai watch 自动发现（或 lecture-ai scan 手动触发）
  → 自动建 session、转码、转录
  → 产出 transcript_raw.json（含 segment 级时间戳）+ transcript_raw.md
  → 状态机停在 TRANSCRIBED，日志完整，重复 retry 不重跑 ASR
```

以上全部成立，Phase 1 才算稳定，才进入 Phase 2。
