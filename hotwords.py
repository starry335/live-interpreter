"""Hotword glossary loading and routing for Live Interpreter.

One user-facing file drives every supported engine:

* ASR side (Paraformer-Realtime-V2): the canonical source surfaces are
  registered as a ``speech-biasing`` vocabulary and passed as
  ``vocabulary_id`` with ``run-task``.
* Translation side (Qwen-MT): source -> target pairs are passed in
  ``translation_options.terms`` (or ``translation.corpus.phrases`` for
  LiveTranslate).
* General text models: a compact glossary block is added to the system
  prompt.
* Output guard: ``!wrong => canonical`` lines rewrite forbidden output.

Accepted file formats
---------------------

UTF-8 text (the recommended ``.txt`` form)::

    # comments start with # at the beginning of a line
    あられ => 阿拉蕾          # ASR term + translation term
    ゆのち => 由乃亲
    ユノ                      # ASR-only term
    ~戦国ユーノ => 由乃        # translation-only alias (skip ASR)
    !战国由乃 => 由乃          # forbidden output -> replacement

Sections are optional and make long lists easier to read::

    [hotwords]
    あられ => 阿拉蕾
    [aliases]
    ラカマチオルレ => 阿拉蕾
    [asr]
    ユノ
    [forbidden]
    战国由乃 => 由乃

A ``kb-terms/v1`` JSON file (``person`` / ``honorific`` / ``works`` /
``gags`` / ``forbidden``) is accepted as well; this is the format used by
the offline Mugen translation project.  For JSON, forbidden Chinese forms
are mapped back to a canonical target when one of the known targets is a
substring of the wrong form.

Limits mirror the provider APIs: Paraformer accepts at most 500 hotwords
and each non-ASCII term is at most 15 characters.  Longer terms are kept
for translation but skipped for ASR.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

PARAFORMER_ASR_MODEL = "paraformer-realtime-v2"
MAX_ASR_TERMS = 500
MAX_ASR_CHARS = 15
MIN_SURFACE_CHARS = 2
MAX_MT_TERMS = 200
MAX_MT_TERMS_PER_REQUEST = 48
MAX_PROMPT_CHARS = 2400
MAX_TERM_CHARS = 80
MAX_FILE_BYTES = 2 * 1024 * 1024
DEFAULT_WEIGHT = 4

LOGGER = logging.getLogger("hotwords")

_HEADER_RE = re.compile(r"^\[([A-Za-z_-]+)\]$")
_KANA_RE = re.compile(r"[ぁ-ゖァ-ヺ]")
_SECTIONS = {"hotwords", "terms", "aliases", "asr", "forbidden"}


class HotwordError(ValueError):
    """Invalid or unreadable hotword file."""


@dataclass(frozen=True)
class Term:
    source: str
    target: str


@dataclass
class HotwordConfig:
    path: str
    pairs: List[Term] = field(default_factory=list)
    aliases: List[Term] = field(default_factory=list)
    forbidden: List[Term] = field(default_factory=list)
    asr_only: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def asr_terms(self) -> List[str]:
        """Source surfaces safe to send to Paraformer speech-biasing."""
        values = [term.source for term in self.pairs] + list(self.asr_only)
        result: List[str] = []
        seen = set()
        for value in values:
            text = value.strip()
            if not text or text in seen:
                continue
            if "#" in text or "\n" in text or not (MIN_SURFACE_CHARS <= len(text) <= MAX_ASR_CHARS):
                continue
            seen.add(text)
            result.append(text)
            if len(result) >= MAX_ASR_TERMS:
                break
        return result

    def mt_terms(self) -> List[Term]:
        """Source -> target pairs for Qwen-MT / LiveTranslate corpora."""
        result: List[Term] = []
        seen = set()
        for term in list(self.pairs) + list(self.aliases):
            source = term.source.strip()
            target = term.target.strip()
            if not source or not target or source in seen:
                continue
            seen.add(source)
            result.append(Term(source, target))
            if len(result) >= MAX_MT_TERMS:
                break
        return result

    def translation_phrases(self) -> Dict[str, str]:
        return {term.source: term.target for term in self.mt_terms()}

    def select_mt_terms(self, text: str, limit: int = MAX_MT_TERMS_PER_REQUEST) -> List[Term]:
        """Terms actually present in ``text``, longest source first.

        Qwen-MT degrades when dozens of unrelated terms are sent at once (and
        a short term such as ユノ can win over the longer 戦国ユーノ), so each
        request only carries the terms that can occur in this sentence.
        """
        haystack = (text or "").casefold()
        if not haystack:
            return []
        matched = [
            term for term in self.mt_terms()
            if len(term.source.strip()) >= MIN_SURFACE_CHARS
            and term.source.casefold() in haystack
        ]
        matched.sort(key=lambda term: (-len(term.source), term.source))
        # Drop a shorter term when a longer matched term covers it (ゆのち
        # wins over ゆの, 戦国ユーノ wins over ユノ); otherwise the model can
        # follow the short term and keep the wrong prefix/suffix.
        kept: List[Term] = []
        for term in matched:
            if any(term.source in longer.source for longer in kept):
                continue
            kept.append(term)
            if len(kept) >= max(1, limit):
                break
        return kept

    def prompt_block(self, max_chars: int = MAX_PROMPT_CHARS) -> str:
        """Compact prompt fragment for general chat models."""
        terms = self.mt_terms()
        if not terms:
            return ""
        prefix = (
            "Terminology (source=>target; always use the target, including "
            "when the source is a known ASR misrecognition): "
        )
        budget = max(0, max_chars - len(prefix))
        pieces = []
        used = 0
        for term in terms:
            piece = f"{term.source}=>{term.target}"
            addition = len(piece) + (2 if pieces else 0)
            if used + addition > budget:
                break
            pieces.append(piece)
            used += addition
        if not pieces:
            return ""
        return prefix + "; ".join(pieces)

    def postprocess(self, text: str) -> str:
        """Rewrite forbidden output forms to their canonical replacement."""
        out = text
        for term in sorted(self.forbidden, key=lambda item: -len(item.source)):
            if term.source and term.source != term.target:
                out = out.replace(term.source, term.target)
        return out.strip()

    def summary(self) -> Dict[str, object]:
        return {
            "path": self.path,
            "pairs": len(self.pairs),
            "aliases": len(self.aliases),
            "asr_only": len(self.asr_only),
            "forbidden": len(self.forbidden),
            "asr_terms": len(self.asr_terms()),
            "mt_terms": len(self.mt_terms()),
        }


def _split_line(line: str) -> Tuple[str, str]:
    for separator in ("=>", "→", "->", "\t"):
        if separator in line:
            source, target = line.split(separator, 1)
            return source.strip(), target.strip()
    return line.strip(), ""


def _derive_forbidden_target(source: str, canonical_targets: Iterable[str]) -> str:
    matches = [target for target in canonical_targets if target and target in source]
    return max(matches, key=len) if matches else ""


def _add_term(
    items: List[Term], seen: Dict[str, str], source: str, target: str,
    category: str, warnings: List[str], path: str,
) -> None:
    if not source or not target:
        warnings.append(f"{path}: skipped empty {category} entry ({source!r} => {target!r})")
        return
    if len(source) > MAX_TERM_CHARS or len(target) > MAX_TERM_CHARS:
        warnings.append(f"{path}: skipped oversized {category} entry ({source[:24]!r}...)")
        return
    if _KANA_RE.search(target):
        warnings.append(
            f"{path}: {category} target {target!r} contains Japanese kana; check for an untranslated target"
        )
    previous = seen.get(source)
    if previous is not None:
        if previous != target:
            warnings.append(
                f"{path}: duplicate {category} source {source!r}; keeping {previous!r}, ignoring {target!r}"
            )
        return
    seen[source] = target
    items.append(Term(source, target))


def _load_text(path: Path) -> HotwordConfig:
    config = HotwordConfig(str(path))
    pairs_seen: Dict[str, str] = {}
    aliases_seen: Dict[str, str] = {}
    forbidden_seen: Dict[str, str] = {}
    asr_seen = set()
    section = "hotwords"
    canonical_targets = [item.target for item in config.pairs]

    try:
        raw = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HotwordError(f"{path}: hotword file must be UTF-8 text") from exc
    except OSError as exc:
        raise HotwordError(f"{path}: cannot read hotword file: {exc}") from exc

    for number, raw_line in enumerate(raw.splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        header = _HEADER_RE.match(line)
        if header:
            name = header.group(1).lower()
            if name not in _SECTIONS:
                config.warnings.append(f"{path}:{number}: unknown section [{name}], ignored")
                section = "hotwords"
            else:
                section = name
            continue

        marker = ""
        if line[:1] in {"~", "!"}:
            marker, line = line[0], line[1:].strip()
        source, target = _split_line(line)
        if not source:
            config.warnings.append(f"{path}:{number}: empty term, ignored")
            continue

        if marker == "!" or section == "forbidden":
            if not target:
                target = _derive_forbidden_target(source, canonical_targets)
            if not target:
                config.warnings.append(
                    f"{path}:{number}: forbidden entry {source!r} has no replacement; ignored"
                )
                continue
            _add_term(config.forbidden, forbidden_seen, source, target, "forbidden", config.warnings, str(path))
            continue

        if marker == "~" or section == "aliases":
            if not target:
                config.warnings.append(
                    f"{path}:{number}: alias {source!r} needs a target, ignored"
                )
                continue
            _add_term(config.aliases, aliases_seen, source, target, "alias", config.warnings, str(path))
            continue

        if target:
            _add_term(config.pairs, pairs_seen, source, target, "term", config.warnings, str(path))
            canonical_targets.append(target)
        elif section == "hotwords" or section == "terms" or section == "asr":
            if source not in asr_seen:
                asr_seen.add(source)
                config.asr_only.append(source)
        else:
            config.warnings.append(f"{path}:{number}: entry without target, ignored")

    _finish(config, path)
    return config


def _load_kb_terms(path: Path) -> HotwordConfig:
    try:
        document = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HotwordError(f"{path}: cannot read kb-terms JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise HotwordError(f"{path}: kb-terms JSON must be an object")

    config = HotwordConfig(str(path))
    pairs_seen: Dict[str, str] = {}
    forbidden_seen: Dict[str, str] = {}
    for section in ("person", "honorific", "works", "gags"):
        mapping = document.get(section) or {}
        if not isinstance(mapping, dict):
            config.warnings.append(f"{path}: section {section!r} is not an object, ignored")
            continue
        for source, target in mapping.items():
            if isinstance(source, str) and isinstance(target, str):
                _add_term(config.pairs, pairs_seen, source, target, section, config.warnings, str(path))
    canonical_targets = [item.target for item in config.pairs]
    forbidden = document.get("forbidden") or []
    if isinstance(forbidden, list):
        for source in forbidden:
            if not isinstance(source, str):
                continue
            target = _derive_forbidden_target(source, canonical_targets)
            if not target:
                config.warnings.append(
                    f"{path}: forbidden JSON entry {source!r} has no inferable replacement; ignored"
                )
                continue
            _add_term(config.forbidden, forbidden_seen, source, target, "forbidden", config.warnings, str(path))
    if document.get("keep_original"):
        config.warnings.append(
            f"{path}: keep_original entries are not enforced by live subtitles; ignored"
        )
    _finish(config, path)
    return config


def _finish(config: HotwordConfig, path: Path) -> None:
    if not config.pairs and not config.aliases and not config.asr_only:
        config.warnings.append(f"{path}: no usable hotword terms found")
    if len(config.pairs) + len(config.aliases) > MAX_MT_TERMS:
        config.warnings.append(
            f"{path}: {len(config.pairs) + len(config.aliases)} translation terms exceed "
            f"the {MAX_MT_TERMS}-term request cap; only the first {MAX_MT_TERMS} are used"
        )
    raw_asr = len(config.pairs) + len(config.asr_only)
    if raw_asr > MAX_ASR_TERMS:
        config.warnings.append(
            f"{path}: {raw_asr} ASR surfaces exceed the {MAX_ASR_TERMS}-term vocabulary cap; "
            f"only the first {MAX_ASR_TERMS} are registered"
        )


def load(path: str | Path) -> HotwordConfig:
    """Load a ``.txt`` hotword list or a ``kb-terms/v1`` JSON file."""
    if not path:
        raise HotwordError("hotword path is empty")
    target = Path(path).expanduser()
    if not target.exists():
        raise HotwordError(f"hotword file not found: {target}")
    if not target.is_file():
        raise HotwordError(f"hotword path is not a file: {target}")
    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HotwordError(f"{target}: cannot stat hotword file: {exc}") from exc
    if size > MAX_FILE_BYTES:
        raise HotwordError(f"{target}: hotword file is larger than {MAX_FILE_BYTES} bytes")
    if target.suffix.lower() == ".json":
        return _load_kb_terms(target)
    try:
        head = target.read_text(encoding="utf-8-sig").lstrip()
    except (OSError, UnicodeDecodeError) as exc:
        raise HotwordError(f"{target}: cannot read hotword file: {exc}") from exc
    if head.startswith("{"):
        return _load_kb_terms(target)
    return _load_text(target)


def default_cache_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "LiveInterpreter" / "hotword-vocabularies.json"
    return Path.home() / ".cache" / "live-interpreter" / "hotword-vocabularies.json"


def _read_cache(path: Path) -> Dict[str, object]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    entries = document.get("entries") if isinstance(document, dict) else None
    return entries if isinstance(entries, dict) else {}


def _write_cache(path: Path, entries: Dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"version": 1, "entries": entries}, ensure_ascii=False, indent=1)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", delete=False, dir=str(path.parent), prefix=".hotwords-", suffix=".tmp"
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    finally:
        if os.path.exists(handle.name):
            try:
                os.unlink(handle.name)
            except OSError:
                pass


def _vocabulary_host(workspace_id: str) -> str:
    workspace = workspace_id.strip()
    return f"{workspace}.cn-beijing.maas.aliyuncs.com" if workspace else "dashscope.aliyuncs.com"


def _post_json(
    url: str, api_key: str, payload: Dict[str, object], timeout: float,
    ssl_context=None,
) -> Dict[str, object]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "LiveInterpreter/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
        data = json.loads(response.read())
    if not isinstance(data, dict):
        raise HotwordError("speech-biasing returned a non-object response")
    return data


def ensure_vocabulary(
    api_key: str,
    workspace_id: str,
    terms: Iterable[str],
    target_model: str = PARAFORMER_ASR_MODEL,
    cache_path: Optional[Path] = None,
    timeout: float = 15.0,
    ssl_context=None,
) -> str:
    """Return a ``vocabulary_id`` for ``terms``, reusing a persistent cache.

    The provider only accepts a pre-built vocabulary for Paraformer, not an
    inline word list.  Creation is cached by host + target model + terms so
    reconnects and later runs do not create duplicate lists.
    """
    key_text = [str(term).strip() for term in terms if str(term).strip()]
    if not key_text:
        raise HotwordError("cannot create a vocabulary without hotword terms")
    if not api_key.strip():
        raise HotwordError("missing DASHSCOPE_API_KEY for hotword vocabulary creation")

    host = _vocabulary_host(workspace_id)
    digest = hashlib.sha256(
        (host + "\n" + target_model + "\n" + "\n".join(key_text)).encode("utf-8")
    ).hexdigest()
    prefix = "li" + digest[:8]
    cache_file = cache_path or default_cache_path()
    entries = _read_cache(cache_file)
    cached = entries.get(digest)
    if isinstance(cached, dict) and str(cached.get("vocabulary_id") or "").strip():
        return str(cached["vocabulary_id"])

    url = f"https://{host}/api/v1/services/audio/asr/customization"

    # A lost cache must not create a second list with the same prefix: list
    # first and reuse an OK list from a previous run.
    try:
        listing = _post_json(
            url, api_key,
            {
                "model": "speech-biasing",
                "input": {
                    "action": "list_vocabulary", "prefix": prefix,
                    "page_index": 0, "page_size": 10,
                },
            },
            timeout, ssl_context,
        )
        output = listing.get("output") or {}
        for item in output.get("vocabulary_list") or []:
            if not isinstance(item, dict) or item.get("status") != "OK":
                continue
            vocabulary_id = str(item.get("vocabulary_id") or "").strip()
            if vocabulary_id:
                entries[digest] = {
                    "vocabulary_id": vocabulary_id,
                    "target_model": target_model,
                    "count": len(key_text),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                }
                try:
                    _write_cache(cache_file, entries)
                except OSError as exc:
                    LOGGER.warning("cannot write hotword vocabulary cache %s: %s", cache_file, exc)
                return vocabulary_id
    except (urllib.error.URLError, OSError, HotwordError, ValueError) as exc:
        LOGGER.info("cannot list hotword vocabularies for prefix %s: %s", prefix, exc)

    payload = {
        "model": "speech-biasing",
        "input": {
            "action": "create_vocabulary",
            "target_model": target_model,
            "prefix": prefix,
            "vocabulary": [
                {"text": text, "weight": DEFAULT_WEIGHT} for text in key_text[:MAX_ASR_TERMS]
            ],
        },
    }
    try:
        created = _post_json(url, api_key, payload, timeout, ssl_context)
    except urllib.error.HTTPError as exc:
        body = exc.read(1024).decode("utf-8", errors="replace")
        raise HotwordError(f"create_vocabulary failed: HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HotwordError(f"create_vocabulary failed: {exc}") from exc

    output = created.get("output") or {}
    vocabulary_id = str(output.get("vocabulary_id") or "").strip()
    if not vocabulary_id:
        request_id = created.get("request_id") or "unknown"
        raise HotwordError(f"create_vocabulary returned no vocabulary_id (request_id={request_id})")

    entries[digest] = {
        "vocabulary_id": vocabulary_id,
        "target_model": target_model,
        "count": len(key_text),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    try:
        _write_cache(cache_file, entries)
    except OSError as exc:
        LOGGER.warning("cannot write hotword vocabulary cache %s: %s", cache_file, exc)
    return vocabulary_id
