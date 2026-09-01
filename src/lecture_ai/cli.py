"""命令行入口。

设计：CLI 只负责参数解析、调用 pipeline、把结果打印成人类可读的样子。
业务逻辑一律在 pipeline / 领域模块里，这里不写。

退出码：0 成功 / 1 业务失败 / 2 用法或配置错误
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import sys
import unicodedata
from pathlib import Path

from lecture_ai import __version__
from lecture_ai.config import Config, find_project_root, load_config
from lecture_ai.errors import ConfigError, LectureAIError
from lecture_ai.logging_setup import setup_logging
from lecture_ai.utils.timefmt import hhmmss, to_iso

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- 输出


def _force_utf8_stdout() -> None:
    """把 stdout 切到 UTF-8。

    Windows 上连接真实控制台时 Python 已经用 UTF-8（底层走 WriteConsoleW），
    但输出被重定向到管道/文件时会退回 locale 编码（简中系统上是 cp936），
    中文就此变成乱码。这里统一成 UTF-8，两种情况都正确。
    """
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def out(msg: str = "") -> None:
    """面向用户的输出。日志走 logging，这里只负责命令行结果展示。"""
    try:
        print(msg)
    except UnicodeEncodeError:  # 极端情况下的兜底，不让编码问题掀翻命令
        sys.stdout.write(msg.encode("utf-8", "replace").decode("utf-8", "replace") + "\n")


def _width(text: str) -> int:
    """终端显示宽度：中文/全角字符占 2 列。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def pad(text: str, columns: int) -> str:
    """按显示宽度左对齐补空格，避免中英混排的表格错位。"""
    return text + " " * max(0, columns - _width(text))


def _bootstrap(args: argparse.Namespace) -> Config:
    config = load_config(Path(args.config) if args.config else None)
    config.ensure_dirs()
    setup_logging(config.paths.log_dir, config.logging, verbose=args.verbose)
    return config


# --------------------------------------------------------------------------- 子命令


def cmd_init(args: argparse.Namespace) -> int:
    """初始化目录与数据库。"""
    from lecture_ai.database import Database

    config = _bootstrap(args)
    Database(config.paths.database)
    out(f"项目根目录：{config.paths.project_root}")
    out(f"已就绪：")
    out(f"  录音投放目录  {config.paths.incoming_audio}")
    out(f"  Session 目录  {config.paths.session_dir}")
    out(f"  数据库        {config.paths.database}")
    out("")
    out("下一步：把课堂录音放进「录音投放目录」，然后运行 lecture-ai watch")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    """环境体检。新机器上先跑这个。"""
    config = _bootstrap(args)
    problems = 0

    def report(name: str, ok: bool | None, detail: str) -> None:
        nonlocal problems
        tag = {True: "[ OK ]", False: "[FAIL]", None: "[WARN]"}[ok]
        if ok is False:
            problems += 1
        out(f"{tag} {pad(name, 16)} {detail}")

    out(f"lecture-ai {__version__} 环境体检")
    out("=" * 64)

    py = sys.version_info
    report("Python", py >= (3, 11), f"{py.major}.{py.minor}.{py.micro}（需要 3.11+）")

    # ffmpeg
    try:
        from lecture_ai.audio import ffmpeg_version, get_tools

        tools = get_tools(config.audio.ffmpeg_path, config.audio.ffprobe_path)
        report("ffmpeg", True, f"{ffmpeg_version(tools)}（来源：{tools.source}）")
        report(
            "ffprobe",
            True if tools.has_ffprobe else None,
            tools.ffprobe if tools.has_ffprobe else "未找到，将退化为解析 ffmpeg 输出（可用但信息较少）",
        )
    except LectureAIError as exc:
        report("ffmpeg", False, str(exc).replace("\n", "\n       "))
        report("ffprobe", False, "随 ffmpeg 一并缺失")

    # ASR
    provider = config.transcription.provider
    if provider == "local_whisper":
        try:
            import faster_whisper  # noqa: F401

            lw = config.transcription.local_whisper
            report("faster-whisper", True,
                   f"已安装 · 模型 {lw.model} · device={lw.device} · {lw.compute_type}")

            from lecture_ai.transcription import (
                inspect_model_cache,
                resolve_model_reference,
                validate_local_model,
            )

            configured_model = resolve_model_reference(lw.model, config.paths.project_root)
            models = list(dict.fromkeys(("tiny", "medium", "large-v3-turbo", configured_model)))
            for model in models:
                cached = inspect_model_cache(model, config.paths.cache_dir)
                size = f"{cached.size_bytes / 2**20:.1f} MiB"
                state = cached.state
                load_detail = ""
                if model == configured_model and cached.source == "local" and state == "ready":
                    try:
                        elapsed = validate_local_model(
                            cached.path,
                            device=lw.device,
                            compute_type=lw.compute_type,
                            cpu_threads=lw.cpu_threads,
                        )
                        load_detail = f" · load OK {elapsed:.2f}s"
                    except Exception as exc:  # CTranslate2 的异常类型不稳定
                        state = "partial"
                        load_detail = f" · load failed: {exc}"

                missing = (
                    f" · missing={','.join(cached.missing_files)}"
                    if cached.missing_files else ""
                )
                detail = (
                    f"{state.upper()} · source: {cached.source} · {size} · "
                    f"path: {cached.path}{missing}{load_detail}"
                )
                if state == "ready":
                    ok = True
                elif model == configured_model:
                    ok = False  # 当前配置的模型不可用会直接阻塞真实转录
                else:
                    ok = None
                display = cached.path.name if cached.source == "local" else model
                report(f"模型 {display}", ok, detail)
        except ImportError:
            report("faster-whisper", False,
                   '未安装。运行：pip install "lecture-ai[asr]"')
    else:
        report("ASR provider", None, f"{provider}（非本地）")

    # GPU
    try:
        import ctranslate2

        n = ctranslate2.get_cuda_device_count()
        if n > 0:
            report("CUDA", True, f"检测到 {n} 块 GPU，建议 device=cuda / compute_type=float16")
        else:
            report("CUDA", None, "无可用 NVIDIA GPU，使用 CPU（建议 compute_type=int8）")
    except (ImportError, AttributeError, RuntimeError):
        report("CUDA", None, "无法检测（ctranslate2 未安装）")

    # 配置文件
    report("config.yaml", config.config_path is not None,
           str(config.config_path or "未找到，正在使用内置默认值"))
    report("courses.yaml", config.courses_path.exists(),
           str(config.courses_path) if config.courses_path.exists() else "未找到，所有录音将归入 unknown")

    glossary_files = (
        sorted(p.name for p in config.glossary_dir.glob("*.txt"))
        if config.glossary_dir.exists() else []
    )
    report("术语词典", bool(glossary_files) or None,
           "、".join(glossary_files) if glossary_files else "无（不影响运行，但专业名词错误率会更高）")

    # 数据库与磁盘
    try:
        from lecture_ai.database import Database

        Database(config.paths.database)
        report("数据库", True, str(config.paths.database))
    except Exception as exc:  # sqlite 的异常类型较杂
        report("数据库", False, f"{config.paths.database}：{exc}")

    usage = shutil.disk_usage(config.paths.project_root)
    free_gb = usage.free / 1e9
    report("磁盘空间", free_gb > 5, f"剩余 {free_gb:.1f} GB")

    # 隐私
    report("隐私设置", None,
           f"云端音频={config.privacy.allow_cloud_audio} · "
           f"云端图片={config.privacy.allow_cloud_images} · "
           f"云端文本={config.privacy.allow_cloud_transcript}")

    # Phase 2A 文本清洗。缺 key/SDK 不阻塞 Phase 1，因此记 WARN 而非 FAIL。
    clean_prompt = config.paths.project_root / "prompts" / "transcript_clean.md"
    report("清洗 prompt", clean_prompt.exists(), str(clean_prompt))
    if config.llm.provider == "openai":
        sdk_ready = importlib.util.find_spec("openai") is not None
        key_ready = bool(os.environ.get("OPENAI_API_KEY"))
        state = (
            f"openai/{config.llm.model} · SDK={'ready' if sdk_ready else 'missing'} · "
            f"OPENAI_API_KEY={'set' if key_ready else 'missing'}"
        )
        report("文本 LLM", True if sdk_ready and key_ready else None, state)
    else:
        report("文本 LLM", None, f"{config.llm.provider}/{config.llm.model}")

    out("=" * 64)
    if problems:
        out(f"发现 {problems} 个问题，请先修复后再使用。")
        return EXIT_FAILURE
    out("环境正常。")
    return EXIT_OK


def cmd_scan(args: argparse.Namespace) -> int:
    """扫描 incoming，建立 session（不转录）。"""
    from lecture_ai.pipeline import Phase1Pipeline

    config = _bootstrap(args)
    pipeline = Phase1Pipeline(config, one_shot=True)
    created = pipeline.ingest_new_audio()

    if not created:
        out(f"没有发现新录音（监听目录：{config.paths.incoming_audio}）")
        return EXIT_OK

    out(f"新建 {len(created)} 个 session：")
    for meta in created:
        out(f"  {meta.session_id}  课程={meta.course.name}  "
            f"时长={hhmmss(meta.audio.duration_sec or 0)}")
    out("")
    out("运行 lecture-ai process <session_id> 开始转录，或 lecture-ai process --all")
    return EXIT_OK


def cmd_probe(args: argparse.Namespace) -> int:
    """只读检查手机录音元数据与 Session 起始时间推断。"""
    from lecture_ai.pipeline import probe_audio_metadata

    config = _bootstrap(args)
    report = probe_audio_metadata(Path(args.audio), config)

    out(f"file: {report.file}")
    out(f"duration: {report.duration_sec:.3f}")
    out(f"codec: {report.codec or '-'}")
    out(f"sample_rate: {report.sample_rate if report.sample_rate is not None else '-'}")
    out(f"channels: {report.channels if report.channels is not None else '-'}")
    out(f"creation_time: {to_iso(report.creation_time) or '-'}")
    out(f"mtime: {to_iso(report.mtime)}")
    out(f"ctime: {to_iso(report.ctime)}")
    out(f"inferred_start_time: {to_iso(report.inferred_start_time)}")
    out(f"start_time_source: {report.start_time_source}")
    out(f"start_time_confidence: {report.start_time_confidence}")
    return EXIT_OK


def cmd_process(args: argparse.Namespace) -> int:
    """转录一个或全部待处理 session。"""
    from lecture_ai.pipeline import Phase1Pipeline

    config = _bootstrap(args)
    pipeline = Phase1Pipeline(config, one_shot=True)
    force = tuple(args.force or ())

    try:
        if args.all:
            if args.scan:
                pipeline.ingest_new_audio()
            outcomes = pipeline.process_pending()
        elif args.session_id:
            outcomes = [pipeline.process_session(args.session_id, force=force)]
        else:
            out("请指定 session_id，或使用 --all 处理全部待处理 session")
            return EXIT_USAGE
    finally:
        pipeline.close()

    if not outcomes:
        out("没有待处理的 session")
        return EXIT_OK

    failed = 0
    for o in outcomes:
        if o.ok:
            out(f"✔ {o.session_id}  {o.message}  （{o.elapsed_sec:.1f} 秒）")
        else:
            failed += 1
            out(f"✘ {o.session_id}  {o.message}")
    return EXIT_FAILURE if failed else EXIT_OK


def cmd_status(args: argparse.Namespace) -> int:
    """总览或单个 session 的详情。"""
    from lecture_ai.database import Database
    from lecture_ai.session import SessionManager

    config = _bootstrap(args)
    db = Database(config.paths.database)
    manager = SessionManager(config, db)

    if args.session_id:
        return _status_detail(manager, db, args.session_id)

    ids = manager.list_ids()
    if not ids:
        out("还没有任何 session。把录音放进 "
            f"{config.paths.incoming_audio} 后运行 lecture-ai scan")
        return EXIT_OK

    counts = db.count_sessions_by_state()
    out("状态统计：" + "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    out("")
    out(f"{pad('SESSION_ID', 44)} {pad('状态', 16)} {pad('课程', 16)} 时长")
    out("-" * 92)
    for session_id in sorted(ids, reverse=True)[: args.limit]:
        try:
            meta = manager.load(session_id)
        except LectureAIError:
            continue
        out(f"{pad(meta.session_id, 44)} {pad(str(meta.state), 16)} "
            f"{pad(meta.course.name, 16)} {hhmmss(meta.audio.duration_sec or 0)}")
    return EXIT_OK


def _status_detail(manager, db, session_id: str) -> int:
    meta = manager.load(session_id)
    sdir = manager.session_dir(session_id)
    out(f"Session   {meta.session_id}")
    out(f"课程      {meta.course.name}（{meta.course.key}）")
    out(f"日期      {meta.date}")
    out(f"起始时间  {meta.start_time}  "
        f"[来源 {meta.start_time_source} / 置信度 {meta.start_time_confidence}]")
    out(f"状态      {meta.state}" + (f"  ← 失败于 {meta.failed_from}" if meta.failed_from else ""))
    if meta.error:
        out(f"错误      {meta.error}")
    out(f"目录      {sdir}")
    out(f"音频      {meta.audio.orig_name or '-'}  "
        f"时长 {hhmmss(meta.audio.duration_sec or 0)}")
    out("")
    out("处理步骤：")
    for name, st in meta.steps.items():
        elapsed = f"{st.elapsed_sec:.1f}s" if st.elapsed_sec else "-"
        extra = f"  [{st.provider}/{st.model}]" if st.provider else ""
        out(f"  {pad(name, 12)} {pad(st.status, 10)} {elapsed:>10}{extra}")
        if st.error:
            out(f"               错误：{st.error}")

    transcript = sdir / "transcript" / "transcript_raw.json"
    if transcript.exists():
        import json

        data = json.loads(transcript.read_text(encoding="utf-8"))
        out("")
        out(f"转录      {data.get('segment_count', 0)} 段  "
            f"语言={data.get('language')}  {transcript}")
    return EXIT_OK


def cmd_sessions(args: argparse.Namespace) -> int:
    """按状态列出 session。"""
    args.limit = args.limit or 50
    args.session_id = None
    return cmd_status(args)


def cmd_retry(args: argparse.Namespace) -> int:
    """重试失败的 session。默认从失败点继续，不重跑已成功的步骤。"""
    from lecture_ai.pipeline import Phase1Pipeline

    config = _bootstrap(args)
    pipeline = Phase1Pipeline(config)
    try:
        outcome = pipeline.process_session(args.session_id, force=tuple(args.force or ()))
    finally:
        pipeline.close()

    if outcome.ok:
        out(f"✔ {outcome.session_id}  {outcome.message}（{outcome.elapsed_sec:.1f} 秒）")
        return EXIT_OK
    out(f"✘ {outcome.session_id}  {outcome.message}")
    return EXIT_FAILURE


def _parse_timestamp(value: str) -> float:
    """解析秒数或 HH:MM:SS(.sss)。"""
    value = value.strip()
    try:
        if ":" not in value:
            seconds = float(value)
        else:
            parts = value.split(":")
            if len(parts) == 2:
                hours = 0.0
                minutes, seconds_part = parts
            elif len(parts) == 3:
                hours, minutes, seconds_part = parts
                hours = float(hours)
            else:
                raise ValueError
            seconds = hours * 3600 + float(minutes) * 60 + float(seconds_part)
        if seconds < 0:
            raise ValueError
        return seconds
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"无效时间戳：{value}") from exc


def _parse_region(value: str) -> tuple[float, float]:
    """解析 START-END；时间点可用秒数或 HH:MM:SS。"""
    if "-" not in value:
        raise argparse.ArgumentTypeError("region 格式应为 START-END")
    start_text, end_text = value.split("-", 1)
    start, end = _parse_timestamp(start_text), _parse_timestamp(end_text)
    if end <= start:
        raise argparse.ArgumentTypeError("region 结束时间必须晚于开始时间")
    return start, end


def cmd_repair(args: argparse.Namespace) -> int:
    """选择性重转录可疑 ASR 区域，RAW 永不覆盖。"""
    from lecture_ai.repair import RepairPipeline

    config = _bootstrap(args)
    outcome = RepairPipeline(config).run(
        args.session_id,
        dry_run=args.dry_run,
        region=args.region,
        force=args.force,
    )
    if outcome.dry_run:
        out(f"DRY RUN · {outcome.session_id} · {outcome.message}")
        for item in outcome.regions:
            out(
                f"  region {item['region_id']:02d}  "
                f"{hhmmss(item['window_start'])}-{hhmmss(item['window_end'])}  "
                f"segments={item['segment_ids']}  reasons={','.join(item['reasons'])}"
            )
        return EXIT_OK
    marker = "复用" if outcome.reused else "完成"
    out(
        f"✔ {outcome.session_id}  修复{marker}：检测 {outcome.regions_detected} 个区域，"
        f"接受 {outcome.regions_accepted} 个（{outcome.elapsed_sec:.1f} 秒）"
    )
    if outcome.output_json:
        out(f"  JSON  {outcome.output_json}")
        out(f"  MD    {outcome.output_md}")
    return EXIT_OK


def cmd_clean(args: argparse.Namespace) -> int:
    """分块清洗 REPAIRED（缺失时回退 RAW）并协调重叠边界。"""
    from lecture_ai.cleaning import CleanPipeline

    config = _bootstrap(args)
    outcome = CleanPipeline(config).run(
        args.session_id,
        dry_run=args.dry_run,
        chunk=args.chunk,
        force=args.force,
    )
    if outcome.dry_run:
        out(
            f"DRY RUN · {outcome.session_id} · source={outcome.source_layer} · "
            f"{outcome.message}"
        )
        for item in outcome.chunks:
            out(
                f"  chunk {item['index']:02d}  core="
                f"{hhmmss(item['core_start'])}-{hhmmss(item['core_end'])}  "
                f"window={hhmmss(item['window_start'])}-{hhmmss(item['window_end'])}  "
                f"segments={len(item['segment_ids'])}"
            )
        return EXIT_OK
    if outcome.partial:
        out(f"✔ {outcome.session_id}  {outcome.message}")
        return EXIT_OK
    marker = "复用" if outcome.reused else "完成"
    out(
        f"✔ {outcome.session_id}  清洗{marker}：{outcome.chunks_processed} 块，"
        f"{outcome.boundaries_processed} 个边界（{outcome.elapsed_sec:.1f} 秒）"
    )
    if outcome.output_json:
        out(f"  JSON  {outcome.output_json}")
        out(f"  MD    {outcome.output_md}")
    return EXIT_OK


def cmd_watch(args: argparse.Namespace) -> int:
    """长驻监听 incoming 目录。"""
    from lecture_ai.pipeline import Watcher

    config = _bootstrap(args)
    return Watcher(config).run(max_iterations=args.max_iterations)


def cmd_reindex(args: argparse.Namespace) -> int:
    """从磁盘 metadata.json 重建 SQLite 索引。"""
    from lecture_ai.database import Database
    from lecture_ai.session import SessionManager

    config = _bootstrap(args)
    manager = SessionManager(config, Database(config.paths.database))
    n = manager.rebuild_index()
    out(f"已重建索引，共 {n} 个 session")
    return EXIT_OK


def cmd_export(args: argparse.Namespace) -> int:
    """Phase 4 才实现：导出到 Obsidian Vault。"""
    out("export 属于 Phase 4（Obsidian 集成），当前尚未实现。")
    out("Phase 1 的产物在：data/sessions/<session_id>/transcript/")
    return EXIT_FAILURE


# --------------------------------------------------------------------------- 解析


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lecture-ai",
        description="课堂自动笔记系统 —— 录音转录、选择性修复与忠实清洗",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型用法：\n"
            "  lecture-ai doctor              检查环境是否就绪\n"
            "  lecture-ai watch               后台监听，自动处理新录音\n"
            "  lecture-ai scan                手动扫描一次（只建 session 不转录）\n"
            "  lecture-ai probe <audio>       检查录音元数据与起始时间\n"
            "  lecture-ai process --all       转录所有待处理 session\n"
            "  lecture-ai status              查看总览\n"
            "  lecture-ai status <session>    查看单个 session 详情\n"
            "  lecture-ai retry <session>     重试失败的 session\n"
            "  lecture-ai repair <session>    选择性重转录可疑区域\n"
            "  lecture-ai clean <session>     分块清洗修复后的转录\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"lecture-ai {__version__}")
    parser.add_argument("--config", help="指定 config.yaml 路径")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    sub.add_parser("init", help="初始化目录与数据库").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="环境体检").set_defaults(func=cmd_doctor)
    sub.add_parser("scan", help="扫描 incoming 并建立 session").set_defaults(func=cmd_scan)

    p_probe = sub.add_parser("probe", help="检查音频元数据与起始时间推断")
    p_probe.add_argument("audio", help="要检查的手机录音文件")
    p_probe.set_defaults(func=cmd_probe)

    p_proc = sub.add_parser("process", help="转录 session")
    p_proc.add_argument("session_id", nargs="?", help="要处理的 session")
    p_proc.add_argument("--all", action="store_true", help="处理全部待处理 session")
    p_proc.add_argument("--scan", action="store_true", help="处理前先扫描 incoming")
    p_proc.add_argument("--force", action="append",
                        choices=["preprocess", "transcribe"],
                        help="强制重跑指定步骤（可多次指定）")
    p_proc.set_defaults(func=cmd_process)

    p_status = sub.add_parser("status", help="查看状态")
    p_status.add_argument("session_id", nargs="?", help="留空则显示总览")
    p_status.add_argument("--limit", type=int, default=20, help="总览显示条数")
    p_status.set_defaults(func=cmd_status)

    p_list = sub.add_parser("sessions", help="列出所有 session")
    p_list.add_argument("--limit", type=int, default=50)
    p_list.set_defaults(func=cmd_sessions)

    p_retry = sub.add_parser("retry", help="重试失败的 session")
    p_retry.add_argument("session_id")
    p_retry.add_argument("--force", action="append",
                         choices=["preprocess", "transcribe"],
                         help="强制重跑指定步骤（默认从失败点继续，不重跑 ASR）")
    p_retry.set_defaults(func=cmd_retry)

    p_repair = sub.add_parser("repair", help="选择性重转录可疑 ASR 区域")
    p_repair.add_argument("session_id")
    p_repair.add_argument("--dry-run", action="store_true", help="只显示修复计划，不调用 ASR")
    p_repair.add_argument(
        "--region", type=_parse_region, metavar="START-END",
        help="只处理指定时间范围（秒或 HH:MM:SS）",
    )
    p_repair.add_argument("--force", action="store_true", help="忽略修复产物缓存后重跑")
    p_repair.set_defaults(func=cmd_repair)

    p_clean = sub.add_parser("clean", help="分块忠实清洗转录并协调边界")
    p_clean.add_argument("session_id")
    p_clean.add_argument("--dry-run", action="store_true", help="只显示分块计划，不调用 LLM")
    p_clean.add_argument("--chunk", type=int, help="只处理指定的 0-based chunk 并写缓存")
    p_clean.add_argument("--force", action="store_true", help="忽略清洗与逐块缓存后重跑")
    p_clean.set_defaults(func=cmd_clean)

    p_watch = sub.add_parser("watch", help="长驻监听 incoming 目录")
    p_watch.add_argument("--max-iterations", type=int, default=None,
                         help=argparse.SUPPRESS)  # 仅测试用
    p_watch.set_defaults(func=cmd_watch)

    sub.add_parser("reindex", help="从 metadata.json 重建数据库索引").set_defaults(func=cmd_reindex)
    sub.add_parser("export", help="导出到 Obsidian（Phase 4）").set_defaults(func=cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)

    if not getattr(args, "command", None):
        parser.print_help()
        return EXIT_USAGE

    try:
        return args.func(args)
    except ConfigError as exc:
        out(f"配置错误：{exc}")
        return EXIT_USAGE
    except LectureAIError as exc:
        out(f"错误：{exc}")
        if args.verbose:
            raise
        return EXIT_FAILURE
    except KeyboardInterrupt:
        out("\n已中断")
        return EXIT_FAILURE


if __name__ == "__main__":
    sys.exit(main())
