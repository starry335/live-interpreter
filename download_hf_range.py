from __future__ import annotations

import argparse
import concurrent.futures
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path


def request_range(url: str, start: int, end: int, timeout: int):
    req = urllib.request.Request(url, headers={"Range": f"bytes={start}-{end}", "User-Agent": "curl/8"})
    return urllib.request.urlopen(req, timeout=timeout)


def download_part(url: str, part_path: Path, start: int, end: int, timeout: int, retries: int) -> None:
    expected = end - start + 1
    for attempt in range(1, retries + 1):
        have = part_path.stat().st_size if part_path.exists() else 0
        if have == expected:
            return
        if have > expected:
            part_path.unlink()
            have = 0
        range_start = start + have
        try:
            with request_range(url, range_start, end, timeout) as response:
                status = getattr(response, "status", response.getcode())
                if status != 206:
                    raise RuntimeError(f"server did not honor Range request: HTTP {status}")
                with part_path.open("ab") as out:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        out.write(chunk)
            if part_path.stat().st_size == expected:
                return
        except (OSError, urllib.error.URLError, TimeoutError, RuntimeError) as exc:
            if attempt == retries:
                raise RuntimeError(f"failed {part_path.name} after {retries} attempts: {exc}") from exc
            time.sleep(min(30, 2 * attempt))


def size_of(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def progress_loop(output: Path, parts_dir: Path, total: int, stop: threading.Event) -> None:
    while not stop.wait(10):
        downloaded = size_of(output)
        if parts_dir.exists():
            downloaded += sum(size_of(path) for path in parts_dir.glob("*.part"))
        pct = downloaded / total * 100 if total else 0
        print(f"progress: {downloaded}/{total} bytes ({pct:.2f}%)", flush=True)


def build_ranges(start: int, total: int, part_size: int):
    index = 0
    offset = start
    while offset < total:
        end = min(total - 1, offset + part_size - 1)
        yield index, offset, end
        offset = end + 1
        index += 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel HTTP Range downloader for large Hugging Face files.")
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--total-size", type=int, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--part-size-mb", type=int, default=64)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--retries", type=int, default=100)
    args = parser.parse_args()

    os.environ.setdefault("NO_PROXY", "*")
    os.environ.setdefault("no_proxy", "*")
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    existing = size_of(output)
    if existing > args.total_size:
        raise RuntimeError(f"output is larger than expected: {existing} > {args.total_size}")
    if existing == args.total_size:
        print(f"complete: {output}", flush=True)
        return 0

    parts_dir = output.with_name(output.name + ".parts")
    parts_dir.mkdir(parents=True, exist_ok=True)
    part_size = args.part_size_mb * 1024 * 1024
    ranges = list(build_ranges(existing, args.total_size, part_size))
    print(
        f"resume from {existing}/{args.total_size} bytes; "
        f"downloading {len(ranges)} parts with {args.workers} workers",
        flush=True,
    )

    stop = threading.Event()
    progress = threading.Thread(target=progress_loop, args=(output, parts_dir, args.total_size, stop), daemon=True)
    progress.start()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = []
            for index, start, end in ranges:
                part_path = parts_dir / f"{index:05d}.{start}-{end}.part"
                futures.append(pool.submit(download_part, args.url, part_path, start, end, args.timeout, args.retries))
            for future in concurrent.futures.as_completed(futures):
                future.result()
    finally:
        stop.set()

    with output.open("ab") as out:
        for index, start, end in ranges:
            part_path = parts_dir / f"{index:05d}.{start}-{end}.part"
            expected = end - start + 1
            if size_of(part_path) != expected:
                raise RuntimeError(f"incomplete part: {part_path}")
            with part_path.open("rb") as part:
                while True:
                    chunk = part.read(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
    if size_of(output) != args.total_size:
        raise RuntimeError(f"merged file has wrong size: {size_of(output)} != {args.total_size}")
    for path in parts_dir.glob("*.part"):
        path.unlink()
    parts_dir.rmdir()
    print(f"complete: {output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
