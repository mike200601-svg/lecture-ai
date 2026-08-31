"""ASR 档位实测脚本。

用途：在具体机器上决定 config.yaml 里该配哪个模型。
没有普适答案 —— 有没有 N 卡、CPU 几核，结论完全不同，必须实测。

用法：
    python scripts/bench_asr.py <音频文件> --models tiny,small,medium,large-v3-turbo
    python scripts/bench_asr.py <音频文件> --minutes 10      # 只跑前 10 分钟，省时间

关注两个指标：
    realtime_factor —— 音频时长 / 转录耗时。>2 才算够用（90 分钟课 45 分钟出结果）
    术语正确率      —— 脚本不会自动算，需要人工抽查输出里的专业名词
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lecture_ai.audio import convert_to_wav, get_tools, probe_audio  # noqa: E402
from lecture_ai.config import load_config  # noqa: E402
from lecture_ai.logging_setup import setup_logging  # noqa: E402
from lecture_ai.transcription import TranscribeOptions, load_glossary  # noqa: E402
from lecture_ai.transcription.faster_whisper_transcriber import (  # noqa: E402
    FasterWhisperTranscriber,
)
from lecture_ai.utils.timefmt import hhmmss  # noqa: E402

DEFAULT_MODELS = "tiny,small,medium,large-v3-turbo"


def main() -> int:
    parser = argparse.ArgumentParser(description="对比不同 Whisper 模型在本机的速度")
    parser.add_argument("audio", type=Path, help="测试音频（建议 5-10 分钟真实课堂录音）")
    parser.add_argument("--models", default=DEFAULT_MODELS, help="逗号分隔的模型列表")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--minutes", type=float, default=None, help="只测前 N 分钟")
    parser.add_argument("--course", default="quantum_mechanics", help="用哪门课的术语表")
    parser.add_argument("--out", type=Path, default=None, help="把各模型转录文本写到该目录")
    args = parser.parse_args()

    if not args.audio.exists():
        print(f"音频不存在：{args.audio}")
        return 2

    config = load_config()
    setup_logging(config.paths.log_dir, config.logging)
    tools = get_tools(config.audio.ffmpeg_path, config.audio.ffprobe_path)

    work_dir = config.paths.cache_dir / "bench"
    work_dir.mkdir(parents=True, exist_ok=True)
    wav = work_dir / "bench_16k.wav"

    print(f"准备音频：{args.audio.name}")
    convert_to_wav(args.audio, wav, tools, sample_rate=16000, channels=1)
    if args.minutes:
        trimmed = work_dir / "bench_trimmed.wav"
        _trim(tools, wav, trimmed, args.minutes * 60)
        wav = trimmed

    duration = probe_audio(wav, tools).duration_sec
    glossary = load_glossary(
        config.glossary_dir, f"{args.course}.txt" if args.course else None
    )
    options = TranscribeOptions(
        language=config.transcription.local_whisper.language,
        hotwords=glossary.as_hotwords(),
        vad_filter=True,
    )

    print(f"测试音频时长：{hhmmss(duration)}  |  术语 {len(glossary)} 条")
    print(f"device={args.device}  compute_type={args.compute_type}")
    print("=" * 78)
    print(f"{'模型':<20} {'加载(s)':>10} {'转录(s)':>10} {'实时倍率':>10} {'段数':>8}")
    print("-" * 78)

    rows = []
    for model in [m.strip() for m in args.models.split(",") if m.strip()]:
        try:
            row = _bench_one(model, wav, duration, options, args, config)
        except Exception as exc:  # 单个模型失败不影响其余对比
            print(f"{model:<20} 失败：{exc}")
            continue
        rows.append(row)
        print(f"{row['model']:<20} {row['load']:>10.1f} {row['transcribe']:>10.1f} "
              f"{row['rtf']:>9.2f}x {row['segments']:>8}")
        if args.out:
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / f"{model.replace('/', '_')}.txt").write_text(
                row["text"], encoding="utf-8"
            )

    print("=" * 78)
    if rows:
        best = max(rows, key=lambda r: r["rtf"])
        print(f"最快：{best['model']}（{best['rtf']:.2f}x 实时）")
        print(f"90 分钟课堂录音预计耗时：")
        for r in rows:
            print(f"  {r['model']:<20} {90 / r['rtf']:6.1f} 分钟")
        print()
        print("速度只是一半 —— 请人工抽查各模型输出里的专业术语正确率再做决定。")
        if args.out:
            print(f"转录文本已写入：{args.out}")
    return 0


def _bench_one(model: str, wav: Path, duration: float, options, args, config) -> dict:
    transcriber = FasterWhisperTranscriber(
        model=model,
        device=args.device,
        compute_type=args.compute_type,
        download_root=config.paths.cache_dir / "models",
    )
    t0 = time.monotonic()
    transcriber._load()
    load_elapsed = time.monotonic() - t0

    t1 = time.monotonic()
    result = transcriber.transcribe(wav, options)
    elapsed = time.monotonic() - t1
    transcriber.close()

    return {
        "model": model,
        "load": load_elapsed,
        "transcribe": elapsed,
        "rtf": duration / elapsed if elapsed else 0.0,
        "segments": len(result.segments),
        "text": "\n".join(f"[{hhmmss(s.start)}] {s.text}" for s in result.segments),
    }


def _trim(tools, src: Path, dst: Path, seconds: float) -> None:
    import subprocess

    subprocess.run(
        [tools.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(src), "-t", str(seconds), "-c", "copy", str(dst)],
        check=True,
    )


if __name__ == "__main__":
    sys.exit(main())
