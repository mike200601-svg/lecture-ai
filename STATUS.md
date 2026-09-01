# STATUS

> 最后更新：2026-09-01

## 总览

| Phase | 内容 | 状态 |
|---|---|---|
| Phase 0 | 工程骨架 | ✅ 完成 |
| Phase 1 | 音频自动转录 MVP | 🟡 代码与手机入站部署完成，**真实课堂录音验收进行中** |
| Phase 2 | AI 课堂笔记 | ⬜ 未开始（仅目录与 prompt 骨架） |
| Phase 3 | 板书照片融合 | ⬜ 未开始（仅目录占位） |
| Phase 4 | Obsidian 集成 | ⬜ 未开始（仅目录占位） |
| Phase 5 | 课程知识库 | ⬜ 未开始 |
| Phase 6 | 搜索 / AI 问答 | ⬜ 未开始 |

---

## 2026-09-01：手机自动入站与真实课堂联调

### 正式链路

- 手机：HONOR BVL-AN00 / Android 16，系统录音机 `com.hihonor.soundrecorder`；
  实际录音目录 `/storage/emulated/0/Sounds`，实际格式为 M4A。
- 主同步：Syncthing-Fork 2.1.3.0，文件夹 `lecture-audio` 为 **Send Only**。
- 电脑：Syncthing 2.1.3，接收目录 `data/incoming/audio` 为 **Receive Only**；
  Windows 任务 `LectureAI Syncthing` 登录启动、允许电池运行、失败自动重启。
- 消费端：Windows 任务 `LectureAI Watch` 持续监听 incoming，使用本地 medium / CPU int8
  自动转码和转录。
- 网络：同一 Wi-Fi 下走 TCP 直连；已经隔离本地连接并实测公共 relay 跨网络连接成功。
  手机可保持 v2rayNG，Syncthing 不需要占用 Android VPN 槽。
- Tailscale + 密钥 SFTP 已验证，但因会与 v2rayNG 争用 Android VPN 槽，只保留为手动备用。

### 已完成的验收

- 将 102.243 秒中文物理讲课 M4A 投入手机录音目录，Syncthing 手机→电脑用时
  **11.41 秒**；两端 SHA-256 均为
  `78ebd4ec9d7cfc4ec6eaba5687ad1f2863a9181130f40d54f89b7ab32d114a4c`。
- watcher 无人工干预创建 `2026-09-01_electrodynamics_001`，预处理后由本地 medium
  转录为 25 段，生成 JSON / Markdown，状态为 `TRANSCRIBED`；本轮 ASR 用时 76.05 秒。
- 分块写入回归实测：70 秒、超过 2 MiB 的 WAV 在写入中不会建 Session，连续两次稳定后
  才进入管道；测试通过（1 passed in 2.13s），生产代码无需为该测试改动。
- 发现 `keep_incoming: false` 会形成“watcher 移走文件 → Syncthing 再下载”的循环，
  已改为 `true`。Session 复制原始音频，incoming 保留同步源，SHA-256 数据库负责去重。
- 真实课堂录音 `Recorder - 20260901-0943.m4a` 已自动到达 incoming。录制尚未结束时，
  中间快照缺少 M4A 最终 `moov`，probe 会拒绝且不会创建坏 Session；停止录音、文件封口后
  watcher 会自动重试。这验证了“半文件不转录”的实际保护链路。

### 运行保障

- 手机 Syncthing-Fork 已加入电池优化白名单并允许后台、移动数据、计量 Wi-Fi；手机熄屏
  不影响同步。不要同时启动 Tailscale 与 v2rayNG，也不要设置 Android 全局代理。
- 电脑交流电：屏幕 10 分钟后关闭，睡眠与休眠均关闭，合盖动作设为“不执行操作”。
  电池策略没有被扩大修改；长时间无人值守应插电。
- RustDesk 1.4.9 已从官方 release 安装到手机和电脑；Windows 服务为 Automatic / Running。
  Android 端仅作为控制客户端。还需用户本人在电脑界面设置永久访问密码，任何脚本和文档
  都不保存该密码。

Phase 2 仍未开始。

---

## 2026-08-31：接手审计与 Phase 1 收尾

- Git 已初始化；`main` 分支基线提交为 `25bac8b phase1 engineering baseline`。
- 正式环境已迁移到项目根 `.venv`：Python 3.13.14；`pip check` 无依赖冲突。
- 完整测试为 **206 passed**；不依赖 GPU、网络或真实模型。
- 已通过 winget 安装 FFmpeg / ffprobe 9.0.1 full build；doctor 优先从 PATH 使用完整版。
- 新增 `lecture-ai probe <audio>`，只读检查手机录音 metadata 与起始时间推断。
- 起始时间优先级已统一为：ffprobe creation_time → filename → mtime-duration → ctime。
- doctor 现在分别显示 tiny / medium / large-v3-turbo 和配置中的本地目录状态、来源、大小与路径；
  本地模型还会校验核心文件并实际初始化，当前配置模型不可用时明确返回 FAIL。
- 代理 `127.0.0.1:7897` 已通过 TCP、curl、Python httpx 与 HuggingFace Hub API 验证；
  使用 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，禁用 Xet 的链路也已测试。
- 模型状态：tiny 缓存 ready；Hugging Face medium / large-v3-turbo 缓存仍 partial；
  用户手动放入的 `models/faster-whisper-medium` 为 **ready**，已通过 CPU/int8 初始化与实际转录。

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

**测试**：206 个，`pytest -q` 全绿。不依赖 GPU / 模型 / 网络。

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
  → transcript_raw.json（21 段，带时间戳）+ transcript_raw.md
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
   当前改用本地 `medium` + CPU/int8。接口未变，换机器只改 config.yaml。
2. **包结构加一层 `lecture_ai/`**：避免 `import session` 与标准库/第三方包重名，
   同时支持 `pip install -e .`。模块划分与总 Prompt 完全一致。

---

## 待办

### 阻塞项：等待当前真实课堂录音结束并完成抽检

Phase 1 剩下的两项验收必须用真实数据，合成语音替代不了：

**手机录音探测：RECORDING IN PROGRESS。** 真实 M4A 已经自动同步中；录制停止、容器封口后
将自动完成 metadata 探测与 medium 转录。

- **T8.3 真实录音验收** —— 一段真实的 60～120 分钟课堂录音，放进
  `data/incoming/audio/`，跑 `python -m lecture_ai watch`。
  验收点：全程无人工干预、时间戳误差 < 2 秒、重复 retry 不重跑 ASR。
- **T8.4 性能实测** —— 真实课堂有环境噪声、回声、多人说话，
  转录速度和质量都会与合成语音不同，需要重测才能最终确认 `medium` 档位。
  本地 medium 已可用，不再受该模型下载阻塞；large-v3-turbo 对比档仍为 partial。

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
| 本地 medium | 3.046x | 约 29.5 分钟 | 合成样本语义基本可读；薛定谔、概率密度、归一化、本征值、厄米算符正确，但 ψ / 模方 / 无限深势阱 / 谐振子仍有错词 |
| large-v3-turbo | **未测成** | | Hugging Face 缓存仍 partial |

> 注 1：合成语音吐字清晰、无噪声，实时倍率会明显优于真实课堂录音。
> 注 2：medium 实测音频 102.243 秒，ASR 33.56 秒，完整 pipeline 36.66 秒，共 21 段；
> 未见整句大量缺失、重复块或明显幻觉，但仍需真实课堂噪声环境验收。

### ⚠️ 模型下载状态

`127.0.0.1:7897` 已确认是可用的 HTTP/Mixed 代理：HuggingFace API 返回 200，
1 MiB CDN range 请求也能完成；直连 HuggingFace 则超时。问题集中在长时间大权重传输：
单连接速度过低，Xet/续传也出现长时间不增长，因此本轮主动停止，避免反复下载数小时。

Hugging Face 缓存中的 `medium` 和 `large-v3-turbo` 仍不完整，但本地 medium 已绕开下载链路。
已做的应对：

- `doctor` 分别报告 tiny / medium / large-v3-turbo 的 ready / partial / missing；
- 模型加载失败的报错里直接给出 `HF_ENDPOINT=https://hf-mirror.com` 等三条对策；
- `.env.example` 里预留了 `HF_ENDPOINT`。

当前项目缓存：

- `tiny`：ready，可用于测试 / smoke test，不得用于正式课堂；
- Hugging Face `medium`：partial；
- 本地 `models/faster-whisper-medium`：ready，核心文件齐全，`model.bin` 1,527,906,378 bytes；
- Hugging Face `large-v3-turbo`：partial。

当前正式候选使用本地 medium，无需联网。`doctor` 会实际初始化配置模型：

```powershell
python -m lecture_ai doctor        # 看「模型文件」这一项是否变成 OK
```

本地 medium 直接初始化耗时 3.143 秒；doctor 多次初始化约 3 秒。模型目录已由
`.gitignore` 忽略，不会把 1.5 GB 权重提交进 Git。tiny 仍不得用于真实课堂验收。

---

## 下一步

1. 当前真实课堂录音结束并封口 → 自动完成 T8.3 / T8.4 验收
2. 连续 3 次真实课堂使用无需人工干预
3. 以上通过后，进入 **Phase 2（AI 课堂笔记）**
