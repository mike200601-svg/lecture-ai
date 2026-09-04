# Session Export Package · v1.0 实现约定

> 状态：**IMPLEMENTED**。当前路线以 [ROADMAP.md](../ROADMAP.md) 为唯一权威。

## 目标

`lecture-ai export-package <session_id>` 把一节课需要交给 GPT 网页版的材料整理到一个
看得懂、拿得走、可复算的目录。本项目只整理输入，不调用模型、不操控网页、不生成最终笔记。

默认生产路线：

```text
phone recording → Syncthing → watcher → local faster-whisper
→ selective repair → transcript_repaired → export-package → GPT Web → final note
```

## 正式输出位置与命名

本机配置：

```text
<paths.export_dir>\
  YYYY-MM-DD_HHMM_课程名_序号\
    YYYY-MM-DD_HHMM_课程名_序号_01_transcript_repaired.md
    02_board\
      YYYY-MM-DD_HHMM_课程名_序号_board_001.jpg
    03_slides\
      YYYY-MM-DD_HHMM_课程名_序号_slides_001.pdf
    YYYY-MM-DD_HHMM_课程名_序号_session_info.md
    YYYY-MM-DD_HHMM_课程名_序号_NOTE_PROMPT.md
    YYYY-MM-DD_HHMM_课程名_序号_manifest.json
```

GPT 网页版的规范输出名是 `YYYY-MM-DD_HHMM_课程名_序号_final_note.md`，会写进 prompt、
session_info 和 manifest。旧 Session 目录名仍可读取；显示身份以 metadata 的日期、开始时间
和课程快照为准，缺失字段明确写 `unknown`，不猜。

## 输入归属规则

- transcript：**只能**读取 Session 正式 `transcript/transcript_repaired.md`。缺失即失败，
  禁止回退 `transcript_raw.md` 或 CLEANED。
- board：Session 的 `images/`、`metadata.images` 已关联文件，或 CLI `--board` 显式提供的
  文件/目录。`data/incoming/images/` 中其他照片只列进 `unassigned` 与 warning，不打包。
- slides：Session 的 `slides/`，或 CLI `--slides` 显式提供的文件/目录。课件没有可靠时间信号，
  不从其他课程目录自动猜。

Syncthing 可以把手机照片同步到 `data/incoming/images/`，也可以把明确按课整理的文件同步到
Session 的 `images/` / `slides/`；同步本身不等于课程归属。

## Manifest

Manifest 至少记录：session_id、course、course_key、date、start_time、end_time、created_at、
REPAIRED source path + SHA-256、board files、slide files、unassigned、warnings，以及建议的最终
笔记文件名。包内每个实际输入文件都记录 SHA-256 和字节数。

## 安全与幂等边界

- 上游 Session 和显式材料只读；全部使用复制，禁止移动或修改源文件。
- 重跑同一节课会原位重建同名生成目录，不制造 `_1` / `_2` 重复副本。
- 输出目录是可重建输入包，最终笔记必须另行保存，不能写回生成目录。
- 不做 OCR、视觉模型、EXIF、自动图文对齐、教材解析、自动上传或 GPT API。
- Phase 3 visual resolver 与 Phase 4 Obsidian 均未实现。

## CLI

```powershell
python -m lecture_ai export-package <session_id>
python -m lecture_ai export-package <session_id> --board <照片...> --slides <课件...>
python -m lecture_ai export-package <session_id> --dry-run
```

原先的 `lecture-ai export` 占位命令已移除（只打印「未实现」，却与 `export-package` 名字相撞）。
Obsidian 入库将来是独立的 `vault-import` / `vault-status`，见 `DESIGN_OBSIDIAN.md`，两者语义不混用。
