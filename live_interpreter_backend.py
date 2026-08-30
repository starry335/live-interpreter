from __future__ import annotations

import os
import sys

import gummy_realtime_worker
import live_interpreter
import qwen_livetranslate_worker


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
    args = sys.argv[1:]
    if args[:1] == ["--qwen-worker"]:
        return qwen_livetranslate_worker.main(args[1:])
    if args[:1] == ["--gummy-worker"]:
        return gummy_realtime_worker.main(args[1:])
    return live_interpreter.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
