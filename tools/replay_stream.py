#!/usr/bin/env python3
"""Replay a 16 kHz mono WAV into a cloud worker at real time.

This is the reproduction entry for the v0.3 gold streaming set.  It spawns
``qwen_asr_stream_worker.py`` (or a frozen ``LiveInterpreterBackend.exe``),
feeds PCM at 1x, and records every stdout JSON event with ``_recv_mono``
(seconds since the first audio frame).  Feed the resulting ``events.jsonl``
to bangdream-hotword-bench ``skill/scripts/eval_stream.py``.

Examples:
  set DASHSCOPE_API_KEY=sk-...
  python tools/replay_stream.py --wav gold-v0.3/audio/xxx.wav --out runs/31_ref \\
    -- --model qwen-audio-3.1-asr-flash-streaming --max-end-silence 200 \\
       --vad-model near_meeting_16k --mt-model qwen3.8-max --mt-mode reference \\
       --hotwords hotwords/yumemita_bangdream.txt

  # frozen build
  python tools/replay_stream.py --exe dist/LiveInterpreterBackend/LiveInterpreterBackend.exe \\
    --entry --qwen-asr-worker --wav ... --out ...
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WORKER = ROOT / "qwen_asr_stream_worker.py"


def parse_args():
    parser = argparse.ArgumentParser(description="Replay a WAV into a Live Interpreter cloud worker.")
    parser.add_argument("--wav", required=True, help="16 kHz mono 16-bit WAV")
    parser.add_argument("--out", required=True, help="Output directory for events.jsonl/meta.json")
    parser.add_argument("--start", type=float, default=0.0, help="Start offset in the WAV (seconds)")
    parser.add_argument("--seconds", type=float, default=None, help="Clip length; default: to EOF")
    parser.add_argument("--speed", type=float, default=1.0, help="1.0 = real time")
    parser.add_argument("--worker", default=str(DEFAULT_WORKER))
    parser.add_argument("--exe", default="", help="Frozen backend exe; worker path is ignored")
    parser.add_argument("--entry", default="", help="Frozen backend entry flag, e.g. --qwen-asr-worker")
    parser.add_argument("--startup-wait", type=float, default=40.0)
    return parser.parse_known_args()


def main() -> int:
    args, extra = parse_args()
    worker_args = extra[1:] if extra[:1] == ["--"] else extra
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        print("DASHSCOPE_API_KEY is not set", file=sys.stderr)
        return 2

    with wave.open(args.wav, "rb") as wav:
        if wav.getnchannels() != 1 or wav.getframerate() != 16000 or wav.getsampwidth() != 2:
            raise SystemExit("WAV must be 16 kHz mono 16-bit PCM")
        wav.setpos(int(args.start * 16000))
        total = wav.getnframes() - int(args.start * 16000)
        if args.seconds is not None:
            total = min(total, int(args.seconds * 16000))
        pcm = wav.readframes(total)

    cmd = (
        [args.exe, args.entry, *worker_args] if args.exe
        else [sys.executable, args.worker, *worker_args]
    )
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    started_wall = time.time()
    proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, bufsize=0, env=env,
    )
    events = []
    stderr_lines = []
    ready = threading.Event()
    started = time.monotonic()

    def read_stdout():
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                event = {"event": "raw", "text": line}
            event["_recv_mono"] = round(time.monotonic() - started, 4)
            event["_recv_wall"] = round(time.time(), 4)
            events.append(event)
            if event.get("event") == "ready":
                ready.set()

    def read_stderr():
        for raw in iter(proc.stderr.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                stderr_lines.append(line)

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    deadline = time.monotonic() + args.startup_wait
    while not ready.wait(0.05) and time.monotonic() < deadline:
        if proc.poll() is not None:
            break
    if not ready.is_set():
        print("worker did not become ready", file=sys.stderr)
        print("\n".join(stderr_lines[-20:]), file=sys.stderr)
        return 1

    feed_started = time.monotonic()
    feed_started_wall = time.time()
    frame_bytes = 3200  # 100 ms
    interval = 0.1 / max(0.01, args.speed)
    next_at = time.monotonic()
    for offset in range(0, len(pcm) - frame_bytes + 1, frame_bytes):
        try:
            proc.stdin.write(pcm[offset:offset + frame_bytes])
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            break
        next_at += interval
        delay = next_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    try:
        proc.stdin.close()
    except OSError:
        pass
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    meta = {
        "wav": str(Path(args.wav).resolve()),
        "start": args.start,
        "seconds": len(pcm) / 32000,
        "speed": args.speed,
        "cmd": cmd,
        "started_wall": started_wall,
        "feed_started_wall": feed_started_wall,
        "startup_offset_s": round(feed_started - started, 3),
        "proc_returncode": proc.returncode,
        "stderr_tail": stderr_lines[-50:],
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    finals = sum(1 for e in events if e.get("event") == "result" and e.get("source_final"))
    translations = sum(1 for e in events if e.get("event") == "result" and e.get("translation_final"))
    print(json.dumps({"out": str(out), "seconds": round(len(pcm) / 32000, 1),
                      "events": len(events), "finals": finals,
                      "translations": translations, "returncode": proc.returncode}, ensure_ascii=False))
    return 0 if proc.returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
