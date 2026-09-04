# lecture-ai · 课堂自动笔记

[![CI](https://github.com/mike200601-svg/lecture-ai/actions/workflows/ci.yml/badge.svg)](https://github.com/mike200601-svg/lecture-ai/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

把大学理工科课堂的录音，变成一份**不漏重点、可以反复看**的课堂笔记。

录音在本机转录（不上传音频），可疑片段自动重转录，然后交给大模型整理成稿。
一节 100 分钟的课，产出的是一份三五万字、带公式和推导过程的 Markdown 笔记，
可以直接扔进 Obsidian 当知识库。

> **English summary** — `lecture-ai` turns university lecture recordings into
> reliable Markdown notes. Audio is transcribed **locally** with faster-whisper
> (never uploaded); suspicious segments are automatically re-transcribed with a
> wider window; the repaired transcript is then turned into a full lecture note
> either by a ChatGPT web session (attachments supported) or by one API call.
> Docs are in Chinese. Windows-first, core logic is cross-platform.

---

## 目录

- [它解决什么问题](#它解决什么问题)
- [它不做什么](#它不做什么)
- [工作原理](#工作原理)
- [快速开始](#快速开始)
- [两条出稿路线](#两条出稿路线)
- [WebUI 面板](#webui-面板)
- [进 Obsidian](#进-obsidian)
- [命令速查](#命令速查)
- [录音与模型](#录音与模型)
- [手机自动同步（可选）](#手机自动同步可选)
- [隐私与伦理](#隐私与伦理)
- [成本](#成本)
- [已知局限](#已知局限)
- [几条硬规矩](#几条硬规矩)
- [开发](#开发)

---

## 它解决什么问题

自己记笔记会漏。老师口头强调的"这个考试要考"、随口举的例子、黑板上推了五分钟
最后擦掉的过程 —— 手速跟不上，注意力也跟不上。

录音能留下全部信息，但**录音是不能复习的**：你不会为了找一句话去听一小时音频。

这个项目做的就是中间那一段：录音 → 可靠文本 → 结构化笔记。

它的核心价值不在"调用了 AI"，而在**转录这一层足够可靠**：

- 本地 faster-whisper 转录，音频不出本机；
- 自动检出复读、幻觉、疑似漏识的片段，**只对这些片段**用更大的窗口重转录；
- 保留时间轴和 segment 切分，任何一句都能回到录音的具体位置；
- 按课表自动归属课程，每节课一个 session 目录，产物带日期时间课程名。

这一层没有现成工具替你做，而它是后面一切的地基 —— 转录里"毕业考试/闭卷考试"
分不清，整理出来的笔记就是错的。

## 它不做什么

这些是**刻意不做**，不是还没做（理由见 [ROADMAP.md](ROADMAP.md)）：

- **不自动建概念页、WikiLink、课程索引、知识图谱。** 做过，然后用数据证明了它们
  是噪声：自动生成的 245 个疑点块里，201 条覆盖的是低价值转录噪声，却漏掉了唯一
  一处会影响备考的实质错误。知识库的价值来自你自己的整理动作。
  完整测量见 [docs/AB_EVALUATION.md](docs/AB_EVALUATION.md)。
- **不做 OCR、不做图片识别、不自动对齐板书与文字。**
- **不上传音频**（除非你显式打开隐私开关）。
- **不双向同步 Obsidian**。Vault 是终点，程序不回读。

## 工作原理

```text
手机录音
  ↓  （可选）Syncthing 自动同步到电脑
data/incoming/audio/
  ↓  watch 发现新文件，按课表匹配课程，建 session
本地 faster-whisper 转录                    ← 音频不出本机
  ↓  transcript_raw.md（带时间戳）
选择性重转录（只处理可疑片段）
  ↓  transcript_repaired.md               ← 本项目的核心产物
  ├─ export-package → GPT 网页会话（可带板书照片、课件）→ 成稿
  └─ note            → 一次 API 调用（纯文本）        → 成稿
  ↓
final_note.md → 放进 Obsidian
```

`transcript_repaired.md`（下称 REPAIRED）是硬边界：出稿的两条路线**都只接受它**，
缺失时明确失败，绝不悄悄回退到未修复的原始转录。

另有一套 6 阶段的 **High Integrity / Audit Mode**（忠实清洗 → 章节检出 → 知识抽取
→ 草稿编排），提供 segment 级溯源和可审计的修改记录。它**不是默认路线** ——
A/B 对照显示它不比直接整理捞到更多知识项，却丢掉了推导过程。代码和测试全部保留，
需要逐句举证时再开。

---

## 快速开始

下面每一步都写清楚**在哪个目录、改哪个文件**。没用过 Python 也能照着走完。

预计时间：安装 10 分钟，第一次转录另需 40–60 分钟（可以挂着不管）。

### 环境要求

- **Python 3.11 或更高**
- **git**
- **ffmpeg**（第 3 步会装）
- **不需要显卡。** 没有 NVIDIA 卡时走 CPU，实测转录耗时约为录音时长的
  **0.44–0.67 倍**（100 分钟的课约 45–65 分钟）
- 硬盘留 3 GB 以上（语音模型约 1.5 GB）

### 0. 先确认 Python 和 git 装好了

随便打开一个 PowerShell 窗口（按 `Win` 键，输入 `powershell`，回车），逐条粘进去：

```powershell
python --version
git --version
```

正常应该分别打印类似 `Python 3.13.2` 和 `git version 2.4x.x`。

- **提示"找不到命令"，或者弹出微软应用商店** → 说明没装。去
  [python.org/downloads](https://www.python.org/downloads/) 下载安装，
  安装第一屏**务必勾上 `Add python.exe to PATH`**（很容易漏，漏了就得重装）。
  git 去 [git-scm.com](https://git-scm.com/download/win) 下载，一路下一步即可。
- **装完要关掉 PowerShell 重新开一个**，否则新命令不生效。

### 1. 选个文件夹，把项目下载下来

**放哪个盘、哪个目录都行**，唯一要求是路径你自己找得到，建议不要有空格。
下面以 `D:\` 为例。

**在目标文件夹里打开 PowerShell**，两种方式任选：

- 用文件资源管理器进到 `D:\`，在**空白处按住 `Shift` 再右键** →
  点「在此处打开 PowerShell 窗口」；
- 或者随便开一个 PowerShell，然后手动切目录：

  ```powershell
  cd D:\
  ```

确认自己站在哪个目录（随时可以用）：

```powershell
pwd
```

然后把项目下载下来：

```powershell
git clone https://github.com/mike200601-svg/lecture-ai.git
cd lecture-ai
```

`git clone` 会在当前目录下**新建一个 `lecture-ai` 文件夹**（例如
`D:\lecture-ai`），项目所有文件都在里面。`cd lecture-ai` 是进到这个文件夹。

> **从这里开始，本文所有命令都在 `D:\lecture-ai` 里执行。**
> 中途关了窗口重开的话，记得先 `cd D:\lecture-ai` 再继续。

### 2. 建一个独立的 Python 环境，然后安装

**为什么要这一步**：venv（虚拟环境）会在项目里建一个专属的 Python，本项目要用的
包只装在里面，不会污染你系统里的 Python，也不会和别的项目打架。删掉整个项目文件夹
就等于卸载干净。

在 `D:\lecture-ai` 里依次执行：

```powershell
# 建虚拟环境，会生成一个 .venv 文件夹
py -3.13 -m venv .venv

# 激活它。成功后命令行开头会多出 (.venv) 字样
.\.venv\Scripts\Activate.ps1

# 装本项目和它的依赖
python -m pip install -U pip
python -m pip install -e ".[dev,asr,ffmpeg]"
```

关于这几条：

- `py -3.13` 里的版本号换成你实际装的即可，比如 `py -3.12`；只装了一个版本时
  直接写 `python -m venv .venv` 也行。
- **激活报错**"无法加载文件…因为在此系统上禁止运行脚本"→ 这是 Windows 默认的
  脚本策略。执行下面这条（只影响当前用户，安全），然后重新激活：

  ```powershell
  Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
  ```
- `.[dev,asr,ffmpeg]` 里的 `.` 指"当前目录这个项目"，方括号里是可选功能组：
  `dev` = 测试工具，`asr` = 本地语音识别，`ffmpeg` = 自带一份 ffmpeg 免得你另外装。
- **以后每次新开窗口都要先激活**（`cd` 过去 + 跑一遍 `Activate.ps1`），
  否则命令找不到。

只有以后想用 API 出稿（`note` 命令）时，再多装一个：

```powershell
python -m pip install -e ".[cloud]"
```

装完验证一下：

```powershell
python -m lecture_ai --version
```

应该打印 `lecture-ai 1.0.0`。

> 装完可能有个警告说 `lecture-ai.exe` 不在 PATH 上，**可以无视**。
> 本文统一用 `python -m lecture_ai`，效果完全一样。

<details>
<summary>macOS / Linux 的对应命令</summary>

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev,asr,ffmpeg]"

brew install ffmpeg              # macOS
sudo apt-get install ffmpeg      # Debian / Ubuntu
```

装过 [uv](https://github.com/astral-sh/uv) 的话更快：

```bash
uv venv && uv pip install -e ".[dev,asr,ffmpeg]"
```

</details>

### 3. 准备两个配置文件

项目的行为由 `config` 文件夹里两个文本文件决定：

| 文件 | 管什么 |
|---|---|
| `config\config.yaml` | 路径、用哪个语音模型、各种开关 |
| `config\courses.yaml` | 你的课表（用来自动判断某段录音是哪门课） |

**这两个文件在刚下载的项目里是不存在的**，只有 `.example.yaml` 结尾的模板。
因为它们装的是各人自己的路径和课表，所以不放进仓库。你要先复制一份：

```powershell
Copy-Item config\config.example.yaml  config\config.yaml
Copy-Item config\courses.example.yaml config\courses.yaml
```

`Copy-Item` 就是"复制"：把前一个文件复制成后一个名字。等价于你在资源管理器里
进 `config` 文件夹、把 `config.example.yaml` 复制粘贴一份、改名成 `config.yaml`。
（macOS / Linux 上这条命令叫 `cp`。）

**怎么打开编辑**——用记事本就够：

```powershell
notepad config\config.yaml
```

装过 VS Code 的话 `code config\config.yaml` 更舒服（有语法高亮）。

#### 看懂 yaml 文件：三条规则

后面几步会让你改这两个文件里的某一行。它们是 **YAML** 格式，只需要知道三点：

1. **缩进表示层级**，用「名字: 值」的形式一层层嵌套。比如

   ```yaml
   transcription:          # 第 1 层
     local_whisper:        # 第 2 层，缩进 2 格
       device: cpu         # 第 3 层，缩进 4 格
   ```

   这表示"transcription 底下的 local_whisper 底下的 device 等于 cpu"。

2. **缩进只能用空格，绝对不能用 Tab 键。** 这是 YAML 最常见的报错原因。
3. **`#` 后面到行尾都是注释**，程序不看，是给人读的。

**你要做的只是改某一行的值，不要动它的缩进。** 后面每次都会告诉你改哪一行。

> 两个 yaml 文件缺失时程序也不会崩，只是全部走内置默认值、所有录音归到
> `unknown` 课程（事后可以用 `relabel` 命令重新归类）。所以你就算这一步搞错了
> 也修得回来。

### 4. 体检

配置就绪后，先让程序自查一遍环境：

```powershell
python -m lecture_ai doctor
```

它会逐项打印结果，每行开头的标记含义：

| 标记 | 含义 | 要不要管 |
|---|---|---|
| `[ OK ]` | 正常 | 不用管 |
| `[WARN]` | 能跑，但有更好的选择 | 可以先不管 |
| `[FAIL]` | 会阻塞真实使用 | 要处理，**每条都带修复建议** |

**第一次跑一定会有一条 `[FAIL] 模型 ... MISSING`** —— 那是正常的初始状态，
语音模型还没下载。下一步就是解决它。

### 5. 语音模型

转录靠的是本地的 faster-whisper 语音模型（一份约 1.5 GB 的权重文件）。
它不在仓库里，需要单独获取——有两条路。

#### 路线 A：什么都不用改（推荐）

刚才复制出来的 `config\config.yaml` **默认就是这个方案**，你不需要动任何东西：

打开 `config\config.yaml`，能看到这样一段（大约在文件前 1/3 处）：

```yaml
transcription:
  provider: local_whisper

  local_whisper:
    # 默认按「无 CUDA 的笔记本」配置：CPU + int8。
    # 这里填 HuggingFace 模型名，首次转录会自动下载到 data/cache/models，开箱可用。
    model: medium               # ← 这一行就是模型设置
    device: cpu                 # ← 用 CPU 还是显卡
    compute_type: int8          # ← 计算精度，CPU 上用 int8 最快
```

`model: medium` 是一个**在线模型名**（来自 HuggingFace 模型库）。
第一次真正开始转录时，程序会自动把这 1.5 GB 下载到项目内的
`data\cache\models` 文件夹，之后就一直复用，不会重复下载。

也就是说：**这条路你不用改配置、不用手动下载，只要第一次转录时能联网。**

#### 路线 B：预先下载 / 想离线跑

如果第一次转录时不方便联网，或者你想先把模型下好：

1. 从 HuggingFace 下载 `Systran/faster-whisper-medium` 的全部文件，放进项目里的
   `models\faster-whisper-medium\` 文件夹（自己新建），里面应包含
   `model.bin`、`config.json`、`tokenizer.json`、`vocabulary.txt`。
2. 然后把上面那段配置里的 `model` 那一行改成：

   ```yaml
       model: models/faster-whisper-medium
   ```

   注意**保持原有缩进**（前面 4 个空格），只改冒号后面的值。相对路径按项目根目录
   解析，也就是 `D:\lecture-ai\models\faster-whisper-medium`。

`models\` 文件夹已被 git 忽略，权重不会进版本库。

#### 有 NVIDIA 显卡的话

只改配置，代码不用动——把那三行改成：

```yaml
    model: large-v3
    device: cuda
    compute_type: float16
```

#### 想自己比较不同模型的效果

```powershell
python scripts\bench_asr.py <一段10~15分钟的真实课堂录音> --models "medium,large-v3-turbo"
```

`tiny` 只用于跑单元测试，识别质量不够，不要用于真实课堂。

### 6. 填你的课表

程序按**录音的起始时间**自动判断这是哪门课，所以要先告诉它你的课表。

```powershell
notepad config\courses.yaml
```

打开后把里面的示例课程整段替换成你自己的。文件结构是这样：

```yaml
courses:                                  # 固定写法，别改
  quantum_mechanics:                      # 课程代号：只能用英文小写和下划线，自己起
    name: 量子力学                         # 显示名称，会出现在文件名里
    teacher: ""                           # 可留空
    semester: 2026-秋                      # 可留空
    glossary: quantum_mechanics.txt       # 术语表文件名，提升专业名词准确率
    obsidian_folder: University/量子力学    # 以后进 Obsidian 时的目录
    schedule:                             # 这门课每周上课的时段，可以有多个
      - weekday: 3                        # 1=周一, 2=周二 … 7=周日
        start: "09:45"                    # 上课时间，必须带引号
        end: "11:25"                      # 下课时间
      - weekday: 5                        # 同一门课的第二个时段
        start: "10:00"
        end: "11:40"

  unknown:                                # 兜底课程，不要删
    name: 未归类
    glossary: ""
    obsidian_folder: University/未归类
    schedule: []
```

几个容易踩的点：

- **课程代号**（`quantum_mechanics` 这层）是给程序用的内部标识，只能英文小写加
  下划线；`name` 才是给人看的中文名。
- 时间**必须带引号**写成 `"09:45"`，不带引号 YAML 会当成别的类型。
- **`unknown` 那段不要删**，匹配不上课表的录音会落到它里面。
- 匹配有 ±30 分钟容差（可在 `config.yaml` 的 `course.match_tolerance_minutes` 调）。
  匹配不上不会失败，只是归到 `unknown`，事后可以
  `python -m lecture_ai relabel <session_id>` 改正。
- `glossary` 指向 `config\glossary\` 下的术语表文件。仓库自带几份理工科示例，
  你也可以自己新建一个 txt（一行一个专业名词）。不需要就写 `glossary: ""`。

改完再体检一次，确认没写坏：

```powershell
python -m lecture_ai doctor
```

看到 `[ OK ] courses.yaml` 就说明格式没问题。若报 `courses.yaml 解析失败`，
八成是缩进用了 Tab 或者时间没加引号。

### 7. 跑第一节课

把录音文件（`.m4a` / `.mp3` / `.wav` 等）复制到项目里的
`data\incoming\audio\` 文件夹。第 4 步跑过 `doctor` 之后这个文件夹就已经自动
建好了，直接进去放文件即可。

然后二选一：

```powershell
# 方式一：长驻监听，放进去就自动处理（推荐日常用）
python -m lecture_ai watch

# 方式二：手动一步步来，方便看清每步在干什么
python -m lecture_ai scan            # 只扫描建档，不转录
python -m lecture_ai status          # 看有哪些课、各自到哪一步了
python -m lecture_ai process --all   # 开始转录（慢，可以挂着）
python -m lecture_ai repair <session_id>   # 对可疑片段做选择性重转录
```

`<session_id>` 从 `status` 的输出里复制，形如
`2026-09-03_1554_digital-electronics_002`。

转录产物在 `data\sessions\<session_id>\transcript\`：

- `transcript_raw.md` —— 原始转录
- `transcript_repaired.md` —— **修复后的正式转录，后面出稿只认这一份**

拿到 `transcript_repaired.md` 之后，选一条出稿路线（下一节）。

---

## 两条出稿路线

|  | `export-package` → GPT 网页 | `note`（API） |
|---|---|---|
| 板书照片、课件 | **能带上**，由你上传 | 不发送，只有转录文本 |
| 需要 API key | 不需要 | 需要 `OPENAI_API_KEY` |
| 计费 | 走你的网页订阅 | 按 token |
| 人工操作 | 上传 + 手动保存成稿 | 一条命令 |
| 成稿文件名 | 同一套命名规则 | 同一套命名规则 |

**有板书或课件的课走网页路线**，成稿质量上限更高 —— 转录里"这个式子等于这个"
只有配上照片才能还原。**纯讲授的课用 `note`** 一条命令就完事。

两条路线产出的文件名完全一致（`日期_时间_课程名_序号_final_note.md`），同一节课
不会因为换路线出现两种命名。

### 网页路线

```powershell
python -m lecture_ai export-package <session_id>
python -m lecture_ai export-package <session_id> --board <照片或目录> --slides <课件或目录>
```

在 `paths.export_dir` 下生成一个目录，含 REPAIRED 转录、板书、课件、投喂提示词和
manifest（记录每个输入文件的 SHA-256）。把整个目录传给固定的网页会话，把输出按
提示保存为 `..._final_note.md`。

归属规则很严：session 的 `images/` 与 `slides/` 视为已明确归属，其他位置的材料
**必须**用 `--board` / `--slides` 显式指定。`data/incoming/images/` 里无法归属的照片
只写 warning，绝不按时间或文件名猜。

### API 路线

```powershell
python -m lecture_ai note <session_id>
python -m lecture_ai note <session_id> --dry-run   # 只渲染提示词，不调用模型
python -m lecture_ai note <session_id> --force     # 覆盖已有成稿
```

需要 `llm.provider: openai` 和 `.env` 里的 `OPENAI_API_KEY`。它会：

- **只发送文本**，任何情况下不读取、不上传图片；
- 自动补 YAML front-matter（course / date / session_id / 转录 SHA-256 / 生成模型）；
- 把模型习惯输出的 `\[..\]` `\(..\)` 数学定界符统一成 Obsidian 认识的 `$$` / `$`
  （代码块内不动）；
- session 里有板书或课件时**明确警告**成稿不含它们，不静默丢材料；
- 成稿已存在时必须显式 `--force` 才覆盖。

输出长度上限单独配置 —— 整篇笔记很长，沿用通用的 `llm.max_tokens` 会被静默截断：

```yaml
note:
  max_output_tokens: 32000
  temperature: 0.3
```

---

## WebUI 面板

不想敲命令的话：

```powershell
python -m lecture_ai serve
# 浏览器打开 http://127.0.0.1:8477
```

**启动报端口错误怎么办**（Windows 上比较常见）：

```powershell
python -m lecture_ai serve --port 0        # 让系统随机挑一个空闲端口，最省事
python -m lecture_ai serve --port 8899     # 或者自己指定
```

Windows 上如果占用端口的程序绑的是 `0.0.0.0`，报的会是
`WinError 10013 (access forbidden)` 而不是 `10048 (already in use)` ——
看着像权限或防火墙问题，其实只是端口冲突。程序会直接给出上面这两条建议，
以及查"是谁占了这个端口"的命令。

> 顺便说一句：默认端口刻意避开了 8765 —— **百度输入法**会监听
> `0.0.0.0:8765`，而它在中文 Windows 上非常普遍。

**不需要额外安装任何东西** —— 用标准库 `http.server` 实现，不引入 Web 框架依赖。

面板能做：看所有课堂的状态（时长、有没有 REPAIRED、有没有成稿、各步骤进度）、
一键出稿、一键生成投喂包、直接预览成稿内容。

API 令牌在页面右上角输入，**只存在服务端进程内存里**：不写 config、不写 `.env`、
不进日志，接口也永不回显（只回一个"有没有设置"的布尔值）。进程退出即消失，
下次启动要重新输。有测试专门扫描整个项目目录，确认令牌没有落进任何文件。

出稿仍然走同一套 `NoteBuilder`，所以 REPAIRED 硬要求、provider 检查、隐私闸门、
幂等保护全部照旧生效 —— 面板不是绕过它们的后门，这一点也有测试。

> **默认只绑 `127.0.0.1`。** 面板没有任何认证，而它能触发按 token 计费的 API 调用。
> `--host 0.0.0.0` 会打印显眼警告：同网段的任何人都能用你的令牌出稿。

**不在当前版本范围内**：上传录音、在面板里触发转录。转录一节课要 45–65 分钟，
塞进 HTTP 请求需要一整套后台任务机制；而录音接入本来已经由 `watch` 自动化了。
面板先解决"看状态 + 出稿"这段 —— 那是命令行最啰嗦、而 API 又足够快的部分。

---

## 进 Obsidian

Obsidian 的"库"就是一个普通文件夹，把 Markdown 放进去就算入库，没有 API 要调。
所以这一步不需要程序：

1. 把成稿存到你按课程分好的目录里（课件 PDF 也放同目录）；
2. 用 Obsidian 打开那个目录的上层文件夹（"打开文件夹作为库"）；
3. 笔记里写 `![[课件.pdf]]` 就能把 PDF 直接嵌进正文显示。

**注意把 `paths.export_dir` 配在库外面。** 投喂包里的转录、提示词、包说明都是
`.md`，进了库会被当成笔记，把真正的成稿淹掉。

程序化入库（`vault-import` / `vault-status`）的设计见
[docs/DESIGN_OBSIDIAN.md](docs/DESIGN_OBSIDIAN.md)，**当前未实现** —— 先用几周
再决定值不值得写。

---

## 命令速查

```powershell
python -m lecture_ai doctor                  # 环境体检，新机器先跑这个
python -m lecture_ai watch                   # 长驻监听
python -m lecture_ai scan                    # 只建 session，不转录
python -m lecture_ai probe <audio>           # 只读检查录音 metadata 与起始时间推断
python -m lecture_ai process --all           # 转录所有待处理 session
python -m lecture_ai status [<session_id>]   # 总览 / 单个详情
python -m lecture_ai retry <session_id>      # 重试失败的（不重跑已成功的转录）
python -m lecture_ai relabel <session_id>    # 课程识别错了，改对并让目录名跟上
python -m lecture_ai reindex                 # 从 metadata.json 重建数据库索引

python -m lecture_ai repair <session_id>         # 选择性重转录（可 --dry-run）
python -m lecture_ai export-package <session_id> # GPT 网页投喂包（可 --board/--slides）
python -m lecture_ai note <session_id>           # API 一步出成稿
python -m lecture_ai serve                       # 本地 WebUI 面板（默认 127.0.0.1:8765）

# High Integrity Mode（按需，不是默认路线）
python -m lecture_ai clean <session_id>
python -m lecture_ai structure <session_id>
python -m lecture_ai knowledge <session_id>
python -m lecture_ai draft <session_id>
```

---

## 录音与模型

起始时间决定课程归属，按以下优先级推断：

1. 音频容器里的 `creation_time`（多数手机录音机会写）。若它与「文件名时间 + 时长」
   接近，判定为封口时间，改用文件名的开始时间
2. 文件名里的时间戳：`录音_20260903_140000.m4a`、`20260903-140000.mp3`
3. 文件修改时间减去时长
4. 文件创建时间（置信度标记为 `low`）

**建议保留录音机原始文件名里的日期时间**，不要改成"新录音1.m4a"。

正在录制中的 M4A 没有最终 `moov` 元数据，无法解码。pipeline 会在建 session 前
拒绝它并稍后重试，不会产生半截转录。

---

## 手机自动同步（可选）

作者用的方案，不是必需 —— 手动复制录音文件一样能用。

手机 Syncthing-Fork + 电脑 Syncthing：

```text
Android /storage/emulated/0/Sounds（Send Only）
  → data/incoming/audio（Receive Only）
  → lecture-ai watch
```

- 录音目录保持**单向**，绝不为了传别的东西改成双向。
- `processing.keep_incoming` 必须保持 `true`。Syncthing 的接收目录是同步真相源，
  watcher 把源文件移走的话 Syncthing 会重新下载它。session 里另有独立的原始副本。
- 电脑需保持开机不睡眠（睡眠/休眠关闭，合盖不执行操作）。
- 手机可熄屏，允许 Syncthing 后台运行。

备用方案 `scripts/setup_phone_sftp.ps1`（Tailscale + 密钥 SFTP）会占用手机的 VPN
槽位，仅在 Syncthing 故障时手动启用。脚本需要你传入自己的 Tailscale IP
（`tailscale ip -4` 查看），不带任何默认地址。

---

## 隐私与伦理

课堂录音是私人学习资料，**默认全部本地处理**。三个开关是硬闸门，不是建议：

```yaml
privacy:
  allow_cloud_audio: false      # false 时禁止任何云端 ASR
  allow_cloud_images: false
  allow_cloud_transcript: true  # 出稿要把转录文字发给模型，需要它为 true
```

`allow_cloud_audio: false` 时即使把 provider 配成 `openai` 也会被**直接拒绝**并说明
原因，不会"警告一下然后照样上传"。

API key 只能写在 `.env`（见 [.env.example](.env.example)）。**写进 `config.yaml` 会被
配置加载器直接报错**，防止误提交。

### 使用前请想清楚两件事

1. **录课可能需要授课老师同意。** 不同学校、不同地区的规定不一样，这个工具录下来
   的是别人说的话，不只是你自己的笔记。
2. **出稿这一步会把老师的讲课内容发给第三方模型服务商。** 音频不会离开本机，但
   转录文字会。如果这不可接受，那就只用到 REPAIRED 为止 —— 那一层完全本地，
   一份带时间戳的完整课堂文本本身已经很有用。

## 成本

本地转录不花钱，只花时间（约录音时长的 0.44–0.67 倍）。

出稿这一步：一节 100 分钟的课，REPAIRED 转录约 8 万字符，渲染成提示词约 4.7 万
字符（中文大约 3–5 万 token 输入），成稿三五万字输出。**走 API 是一节课一次调用，
按 token 计费，具体金额看你选的模型定价 —— 跑之前先算一下，十几节课累积起来不是
小数目。** 先用 `--dry-run` 看提示词多大。

网页路线走订阅制，不按 token 走，代价是每节课要手动上传和保存。这也是它不会被
API 路线取代的原因。

## 已知局限

诚实清单：

- **Windows 优先。** 核心逻辑三平台都跑 CI（Linux / macOS / Windows × Python
  3.11–3.13），但**开发与日常使用只在 Windows 上做过**；手机同步辅助脚本是
  PowerShell，Windows 专用。macOS / Linux 上测试通过 ≠ 完整工作流验证过。
- **课表匹配按中国大学作息设计**（weekday + 起止时间 + 容差）。
- **成稿质量取决于模型。** 项目负责把材料准备到可靠状态，最后一步的发挥不由它控制。
- **板书只能走网页路线。** API 路线不发图片，这是有意的边界，不是待办。
- **Phase 3 视觉融合、Phase 4 Obsidian 程序化入库：均未实现。**
- **WebUI 只覆盖「看状态 + 出稿」**，不能上传录音或在页面里触发转录（见上文原因）。
- **WebUI 没有认证**，因此默认只绑 `127.0.0.1`，不适合直接暴露到公网。

## 几条硬规矩

这些在代码里有对应测试保护：

- **原始录音永不删除、永不覆盖。** 同名文件自动加序号。
- **RAW 转录永不覆盖。** 固定走 RAW → REPAIRED → CLEANED，每层保存来源 SHA。
- **转录必须带时间戳。** 只存纯文本不允许 —— 溯源和板书对齐都靠时间轴。
- **retry 绝不重跑已成功的 ASR。** 那是最贵的一步，缓存合法就复用。
- **出稿只接受正式 REPAIRED**，缺失即失败，不回退 RAW。
- **`metadata.json` 是真相源**，SQLite 只是索引，删了可以 `reindex` 重建。
- **渲染器不得生成 WikiLink。**

## 开发

```bash
python -m pytest -q            # 全部测试（不需要 GPU / 模型 / 网络）
python -m pytest -q -k e2e     # 只跑端到端
```

CI（[.github/workflows/ci.yml](.github/workflows/ci.yml)）在 Linux / macOS /
Windows 上跑同一套测试，另有一个 job 构建 wheel、`twine check` 元数据，并断言
WebUI 页面文件真的被打进了 wheel —— 漏了它 `serve` 装完就是 500。

一条硬性架构约束：**领域层（ingestion / session / audio / transcription / database /
utils）不得反向 import pipeline 或 cli**，有专门的测试盯着。

架构与历史记录见 [docs/](docs/)（有索引）。路线以 [ROADMAP.md](ROADMAP.md) 为
唯一权威。想知道这个项目为什么长成现在这样，直接读
[docs/AB_EVALUATION.md](docs/AB_EVALUATION.md) —— 那是它砍掉自己一大半流水线的理由。

## License

[MIT](LICENSE)
