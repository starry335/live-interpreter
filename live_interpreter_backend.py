from __future__ import annotations

import os
import sys

import gummy_realtime_worker
import live_interpreter
import qwen_livetranslate_worker
import paraformer_mt_worker
import qwen_asr_stream_worker
import runtime_safety


def configure_utf8() -> None:
    os.environ["PYTHONIOENCODING"] = "utf-8"
    os.environ["PYTHONUTF8"] = "1"
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main() -> int:
    configure_utf8()
    runtime_safety.install("backend", os.environ.get("LIVE_INTERPRETER_WORK_DIR", ""))
    args = sys.argv[1:]
    if args[:1] == ["--qwen-asr-worker"]:
        return runtime_safety.guard("qwen-asr", qwen_asr_stream_worker.main, args[1:])
    if args[:1] == ["--paraformer-mt-worker"]:
        return runtime_safety.guard("paraformer-mt", paraformer_mt_worker.main, args[1:])
    if args[:1] == ["--qwen-worker"]:
        return runtime_safety.guard("qwen-live", qwen_livetranslate_worker.main, args[1:])
    if args[:1] == ["--gummy-worker"]:
        return runtime_safety.guard("gummy", gummy_realtime_worker.main, args[1:])
    return runtime_safety.guard("backend-main", live_interpreter.main, args)


if __name__ == "__main__":
    raise SystemExit(main())
