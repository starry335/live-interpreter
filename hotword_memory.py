"""User-maintained source/target terminology, shared by the launcher and workers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile


def validate_memory(value):
    if not isinstance(value, dict) or type(value.get("enabled")) is not bool or not isinstance(value.get("terms"), dict):
        raise ValueError("热词文件格式错误，应包含 enabled 和 terms。")
    terms = value["terms"]
    use_builtin = value.get("use_builtin", True)
    if type(use_builtin) is not bool:
        raise ValueError("内置词表开关必须为 true 或 false。")
    if len(terms) > 200:
        raise ValueError("最多保存 200 条热词。")
    for source, target in terms.items():
        if not isinstance(source, str) or not isinstance(target, str) or not source.strip() or not target.strip():
            raise ValueError("每条热词都需要原词和固定译法。")
        if len(source) > 200 or len(target) > 200 or any(ch in source + target for ch in "\r\n\t\0"):
            raise ValueError("原词和译法各限 200 字，不能包含换行、制表符或空字符。")
        if source != source.strip() or target != target.strip():
            raise ValueError("原词和译法的首尾不能包含空白。")
    if sum(len(source) + len(target) for source, target in terms.items()) > 20000:
        raise ValueError("热词总长度不能超过 20000 字。")
    return {"enabled": value["enabled"], "terms": dict(terms), "use_builtin": use_builtin}


def parse_hotwords(text):
    terms = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = re.split(r"=>|=|\t", line, maxsplit=1)
        if len(parts) != 2 or not all(part.strip() for part in parts):
            raise ValueError(f"第 {number} 行格式错误，请使用：原词 = 固定译法。")
        source, target = (part.strip() for part in parts)
        if source in terms and terms[source] != target:
            raise ValueError(f"第 {number} 行原词重复，且译法不同：{source}")
        terms[source] = target
    return validate_memory({"enabled": True, "terms": terms})["terms"]


def load_memory(path):
    return validate_memory(json.loads(Path(path).read_text(encoding="utf-8-sig")))


def load_hotwords(path):
    if not path:
        return {}
    memory = load_memory(path)
    return memory["terms"] if memory["enabled"] else {}


def save_memory(path, memory):
    memory = validate_memory(memory)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(memory, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
