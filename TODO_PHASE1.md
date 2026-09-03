# TODO_PHASE1 —— Phase 1 细粒度任务拆解

> **Historical Snapshot。** Phase 1 已通过真实课堂验收；当前状态与路线只看
> [STATUS.md](STATUS.md) 和 [ROADMAP.md](ROADMAP.md)。

> 目标：**用真实课堂录音稳定完成 `Audio → Session → ASR → Timestamp Transcript`**
> 在这条链路稳定之前，不碰板书识别、不碰 Obsidian、不碰知识图谱。
>
> 状态标记：`[ ]` 未开始 `[~]` 进行中 `[x]` 已完成 `[!]` 阻塞

---

## T0 工程骨架（Phase 0）

### T0.1 目录结构与包初始化 `[x]`

- **目标**：建立 `src/lecture_ai/` 下全部模块目录与 `data/`、`config/`、`prompts/`、
  `tests/`、`scripts/`、`logs/`，Phase 2～4 模块留空占位。
- **实现**：按 ARCHITECTURE_V1 §2 建目录；每个包写 `__init__.py`（占位模块只写一行 docstring
  说明属于哪个 Phase）；`data/` 各子目录放 `.gitkeep`。
- **测试方法**：`python -c "import lecture_ai; print(lecture_ai.__version__)"`。
- **验收标准**：所有模块可被 import，无循环依赖；`data/incoming/audio` 等目录存在。

### T0.2 pyproject.toml 与可编辑安装 `[x]`

- **目标**：`pip install -e .` 后获得 `lecture-ai` 命令。
- **实现**：`pyproject.toml` 用 setuptools + `src` 布局；`[project.scripts]` 声明
  `lecture-ai = "lecture_ai.cli:main"`；依赖分 `core` 与 `dev` 两组，
  faster-whisper 放入可选组 `asr`，保证没装模型也能跑测试。
- **测试方法**：`pip install -e .` → `lecture-ai --help`。
- **验收标准**：命令可用；`pip install -e ".[dev]"` 能装上 pytest。

### T0.3 .gitignore / .env.example `[x]`

- **目标**：确保录音、模型缓存、密钥永不进版本库。
- **实现**：忽略 `data/`（保留 `.gitkeep`）、`logs/`、`.env`、`__pycache__`、`*.db`。
- **测试方法**：`git status --porcelain` 无录音类文件。
- **验收标准**：`.env` 被忽略；`.env.example` 含所有需要的 key 名但无真实值。

### T0.4 Python 3.13 项目环境 `[x]`

- **目标**：正式开发与运行统一使用 Python 3.13，避免 Python 3.14 的生态兼容风险。
- **实现**：项目根创建 `.venv`，安装 `.[dev,asr,ffmpeg]`，不使用仅 3.14 可用语法。
- **测试方法**：`.venv\Scripts\python.exe -m pytest -q` 与 `pip check`。
- **验收标准**：解释器为 Python 3.13；依赖无冲突；完整测试全绿。

---

## T1 基础设施

### T1.1 错误层次 `errors.py` `[x]`

- **目标**：统一异常，便于 pipeline 区分「可重试」与「配置错误」。
- **实现**：`LectureAIError` 基类，派生 `ConfigError`、`DependencyMissing`、
  `IngestError`、`AudioError`、`TranscriptionError`、`InvalidTransition`、`SessionNotFound`。
- **测试方法**：单元测试断言继承关系。
- **验收标准**：所有自定义异常继承自 `LectureAIError`，可被顶层统一捕获。

### T1.2 配置加载 `config.py` `[x]`

- **目标**：单一入口读取 `config/config.yaml` + `.env`，产出类型化配置对象。
- **实现**：
  - PyYAML 读取；`${ENV_VAR}` 插值；路径统一 `Path` 化并相对项目根解析；
  - dataclass：`Config(paths, transcription, audio, llm, vision, processing, obsidian, privacy, logging)`；
  - API key **只从环境变量读**，config.yaml 里出现 key 值则直接报 `ConfigError`；
  - 缺失字段用默认值，不崩溃。
- **测试方法**：临时目录写最小 yaml → 断言解析结果；写入含 `api_key: sk-xxx` 的 yaml → 断言抛错。
- **验收标准**：默认配置可加载；路径全为绝对 `Path`；密钥泄漏被拦截。

### T1.3 日志 `logging_setup.py` `[x]`

- **目标**：正式 logging，禁止 print 满天飞。
- **实现**：
  - `logs/lecture-ai.log` + `RotatingFileHandler(10MB × 5)`，`encoding="utf-8"`；
  - 控制台 handler 强制 UTF-8（Windows GBK 兜底）；
  - 格式含 `time | level | module | session | message`，session 通过
    `LoggerAdapter` 注入，未绑定时显示 `-`；
  - 支持 per-session 日志文件 `sessions/<id>/logs/session.log`。
- **测试方法**：写含中文和 emoji 的日志 → 读回文件断言无乱码。
- **验收标准**：中文日志文件正常；控制台不抛 `UnicodeEncodeError`。

### T1.4 工具函数 `utils/` `[x]`

- **目标**：hash、slug、时间格式、路径安全。
- **实现**：
  - `hashing.sha256_file()` 分块读，1MB chunk；
  - `slug.slugify()` 中文转拼音过重，改为「保留中英数字 + 连字符」并截断 40 字符；
  - `timefmt.hhmmss()` 秒 → `00:24:12`；`parse_dt` / `iso()`；
  - `paths.ensure_dir()` / `safe_move()`（跨盘降级 copy+delete）。
- **测试方法**：已知内容文件的 sha256 与 `hashlib` 直算结果比对；边界值测试 `hhmmss(0)`、`hhmmss(3661)`。
- **验收标准**：全部单测通过；`safe_move` 在目标已存在时不覆盖而是加序号。

---

## T2 数据库

### T2.1 schema.sql + 建表 `[x]`

- **目标**：SQLite 落地 ARCHITECTURE_V1 §5 的 schema。
- **实现**：`schema.sql` 全量 DDL；`db.init()` 幂等执行（`IF NOT EXISTS`）；
  `schema_version` 表写入版本 1；`PRAGMA journal_mode=WAL`、`foreign_keys=ON`。
- **测试方法**：对空目录调 `init()` 两次，断言不报错且表齐全。
- **验收标准**：重复 init 幂等；WAL 生效。

### T2.2 仓储方法 `db.py` `[x]`

- **目标**：封装所有 SQL，上层不写裸 SQL。
- **实现**：`upsert_course` / `insert_session` / `update_session_state` / `get_session` /
  `list_sessions(state=None)` / `file_exists(sha256)` / `insert_file` /
  `upsert_processing(session, step, status, ...)`。连接用上下文管理器，
  `row_factory = sqlite3.Row`。
- **测试方法**：内存库（`:memory:`）跑一遍完整 CRUD；重复 `insert_file` 同 sha256 断言被拒绝/忽略。
- **验收标准**：去重生效；并发（WAL）下 watch + status 同时读不报 locked。

---

## T3 Session 与课程

### T3.1 状态机 `session/models.py` `[x]`

- **目标**：显式状态 + 合法迁移表。
- **实现**：`SessionState(StrEnum)` 含 NEW / AUDIO_READY / TRANSCRIBING / TRANSCRIBED /
  IMAGES_READY / FUSING / GENERATING_NOTE / EXPORTED / DONE / FAILED；
  `ALLOWED: dict[State, set[State]]`；`SessionMeta` dataclass 支持 `to_dict/from_dict`。
- **测试方法**：参数化测试所有合法迁移通过、若干非法迁移抛 `InvalidTransition`；
  `FAILED → 原状态` 的 retry 路径可走通。
- **验收标准**：非法迁移 100% 被拦截；`to_dict → from_dict` 往返无损。

### T3.2 课程配置 `session/courses.py` `[x]`

- **目标**：`courses.yaml` 定义课程与上课时间表，用于自动归属。
- **实现**：
  ```yaml
  courses:
    quantum_mechanics:
      name: 量子力学
      teacher: 张老师
      semester: 2026-秋
      glossary: quantum_mechanics.txt
      schedule:
        - weekday: 3        # 1=周一
          start: "14:00"
          end:   "15:40"
  ```
  `match_course(dt)` 返回最匹配课程；允许 `tolerance_min`（默认 30 分钟）容差；
  无匹配返回 `unknown`。
- **测试方法**：构造周三 14:10 的时间 → 断言匹配 quantum_mechanics；周日 09:00 → unknown；
  边界 13:35（容差内）→ 匹配。
- **验收标准**：匹配逻辑正确；配置缺失时不崩溃，退化为 unknown。

### T3.3 SessionManager `[x]`

- **目标**：session 的创建、加载、保存、状态迁移、序号分配。
- **实现**：
  - `create(course, start_time, ...)` → 生成 `YYYY-MM-DD_<slug>_NNN`，NNN 为当天该课程内递增；
  - `metadata.json` 原子写（写 `.tmp` 再 `os.replace`）；
  - `transition(session, new_state)` 校验合法性 → 更新 metadata + DB；
  - `fail(session, error)` 记录 `failed_from`；`clear_failure()` 供 retry；
  - `rebuild_index()` 从 sessions 目录重建 SQLite（灾难恢复）。
- **测试方法**：同日同课程连建 3 个 session → 断言序号 001/002/003；
  写 metadata 过程中模拟异常 → 断言原文件未损坏。
- **验收标准**：序号不冲突；metadata 原子性；DB 与 metadata 一致。

---

## T4 Ingestion

### T4.1 文件稳定性检测 `[x]`

- **目标**：绝不处理同步到一半的文件。
- **实现**：`is_stable(path)`：记录 `(size, mtime)`，要求连续 `stable_checks`（默认 2）次
  采样一致，且距最后修改 ≥ `quiet_seconds`（默认 10 s），且能以独占方式打开成功。
- **测试方法**：起线程持续写文件 → 断言期间 `is_stable` 为 False；停止写入并等待 → 变 True。
- **验收标准**：写入中的文件 100% 被判为不稳定。

### T4.2 扫描与去重 `scanner.py` `[x]`

- **目标**：发现 `data/incoming/audio/` 的新音频。
- **实现**：按扩展名 `.mp3/.m4a/.wav/.flac/.aac/.ogg/.opus` 过滤（配置化）；
  稳定则算 SHA256；查 `files` 表命中则跳过并记日志；否则产出 `DiscoveredFile`。
- **测试方法**：放 2 个内容相同、文件名不同的音频 → 断言只产生一个 session；
  放 `.txt` → 断言忽略。
- **验收标准**：内容级去重生效；无关文件不误吞。

### T4.3 归档策略 `[x]`

- **目标**：原始文件进 session，永不删除。
- **实现**：`safe_move` 到 `sessions/<id>/raw/`，保留原文件名；
  若配置 `processing.keep_incoming: true` 则改为 copy 且在 `processed/` 记录软链/清单。
- **测试方法**：跑一次 ingest → 断言 incoming 已清空、raw 下文件字节一致（比对 sha256）。
- **验收标准**：**任何情况下原始录音不被删除或覆盖**。

### T4.4 Watcher `pipeline/watcher.py` `[x]`

- **目标**：`lecture-ai watch` 长驻轮询。
- **实现**：轮询间隔 `processing.poll_interval`（默认 15 s，不用 watchdog，避免 Windows
  文件事件噪声）；捕获 `KeyboardInterrupt` 优雅退出；单文件失败不中断循环；
  单实例锁文件防止重复启动。
  **注意**：最终放在 `pipeline/` 而非 `ingestion/` —— 它要编排「扫描 + 处理」，
  放在 ingestion 会造成 `ingestion → pipeline → ingestion` 循环依赖。
- **测试方法**：启动 watcher（短间隔）→ 投入文件 → 断言若干秒内 session 被创建 → 发送中断。
- **验收标准**：可长时间运行不泄漏；Ctrl+C 干净退出；异常写日志后继续。

---

## T5 音频处理

### T5.1 ffmpeg 定位与体检 `audio/ffmpeg.py` `[x]`

- **目标**：三级降级找到可执行文件。
- **实现**：config → PATH → `imageio_ffmpeg`；缓存结果；`ffprobe` 缺失时标记
  `probe_fallback=True`。
- **测试方法**：monkeypatch PATH 为空 → 断言走到 imageio 兜底或抛 `DependencyMissing`
  且错误信息含安装指引。
- **验收标准**：`lecture-ai doctor` 能报告 ffmpeg 来源与版本。

### T5.2 音频探测 `probe_audio()` `[x]`

- **目标**：取时长 / 采样率 / 声道 / creation_time。
- **实现**：优先 `ffprobe -v error -print_format json -show_format -show_streams`；
  无 ffprobe 时解析 `ffmpeg -i` 的 stderr（`Duration: HH:MM:SS.ss`）。
- **测试方法**：用 stdlib `wave` 生成已知 3.0 s WAV → 断言 duration 误差 < 0.1 s。
- **验收标准**：两条路径都能拿到时长。

### T5.3 转码 `preprocess.py` `[x]`

- **目标**：产出 ASR 友好的 `audio_16k.wav`。
- **实现**：`ffmpeg -i in -ac 1 -ar 16000 -c:a pcm_s16le -vn out`；
  可选 `-af loudnorm`（配置 `audio.normalize`）；已存在且有效则跳过；
  原始文件只读打开，绝不写回。
- **测试方法**：对 44.1kHz 立体声样本转码 → probe 断言 16000Hz / 1ch。
- **验收标准**：幂等（二次调用秒返回）；原文件 mtime/sha256 不变。

### T5.4 长音频切片（默认关闭） `[x]`

- **目标**：满足总 Prompt 1.3「长录音切片」，但不给 MVP 添乱。
- **实现**：`split_audio(path, chunk_sec, overlap_sec)` 用 ffmpeg `-ss/-t` 切片；
  仅当 `audio.chunking.enabled` 或时长 > `auto_chunk_threshold_min`（默认 180 分钟）时启用；
  各片转录后按偏移量合并时间戳，重叠区去重。
- **测试方法**：对 10 s 音频用 4 s/1 s 切片 → 断言片数与偏移正确；
  合并后 segment 时间轴单调递增。
- **验收标准**：默认路径不触发切片；启用时时间轴正确。

### T5.5 手机录音 metadata 诊断 `[x]`

- **目标**：在真实课堂验收前确认手机录音是否保留可靠的绝对录制时间。
- **实现**：`lecture-ai probe <audio>` 只读输出 file / duration / codec / sample_rate /
  channels / creation_time / mtime / ctime，以及 inferred_start_time /
  start_time_source / start_time_confidence。
- **测试方法**：用带中文路径的音频和 stub probe 覆盖完整字段、缺失文件与 CLI 注册。
- **验收标准**：不创建 Session、不转码、不修改原文件；推断优先级为
  ffprobe creation_time → filename timestamp → mtime-duration → ctime。

---

## T6 转录

### T6.1 抽象接口 `transcription/base.py` `[x]`

- **目标**：ARCHITECTURE_V1 §6 的接口落地。
- **实现**：`Transcriber` ABC + `TranscriptSegment` / `TranscriptResult` /
  `TranscribeOptions` dataclass（全部 frozen）。
- **测试方法**：实现一个 3 行的子类 → 断言可实例化并返回合法结果。
- **验收标准**：上层只依赖抽象；`TranscriptResult` 可 JSON 序列化。

### T6.2 FakeTranscriber `[x]`

- **目标**：无需模型即可端到端跑通链路与 CI。
- **实现**：按音频时长切成固定长度假 segment，文本可配置。
- **测试方法**：端到端测试用它跑完整 pipeline。
- **验收标准**：不依赖 ffmpeg 与网络。

### T6.3 FasterWhisperTranscriber `[x]`

- **目标**：Phase 1 主力 ASR。
- **实现**：
  - 懒加载 `WhisperModel(model, device, compute_type, cpu_threads, download_root=data/cache)`；
  - 传 `vad_filter`、`beam_size`、`language`、`hotwords`、`initial_prompt`、
    `condition_on_previous_text=False`（防长音频复读）；
  - 流式消费 generator，边转边回调进度（已转秒数 / 总时长）；
  - 模型下载失败 / 显存不足给出可读错误。
- **测试方法**：`tiny` 模型 + 5 s 真实语音样本，断言产出非空 segment 且时间戳递增
  （标记 `@pytest.mark.slow`，默认跳过）。
- **验收标准**：真实录音可完整转录；进度可见；异常不吞。

### T6.4 云端 provider（默认关闭） `[x]`

- **目标**：允许配置切换到云端 ASR，但受隐私开关约束。
- **实现**：`OpenAITranscriber` 读 `OPENAI_API_KEY`；
  **启动前检查 `privacy.allow_cloud_audio`，为 false 直接拒绝**并说明原因。
- **测试方法**：`allow_cloud_audio: false` + provider=openai → 断言抛 `ConfigError`。
- **验收标准**：隐私开关是硬闸门，不是建议。

### T6.5 术语词典 `glossary.py` `[x]`

- **目标**：专业术语进入 ASR 提示。
- **实现**：加载 `common.txt` + `<course>.txt`，去重、去注释、保序；
  `as_hotwords(max_terms=200)`；`as_initial_prompt()` 生成自然语言提示句。
- **测试方法**：写含注释与重复项的词表 → 断言解析结果；超长词表断言被截断。
- **验收标准**：词表变更无需改代码；缺文件不崩溃。

### T6.6 转录输出 `writer.py` `[x]`

- **目标**：产出总 Prompt 1.6 要求的两个文件。
- **实现**：
  - `transcript_raw.json`：`{schema_version, session_id, provider, model, language,
    duration_sec, created_at, segments:[{id,start,end,text,...}]}`；
  - `transcript_raw.md`：`[00:24:12] 文本` 逐行，附 YAML front-matter；
  - UTF-8、`newline="\n"`、原子写。
- **测试方法**：写入后读回校验 JSON schema；断言 `segments` 非空且含 start/end。
- **验收标准**：**绝不只保存纯文本**；时间戳齐全，可供 Phase 3 融合。

---

## T7 Pipeline 与 CLI

### T7.1 Phase1 编排 `pipeline/phase1.py` `[x]`

- **目标**：把 T4～T6 串成可断点续跑的流程。
- **实现**：`process_session(session, force_steps=())`：
  按 preprocess → transcribe → write 顺序，每步先查缓存；
  每步包 try/except → 失败置 FAILED 并记录；
  `processing` 表记录耗时；成功推进状态机。
- **测试方法**：端到端（Fake ASR）跑通；人为删除 transcript 再 retry → 断言只重跑转录；
  人为让 transcribe 抛错 → 断言状态为 FAILED 且 preprocess 产物保留。
- **验收标准**：**retry 绝不重跑已成功的 ASR**。

### T7.2 CLI `[x]`

- **目标**：总 Prompt 第八节的命令集。
- **实现**：argparse 子命令
  `init` / `doctor` / `scan` / `process` / `status` / `sessions` / `retry` / `watch`
  （`export` 在 Phase 4 前为占位并明确提示）；
  `--config` 指定配置路径；`--verbose`；退出码 0/1/2 区分成功/业务失败/用法错误。
- **测试方法**：`lecture-ai --help`、`doctor`、`status` 在空项目上不崩溃。
- **验收标准**：所有子命令有帮助文本；无堆栈直喷用户（除 `--verbose`）。

### T7.3 doctor 体检 `[x]`

- **目标**：新机器 5 秒定位环境问题。
- **实现**：检查 Python 版本、ffmpeg/ffprobe、faster-whisper 是否可 import、
  模型缓存目录可写、config/courses/glossary 是否存在、DB 可写、
  GPU 情况与推荐 compute_type。
- **测试方法**：手动运行观察输出。
- **验收标准**：每项给 OK / WARN / FAIL 与修复建议。

---

## T8 测试与验收

### T8.1 单元测试 `[x]`

- **目标**：核心逻辑有回归保护。
- **实现**：pytest，覆盖 config / 状态机 / 课程匹配 / 稳定性 / 去重 / slug /
  时间格式 / writer / glossary / db。
- **测试方法**：`pytest -q`。
- **验收标准**：全绿；不依赖网络与真实模型；含中文路径用例。

### T8.2 端到端测试（Fake ASR） `[x]`

- **目标**：链路完整性回归。
- **实现**：临时项目根 → 生成 WAV（stdlib `wave`，不依赖 ffmpeg）→ 投入 incoming →
  `scan` → `process` → 断言 transcript 产物与状态。
- **测试方法**：`pytest -q -k e2e`。
- **验收标准**：无外部依赖即可通过。

### T8.3 真实录音验收 `[~]` ← **真实课堂录音正在采集**

> 已用 Windows SAPI 合成的 1 分 42 秒中文讲课音频跑通全链路（见 STATUS.md），
> 但合成语音无噪声、吐字清晰，替代不了真实课堂验收。
> 2026-09-01 已部署 Syncthing 手机自动入站；当前真实课堂 M4A 正在录制。录制中的
> 不完整快照会被解码预检拒绝，不会创建半截 Session，停止录音后由 watcher 自动重试。

- **目标**：Phase 1 的最终验收。
- **实现**：用户把真实 60～120 分钟课堂录音放入 `data/incoming/audio/` → 运行
  `lecture-ai watch`（或 `scan` + `process`）。
- **测试方法**：观察日志与耗时，抽查 transcript 时间戳与内容对齐情况。
- **验收标准**：
  1. 全程无人工干预完成；
  2. `transcript_raw.json` 正常生成；
  3. `transcript_raw.md` 正常生成；
  4. segment 级时间戳存在、单调且合理；
  5. 随机抽查至少 5 个时间点，时间戳与真实录音误差目标 < 2 s；
  6. 重复 `retry` 不重跑已经成功的 ASR；
  7. 原始录音未被覆盖或删除；
  8. Session metadata 与 SQLite 状态一致；
  9. 随机抽取至少 5 个连续 transcript 片段，每段约 1～2 分钟，总抽查长度不少于约 8 分钟；
  10. 人工只读 transcript 应能基本理解授课知识内容。允许少量同音字、术语拼写、标点和
      口语错误；不能频繁出现整句缺失、大段语义错误、明显重复、Whisper hallucination，
      或完全无法理解老师在讲什么。不得以“Phase 2 会纠错”为由放过语义信息丢失。

### T8.4 性能实测与档位确定 `[~]` ← **等待当前真实录音封口并转录**

> `scripts/bench_asr.py` 已实现。tiny 档已测得 7.26x 实时（合成语音）。
> 手动下载的 `models/faster-whisper-medium` 已通过 CPU/int8 初始化，并在同一段 102.243 秒
> 合成讲课音频上完成实际 pipeline 转录：ASR 33.56 秒、3.046x 实时、21 段。
> Hugging Face 缓存中的 medium / large-v3-turbo 仍为 partial，但本地 medium 已不受下载阻塞。

- **目标**：确定本机默认模型档位。
- **实现**：同一段 10～15 分钟真实课堂录音分别跑 `medium` / `large-v3-turbo`，至少记录
  model、device、compute_type、音频时长、转录耗时、实时倍率、可行时的峰值/近似内存，
  并人工评估普通中文、专业术语、严重语义丢失、重复与幻觉。
- **测试方法**：`scripts/bench_asr.py`。
- **验收标准**：写入 STATUS.md，给出默认档位结论（转录耗时应 < 录音时长的 0.5 倍）。

---

## 完成定义（Definition of Done）

Phase 1 全部完成需同时满足：

- [x] T0～T7 全部 `[x]`
- [x] `pytest -q` 全绿（206 项）
- [ ] T8.3 真实录音验收通过
- [x] STATUS.md 更新（含本地 medium 合成样本实测；真实课堂数据待补）
- [ ] 连续 3 次真实课堂使用无需人工干预

**只有以上全部满足，才进入 Phase 2（AI 课堂笔记）。**
