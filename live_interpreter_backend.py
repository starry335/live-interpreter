from __future__ import annotations

import sys

import gummy_realtime_worker
import live_interpreter
import qwen_livetranslate_worker


def main() -> int:
    args = sys.argv[1:]
    if args[:1] == ["--qwen-worker"]:
        return qwen_livetranslate_worker.main(args[1:])
    if args[:1] == ["--gummy-worker"]:
        return gummy_realtime_worker.main(args[1:])
    return live_interpreter.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
