# STATUS

> 最后更新：2026-08-31

## 总览

| Phase | 内容 | 状态 |
|---|---|---|
| Phase 0 | 工程骨架 | ✅ 完成 |
| Phase 1 | 音频自动转录 MVP | 🟡 代码完成，**待真实课堂录音验收** |
| Phase 2 | AI 课堂笔记 | ⬜ 未开始（仅目录与 prompt 骨架） |
| Phase 3 | 板书照片融合 | ⬜ 未开始（仅目录占位） |
| Phase 4 | Obsidian 集成 | ⬜ 未开始（仅目录占位） |
| Phase 5 | 课程知识库 | ⬜ 未开始 |
| Phase 6 | 搜索 / AI 问答 | ⬜ 未开始 |

---

## 2026-08-31：接手审计与 Phase 1 收尾

- 当前工作目录不含 `.git`，因此无法核验 branch、提交历史或未提交改动。
- 正式环境已迁移到项目根 `.venv`：Python 3.13.14；`pip check` 无依赖冲突。
- 完整测试为 **202 passed**；不依赖 GPU、网络或真实模型。
- 已通过 winget 安装 FFmpeg / ffprobe 9.0.1 full build；doctor 优先从 PATH 使用完整版。
- 新增 `lecture-ai probe <audio>`，只读检查手机录音 metadata 与起始时间推断。
- 起始时间优先级已统一为：ffprobe creation_time → filename → mtime-duration → ctime。
- doctor 现在分别显示 tiny / medium / large-v3-turbo 的 `ready / partial / missing`、大小与缓存位置；
  当前配置模型不可用时明确返回 FAIL。
- 代理 `127.0.0.1:10808` 已通过 TCP、curl、Python httpx 与 HuggingFace Hub API 验证；
  使用 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，禁用 Xet 的链路也已测试。
- 模型状态：tiny ready；medium partial；large-v3-turbo partial。大权重链路仍不稳定，
  已停止反复下载，当前正式课堂 ASR 模型仍是 blocker。

Phase 1 仍为：**代码完成，等待真实 60～120 分钟课堂录音验收与连续 3 节课验证**。
没有进入 Phase 2。

---

## 2026-08-31：Phase 0 + Phase 1 实现完成

### 已交付

**文档**

- `ARCHITECTURE_V1.md` —— Phase 0～2 架构、数据流、Session 模型、SQLite schema、
  ASR 接口、Windows 依赖、实施步骤、10 项风险点
- `TODO_PHASE1.md` —— Phase 1 细粒度任务拆解（每项含目标 / 实现 / 测试方法 / 验收标准）
- `README.md` —— 安装与使用
- `prompts/*.md` —— Phase 2/3 的 prompt 骨架（记录已确定的约束，实现时再打磨）

**代码**（`src/lecture_ai/`，约 3800 行）

| 模块 | 内容 |
|---|---|
| `config` | YAML + .env 加载，路径解析，**密钥写进 yaml 会直接报错** |
| `logging_setup` | RotatingFileHandler + UTF-8，per-session 日志 |
| `database` | SQLite（WAL），courses / sessions / files / processing 四表 |
| `session` | 状态机、metadata.json 原子写、课表匹配、索引重建 |
| `ingestion` | 文件稳定性判定、SHA256 去重、录音起始时间三级推断 |
| `audio` | ffmpeg 三级降级定位、探测、转码、切片 |
| `pipeline/diagnostics` | 手机录音 metadata 与绝对起始时间只读诊断 |
| `transcription` | Transcriber 抽象 + faster-whisper / OpenAI / Fake 三个实现、术语词典、输出 |
| `pipeline` | Phase1 编排（幂等 + 断点续跑）、watch 服务 |
| `cli` | init / doctor / scan / process / status / sessions / retry / watch / reindex |

**测试**：202 个，`pytest -q` 全绿。不依赖 GPU / 模型 / 网络。

### 实测环境

| 项目 | 实测值 |
|---|---|
| OS | Windows 11 Home 26200 |
| Python | 3.13.14（项目根 `.venv`） |
| CPU | Intel Core Ultra 5 225H（14 核） |
| RAM | 31.5 GB |
| GPU | Intel Arc 130T —— **无 NVIDIA，无 CUDA** |
| ffmpeg / ffprobe | 9.0.1 full build（系统 PATH；`imageio-ffmpeg` 保留为兜底） |

### 真实链路验证

用 Windows SAPI 合成了一段 1 分 42 秒的中文物理讲课音频（含薛定谔方程、玻恩概率解释、
归一化、厄米算符等术语），完整跑通：

```text
data/incoming/audio/录音_20260902_140000.wav
  → scan：解析文件名得到起始时间 2026-09-02 14:00（high 置信）
  → 按课表匹配到「量子力学」，建 session 2026-09-02_quantum-mechanics_001
  → ffmpeg 转码 22050Hz → 16kHz 单声道
  → 载入 78 条专业术语作为 hotwords
  → faster-whisper 转录
  → transcript_raw.json（8 段，带时间戳）+ transcript_raw.md
  → 状态 TRANSCRIBED
```

**可恢复性已实测**：重跑 `process` 耗时从 48.7 秒降到 0.2 秒，未加载模型，
transcript 文件 mtime 未变 —— 确认 retry 不会重跑已成功的 ASR。

### 开发中发现并修复的真实缺陷

1. **循环 import**：`ingestion.watcher` 反向依赖 `pipeline`，违反单向依赖规则。
   已把 watcher 移到 pipeline 层，并加了两个测试盯着这件事
   （逐模块独立 import + 领域层不得 import 上层）。
   测试最初没发现，是因为 pytest 按字母序先导入了 `test_ingestion.py`，把环掩盖掉了。

2. **`scan` 永远扫不到文件**：文件稳定性判定需要连续 2 次采样一致，
   但采样历史只存在进程内，而 `scan` 跑完就退出，永远凑不满 2 次。
   已引入 `one_shot` 模式：一次性命令就地补采样，watch 模式仍靠轮询间隔累积。

3. **`rebuild_index` 外键失败**：重建索引时没有先恢复 courses 表，
   sessions 的外键插不进去。已改为同时恢复 courses 与 files（含 sha256 去重记录）。

4. **管道输出中文乱码**：Windows 下 stdout 被重定向时退回 cp936。已统一强制 UTF-8。

5. **状态倒退隐患**：复用已有转录时无条件把状态设成 TRANSCRIBED，
   等 Phase 3 让 session 走到 IMAGES_READY 之后，重跑 Phase 1 会抛非法迁移。
   已改为只推进「尚未到达 TRANSCRIBED」的 session，并加了回归测试。

### 与总 Prompt 的两处偏离（已在 ARCHITECTURE_V1 §0.1 说明）

1. **ASR 不用 CUDA**：本机无 N 卡，`device: cuda` + `large-v3` 的前提不成立。
   改用 CPU + int8 + `large-v3-turbo`。接口未变，换机器只改 config.yaml。
2. **包结构加一层 `lecture_ai/`**：避免 `import session` 与标准库/第三方包重名，
   同时支持 `pip install -e .`。模块划分与总 Prompt 完全一致。

---

## 待办

### 阻塞项：需要你提供真实课堂录音

Phase 1 剩下的两项验收必须用真实数据，合成语音替代不了：

- **T8.3 真实录音验收** —— 一段真实的 60～120 分钟课堂录音，放进
  `data/incoming/audio/`，跑 `python -m lecture_ai watch`。
  验收点：全程无人工干预、时间戳误差 < 2 秒、重复 retry 不重跑 ASR。
- **T8.4 性能实测** —— 真实课堂有环境噪声、回声、多人说话，
  转录速度和质量都会与合成语音不同，需要重测才能定最终模型档位。
  另外还卡在正式候选模型权重未完整下载（见下方“模型下载状态”）。

### 已完成的环境收尾

- **完整版 ffmpeg / ffprobe 已安装**，可直接读取容器 `creation_time`。
- **Python 3.13 `.venv` 已建立并验证**。
- **metadata probe 已提供**：真实手机录音到手后先运行
  `python -m lecture_ai probe <audio>` 检查绝对时间来源。

### 建议但不阻塞

- 确认 `config/courses.yaml` 里的课表（当前是示例数据）。
- 补充各门课的术语表 `config/glossary/*.txt`（直接影响专业名词识别率）。

---

## 性能实测数据

测试音频：1 分 42 秒中文讲课（SAPI 合成，无噪声）。
CPU：Intel Core Ultra 5 225H / int8 量化 / 14 线程。

| 模型 | 实时倍率 | 90 分钟录音预计耗时 | 术语识别质量 |
|---|---|---|---|
| tiny | 7.26x | 约 12 分钟 | 差：薛定谔→"血定饿"、厄米算符→"恶米算符"、归一化→"规一化"、深势阱→"申事井" |
| medium / large-v3-turbo | **未测成** | | 需要完整权重 + 真实课堂录音 |

> 注 1：合成语音吐字清晰、无噪声，实时倍率会明显优于真实课堂录音。
> 注 2：tiny 的错误率恰好印证了总 Prompt 的判断——ASR 对专业术语不可靠，
> 必须靠 Phase 2 的 AI 纠错兜底，Phase 1 不追求完美文本。

### ⚠️ 模型下载状态

`127.0.0.1:10808` 已确认是可用的 HTTP/Mixed 代理：HuggingFace API 返回 200，
1 MiB CDN range 请求也能完成；直连 HuggingFace 则超时。问题集中在长时间大权重传输：
单连接速度过低，Xet/续传也出现长时间不增长，因此本轮主动停止，避免反复下载数小时。

因此默认模型 `large-v3-turbo` **目前还没跑起来过**。已做的应对：

- `doctor` 分别报告 tiny / medium / large-v3-turbo 的 ready / partial / missing；
- 模型加载失败的报错里直接给出 `HF_ENDPOINT=https://hf-mirror.com` 等三条对策；
- `.env.example` 里预留了 `HF_ENDPOINT`。

当前项目缓存：

- `tiny`：ready，可用于测试 / smoke test，不得用于正式课堂；
- `medium`：partial（尚无完整 `model.bin`）；
- `large-v3-turbo`：partial（尚无完整 `model.bin`）。

**你需要做的**：网络条件允许时（换网络 / 挂代理 / 用可达的镜像）先把模型拉下来：

```powershell
python -m lecture_ai doctor        # 看「模型文件」这一项是否变成 OK
```

拉不下来可以用 tiny 做 pipeline smoke test，但**不得用 tiny 通过真实课堂验收**——
专业术语与语义信息质量不足。

---

## 下一步

1. 你提供真实课堂录音 → 完成 T8.3 / T8.4 验收
2. 连续 3 次真实课堂使用无需人工干预
3. 以上通过后，进入 **Phase 2（AI 课堂笔记）**
