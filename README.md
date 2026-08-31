# 课堂自动笔记与 Obsidian 知识库系统

把大学理工科课堂的录音（以后还有板书照片）自动变成结构化的 Obsidian 笔记。

**当前进度：Phase 1（音频自动转录）已实现，等待真实课堂录音验收。**

```text
手机录音 → 复制进 incoming → 自动建 Session → 转码 → Whisper 转录 → 带时间戳的 transcript
```

Phase 2（AI 笔记整理）、Phase 3（板书融合）、Phase 4（Obsidian 集成）尚未开始，
模块目录已占位。设计见 [ARCHITECTURE_V1.md](ARCHITECTURE_V1.md)，
任务拆解见 [TODO_PHASE1.md](TODO_PHASE1.md)，进度见 [STATUS.md](STATUS.md)。

---

## 快速开始（Windows）

### 1. 安装

```powershell
# 项目根目录下
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev,asr,ffmpeg]"

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
```

产物在 `data/sessions/<session_id>/transcript/`：

- `transcript_raw.json` —— 带 segment 级时间戳，供 Phase 2/3 使用
- `transcript_raw.md` —— 人类可读版，`[00:24:12] 文本` 逐行

---

## 录音文件命名建议

系统按以下优先级推断录音起始时间（决定课程归属，也是 Phase 3 板书对齐的基础）：

1. 音频容器里的 `creation_time`（多数手机录音机会写，**最可靠**）
2. 文件名里的时间戳：`录音_20260903_140000.m4a`、`20260903-140000.mp3` 等
3. 文件修改时间减去时长
4. 文件 ctime（Windows 文件创建时间，置信度标记为 `low`）

如果手机录音机不写元数据，**建议保留文件名里的日期时间**，不要重命名成 `新录音1.m4a`。

---

## 关于本机的模型选择

本机是 Intel Core Ultra 5 225H + Arc 130T 核显，**没有 NVIDIA 显卡**，
所以走 CPU + int8 量化，模型用 `large-v3-turbo` 而不是 `large-v3`
（后者在 CPU 上跑 90 分钟录音要几个小时）。

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
`python -m lecture_ai doctor` 可分别查看 tiny / medium / large-v3-turbo 的
`ready`、`partial` 或 `missing` 状态及项目缓存位置。

---

## 隐私

课堂录音是私人学习资料，**默认全部本地处理**。

```yaml
privacy:
  allow_cloud_audio: false      # 硬闸门：false 时禁止任何云端 ASR
  allow_cloud_images: true
  allow_cloud_transcript: true
```

`allow_cloud_audio: false` 时，即使把 provider 配成 `openai` 也会被直接拒绝并说明原因，
不会「警告一下然后照样上传」。

API key 只能写在 `.env`（见 [.env.example](.env.example)）。
写进 `config.yaml` 会被配置加载器直接报错，防止误提交。

---

## 几条硬规矩

这些在代码里有对应的测试保护：

- **原始录音永不删除、永不覆盖**。同名文件自动加序号，不会顶掉已有文件。
- **转录结果必须带时间戳**。只存纯文本是不允许的 —— Phase 3 要靠时间轴对齐板书。
- **retry 绝不重跑已成功的 ASR**。那是最贵的一步，缓存合法就复用。
- **metadata.json 是真相源**，SQLite 只是索引。库删了可以 `python -m lecture_ai reindex` 重建。

---

## 开发

```powershell
python -m pytest -q            # 全部测试（不需要 GPU / 模型 / 网络）
python -m pytest -q -k e2e     # 只跑端到端
```

代码结构与分层规则见 [ARCHITECTURE_V1.md](ARCHITECTURE_V1.md) 第 1、2 节。
一条硬性约束：**领域层（ingestion / session / audio / transcription / database / utils）
不得反向 import pipeline 或 cli**，有测试专门盯着这件事。
