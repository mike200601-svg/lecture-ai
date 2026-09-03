# 课堂自动笔记与 Obsidian 知识库系统

把大学理工科课堂的录音（以后还有板书照片）自动变成结构化的 Obsidian 笔记。

**当前进度：Phase 1/1.5 已完成；Phase 2A Canary 已通过且 Gold 全量网页核验进行中；
Phase 2B/2C/2D 工程已完成，等待 Gold 网页链完成后做真实产物 QA。**

```text
荣耀录音机 → Syncthing-Fork → Session → Whisper RAW → 选择性 REPAIRED
                                               → 分块 LLM CLEANED
                                               → STRUCTURED → KNOWLEDGE → AUDIO_DRAFT
```

Phase 3 与 Obsidian 集成尚未开始。Phase 2 数据分层与边界见
[ARCHITECTURE_PHASE2.md](ARCHITECTURE_PHASE2.md)，任务见
[TODO_PHASE2.md](TODO_PHASE2.md)，进度见 [STATUS.md](STATUS.md)。

---

## 快速开始（Windows）

### 1. 安装

```powershell
# 项目根目录下
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev,asr,ffmpeg]"

# 仅使用可选 OpenAI API provider 时才需要；默认 GPT 网页模式无需 SDK/API key
python -m pip install -e ".[cloud]"

# ffmpeg（二选一）
winget install Gyan.FFmpeg     # 推荐：功能完整，自带 ffprobe，装完重开终端
pip install imageio-ffmpeg     # 备选：无需管理员权限，但不含 ffprobe
```

> `pip install -e .` 后 `lecture-ai` 命令可能不在 PATH 上（pip 会有提示）。
> 用 `python -m lecture_ai` 效果完全一样，本文档统一用后者。

### 2. 体检

```powershell
python -m lecture_ai doctor
```

每一项都会给 OK / WARN / FAIL 和修复建议。全绿再往下走。

### 3. 配置课表

编辑 [config/courses.yaml](config/courses.yaml)，填上真实课表：

```yaml
courses:
  quantum_mechanics:
    name: 量子力学
    glossary: quantum_mechanics.txt
    schedule:
      - weekday: 3          # 1=周一
        start: "14:00"
        end: "15:40"
```

录音进来时会按起始时间自动匹配课程。匹配不上不会失败，只会归入 `unknown`。

### 4. 用起来

```powershell
# 把手机录音复制到 data/incoming/audio/ ，然后：
python -m lecture_ai watch          # 长驻监听，发现新录音自动处理

# 或者手动一步步来（方便 debug）
python -m lecture_ai scan           # 只建 session，不转录
python -m lecture_ai probe <audio>  # 只读检查手机录音 metadata 与起始时间推断
python -m lecture_ai process --all  # 转录所有待处理 session
python -m lecture_ai status         # 看总览
python -m lecture_ai status <session_id>   # 看单个 session 详情
python -m lecture_ai retry <session_id>    # 重试失败的（不会重跑已成功的转录）
python -m lecture_ai repair <session_id>   # 只重转录复读/幻觉可疑窗口
python -m lecture_ai repair <session_id> --dry-run
python -m lecture_ai clean <session_id>    # REPAIRED 优先，缺失时读 RAW
python -m lecture_ai clean <session_id> --dry-run
python -m lecture_ai clean-canary <session_id> --chunks 2 5 9
python -m lecture_ai structure <session_id>          # 只接受正式 CLEANED
python -m lecture_ai structure <session_id> --dry-run
python -m lecture_ai knowledge <session_id>          # 只接受正式 CLEANED + outline
python -m lecture_ai knowledge <session_id> --dry-run
python -m lecture_ai draft <session_id>              # 只接受正式 outline + knowledge
python -m lecture_ai draft <session_id> --dry-run
```

产物在 `data/sessions/<session_id>/transcript/`：

- `transcript_raw.json` —— 带 segment 级时间戳，供 Phase 2/3 使用
- `transcript_raw.md` —— 人类可读版，`[00:24:12] 文本` 逐行
- `transcript_repaired.json/.md` —— Phase 1.5 选择性重转录结果与逐窗决策历史

当前本地 Whisper 默认关闭 ASR `hotwords`：真实口音课堂 A/B 发现长词表会在弱语音处
直接串入术语。glossary 仍保留给 Phase 2 纠错。`repair` 会同时检测复读、超长低文字
密度和 glossary prompt 串入；稀疏恢复产生的新异常会被拒绝并完整留痕。

Phase 2A 产物在同一 Session 的 `analysis/`：

- `transcript_clean.json` —— 正式机器输入，保留时间戳、source SHA、chunk 与 uncertainty
- `transcript_clean.md` —— 人工抽查版
- `clean_cache/` —— 逐块和边界缓存；某次 LLM 处理失败不会重跑已成功块
- `canary/chunk_NNN/` —— 隔离的 RAW/REPAIRED/CLEANED 对照与 GPT 网页交换文件

默认 `llm.provider=chatgpt_web`：运行 `clean-canary` 后，把每个 `prompt.md` 分别粘贴到
GPT 网页的新对话，将严格 JSON 回复保存为同目录 `response.json`，再运行同一命令完成
schema/拓扑/长度校验并生成 `cleaned.json/.md`。Canary 通过前禁止生成 Gold 正式 CLEANED。
prompt 更新时旧 response/cache/CLEANED 会改名封存，不会静默覆盖，也不会被误当成当前
结果。公式纠错继续遵守主 prompt 的“不得猜测”约束，由三片 Canary 与人工抽查把关，
不额外使用会阻止正常上下文纠错的 token 硬锁。
可选 `openai` API provider 仍保留，只有使用它时才从 `.env` 读取 `OPENAI_API_KEY`。

正式网页任务使用统一批处理交换，不再逐项人工搬运。只要某个 Session 已经生成
`analysis/clean_web/`、`structure_web/`、`knowledge_web/` 或 `audio_draft_web/` 任务，
常驻 `watch` 会自动：

1. 把所有未完成 chunk/boundary 合并成带 manifest 和指纹的 ZIP；
2. 将 ZIP 写入 `data/web_exchange/<session>/to_phone/`；
3. 监听 `data/web_exchange/<session>/from_phone/`；
4. 对返回包逐项校验，合格项进正式 cache，不合格项封存并自动生成下一份返工包；
5. 全部通过后自动组装当前阶段产物；生产配置 `processing.auto_advance_phase2: true`
   会随即生成下一阶段任务包，直到 Phase 2D 完成后停在最终 QA。

ZIP 内的 `README.md` 是给 GPT 网页的完整任务说明。把整包上传给 GPT，下载它返回的 ZIP，
原样放进 `from_phone/` 即可；不得修改或丢弃 `manifest.json`。共享目录中的 `state.json`
可直接在手机查看当前批次、待处理项和最终产物状态。

Phase 2B 的 `structure` 命令只读取正式 `analysis/transcript_clean.json`，缺失时直接失败，
不会回退 RAW/REPAIRED。输出为 `analysis/outline.json`：章节必须按顺序恰好覆盖全部 CLEANED
segment；子主题、定义、推导、例题、强调、考试提示和过渡都带来源 id 与时间范围。网页
结果同样进入上述手机整包通道，返回后由 watcher 自动校验并续跑。

Phase 2C 的 `knowledge` 命令只读取同一 Session 的正式 CLEANED 与 `outline.json`，同时校验
两层 SHA 和 provenance。输出 `analysis/knowledge.json` 与 `analysis/unresolved_visual.json`；
所有概念、公式、例题和强调都必须引用来源 segment，模型不得补全音频中残缺的公式。
CLEANED 中的不确定项与视觉引用必须全部进入显式队列，留给人工或 Phase 3 解决。网页结果
沿用同一手机 ZIP 协议，严格校验后自动落盘并停在 `ready_for_phase2c_qa`。
独立运行时该状态可单独验收；启用自动推进后会直接继续生成 Phase 2D 手机任务包。

Phase 2D 的 `draft` 命令只读取通过来源校验的 STRUCTURED、KNOWLEDGE 与视觉未决队列。
GPT 网页只返回严格的章节编排 JSON，不直接自由输出 Markdown；本地渲染器确定性生成
`note/lecture_audio_draft.md`，并把机器审计载荷保存为 `analysis/audio_draft.json`。每个
knowledge item 必须恰好出现一次，所有视觉/听辨/残缺公式问题强制显示为 `[!question]`。
草稿 frontmatter 明确标记 `source_layer: AUDIO_ONLY` 和 `final: false`，禁止 WikiLink、概念页、
Vault 写入与教材化补全；网页任务同样走手机 ZIP，完成后停在 `ready_for_phase2d_qa`。

### 5. 手机自动同步（当前正式方案）

手机端使用 Syncthing-Fork，电脑端使用 Syncthing 2.1.3：

```text
Android /storage/emulated/0/Sounds（Send Only）
  → Syncthing 动态发现 / 中继
  → data/incoming/audio（Receive Only）
  → LectureAI Watch
```

网页批处理另用独立的双向 Syncthing 文件夹 `lecture-web-exchange`：

```text
电脑 data/web_exchange（Send & Receive）
  ↔ Syncthing 动态发现 / 中继
  ↔ Android 自选目录（Send & Receive）
```

录音目录继续保持单向，绝不为了传网页作业而修改它。手机首次收到
`lecture-web-exchange` 邀请时，只需接受、选择一个容易找到的本地目录并保持
Send & Receive；之后任务包、返回包和 `state.json` 均自动同步。

- 手机录音机可继续使用原生 M4A，不需要改文件名或手动复制。
- 手机可以熄屏；Syncthing-Fork 已允许后台运行、移动数据和计量 Wi-Fi。
- 电脑必须保持开机且不能睡眠。当前交流电策略为屏幕 10 分钟后关闭、睡眠/休眠关闭；
  交流电合盖动作也是“不执行操作”。
- Windows 任务计划程序中的 `LectureAI Syncthing` 和 `LectureAI Watch` 会在登录时启动，
  也允许在电池供电时运行并自动重启。
- `processing.keep_incoming` 必须保持 `true`。Syncthing 的接收目录是同步真相源；若 watcher
  把源文件移走，Syncthing 会重新下载它。Session 中仍会保留独立的原始录音副本。
- 正在录制的 M4A 可能出现短暂稳定的中间快照，但它没有最终 `moov` 元数据时无法解码；
  pipeline 会在建 Session 前拒绝它并稍后重试，不会产生半截转录。

已经实测不同网络下可经公共 relay 连接，手机上的 v2rayNG 可以继续作为唯一 VPN。
不要再设置 Android 全局 HTTP 代理；Android 同一时间通常只能有一个 VPN 服务。

备用方案是 `scripts/setup_phone_sftp.ps1` 提供的 Tailscale + 密钥 SFTP。它会占用手机的
VPN 槽，因此仅在 Syncthing 故障时手动启用，不作为日常主链路。

远程查看电脑选用 RustDesk（Windows 服务自动启动）。手机与电脑均已安装 1.4.9；首次
无人值守使用前，需要用户本人在电脑端设置一个永久密码，密码不要写进仓库或聊天记录。

---

## 录音文件命名建议

系统按以下优先级推断录音起始时间（决定课程归属，也是 Phase 3 板书对齐的基础）：

1. 音频容器里的 `creation_time`（多数手机录音机会写）。若它与「文件名时间 + 录音时长」接近，系统会判定它是封口/结束时间，改用文件名的开始时间
2. 文件名里的时间戳：`录音_20260903_140000.m4a`、`20260903-140000.mp3` 等
3. 文件修改时间减去时长
4. 文件 ctime（Windows 文件创建时间，置信度标记为 `low`）

如果手机录音机不写元数据，**建议保留文件名里的日期时间**，不要重命名成 `新录音1.m4a`。

---

## 关于本机的模型选择

本机是 Intel Core Ultra 5 225H + Arc 130T 核显，**没有 NVIDIA 显卡**，
所以当前使用已手动下载的本地 `medium` 模型，走 CPU + int8：

```yaml
transcription:
  local_whisper:
    model: models/faster-whisper-medium
    device: cpu
    compute_type: int8
```

相对模型路径按项目根解析；`models/` 已被 `.gitignore` 忽略，权重不会进入 Git。
`doctor` 会检查 `model.bin`、`config.json`、`tokenizer.json`、`vocabulary.txt`，并实际初始化模型。

换机器只改 [config/config.yaml](config/config.yaml)，代码不用动：

```yaml
transcription:
  local_whisper:
    model: large-v3        # 有 N 卡时可以上完整版
    device: cuda
    compute_type: float16
```

想自己实测选档位：

```powershell
python scripts/bench_asr.py <一段10～15分钟真实课堂录音> --models "medium,large-v3-turbo"
```

`tiny` 只用于单元测试和 smoke test，不能作为真实课堂默认模型。运行
`python -m lecture_ai doctor` 可查看 Hugging Face 缓存及配置中的本地模型是
`ready`、`partial` 还是 `missing`，同时显示来源、大小、路径和本地模型加载结果。

---

## 隐私

课堂录音是私人学习资料，**默认全部本地处理**。

```yaml
privacy:
  allow_cloud_audio: false      # 硬闸门：false 时禁止任何云端 ASR
  allow_cloud_images: false
  allow_cloud_transcript: true
```

`allow_cloud_audio: false` 时，即使把 provider 配成 `openai` 也会被直接拒绝并说明原因，
不会「警告一下然后照样上传」。

`allow_cloud_transcript: false` 同样会硬性禁止 Phase 2A 云端文本清洗。Phase 2A 默认只发送
分块转录文字与术语表；原始音频仍留在本机。

若启用可选 API provider，API key 只能写在 `.env`（见 [.env.example](.env.example)）。
写进 `config.yaml` 会被配置加载器直接报错，防止误提交。GPT 网页模式无需 key。

---

## 几条硬规矩

这些在代码里有对应的测试保护：

- **原始录音永不删除、永不覆盖**。同名文件自动加序号，不会顶掉已有文件。
- **RAW 转录永不覆盖**。下游固定走 RAW → REPAIRED → CLEANED，每层保存 source SHA。
- **转录结果必须带时间戳**。只存纯文本是不允许的 —— Phase 3 要靠时间轴对齐板书。
- **retry 绝不重跑已成功的 ASR**。那是最贵的一步，缓存合法就复用。
- **metadata.json 是真相源**，SQLite 只是索引。库删了可以 `python -m lecture_ai reindex` 重建。

---

## 开发

```powershell
python -m pytest -q            # 全部测试（不需要 GPU / 模型 / 网络）
python -m pytest -q -k e2e     # 只跑端到端
```

Phase 1 结构见 [ARCHITECTURE_V1.md](ARCHITECTURE_V1.md)，Phase 1.5/2 结构见
[ARCHITECTURE_PHASE2.md](ARCHITECTURE_PHASE2.md)。
一条硬性约束：**领域层（ingestion / session / audio / transcription / database / utils）
不得反向 import pipeline 或 cli**，有测试专门盯着这件事。
