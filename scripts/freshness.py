"""Cheap source change checks and semantic comparison; never ask an LLM to compare logs."""
import hashlib
import json
from pathlib import Path
from collect import ROOTS


PATTERNS = {"gemini": "session-*.json", "zcode-desktop": "session_*.json",
            "kimi": "wire.jsonl", "kimi-code": "wire.jsonl",
            "deepseek-harness": "session*.jsonl*"}


def source_signature(home, root_args, timezone):
    overrides = {}
    for item in root_args:
        key, sep, value = item.partition("=")
        if not sep or key not in ROOTS:
            raise ValueError("Invalid --root: expected known AGENT=PATH")
        overrides[key] = Path(value).expanduser().resolve()
    rows = [("timezone", timezone)]
    for name, relative in ROOTS.items():
        root = overrides.get(name, home / relative)
        rows.append((name, str(root), root.exists()))
        if root.is_file():
            paths = [root, Path(str(root) + "-wal")]
        else:
            paths = sorted(root.rglob(PATTERNS.get(name, "*.jsonl")))
            if name == "cursor":
                paths = [p for p in paths if "agent-transcripts" in p.parts]
        for path in paths:
            try:
                stat = path.stat()
                rows.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                rows.append((str(path), "missing"))
    # A parser fix must invalidate old evidence even if raw logs did not change.
    for script in sorted(Path(__file__).parent.glob("*.py")):
        rows.append((script.name, hashlib.sha256(script.read_bytes()).hexdigest()))
    return digest(rows)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evidence_signature(index_path):
    index = json.loads(index_path.read_text())
    sessions = [json.loads(Path(s["evidence"]).read_text()) for s in index["sessions"]]
    sessions.sort(key=lambda s: (s["agent"], str(s["session_id"]), s["source"]))
    # Ignore scan time, extraction directory, and activity in unrelated sessions.
    coverage = {name: {k: s.get(k) for k in ("status", "errors", "undated_messages")}
                for name, s in index["coverage"].items()}
    return digest({"sessions": sessions, "coverage": coverage,
                   "warnings": index["warnings"], "additional": index.get("additional_candidates", [])})
