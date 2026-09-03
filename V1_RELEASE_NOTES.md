# v1.0 Release Notes

## v1.0 能做什么

手机课堂录音经 Syncthing、watcher、本地 faster-whisper 和 selective repair，生成正式
`transcript_repaired.md`；`export-package` 再把它与已明确归属的板书、课件复制成带
日期、时间、课程名和 SHA-256 manifest 的 GPT Web 投喂包。

## 日常怎么用

运行 `lecture-ai watch` 接收并转录录音，确认本节课材料归属后运行
`lecture-ai export-package <session_id> [--board ...] [--slides ...]`，再把
`<paths.export_dir>` 中对应目录上传到固定
GPT 网页会话。最终初稿按包内提示保存为 `日期_时间_课程名_序号_final_note.md`。

## 刻意不做

不做 OCR、图片识别、EXIF、自动上传/GPT API、教材解析、Phase 3 visual resolver、
Phase 4 Obsidian、概念页、WikiLink 或知识图谱。完整 Phase 2 只作为 High Integrity Mode。

## 后续唯一非阻塞方向

Obsidian import。
