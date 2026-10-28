"""Cheap source change checks and semantic comparison; never ask an LLM to compare logs."""
import hashlib
import json
from pathlib import Path
from sources import (ROOTS, source_paths, dependencies, workspace_dependencies,
                     file_state, parser_version, timezone_identity)


def source_signature(home, root_args, timezone):
    overrides = {}
    for item in root_args:
        key, sep, value = item.partition("=")
        if not sep or key not in ROOTS:
            raise ValueError("Invalid --root: expected known AGENT=PATH")
        overrides[key] = Path(value).expanduser().resolve()
    rows = [("timezone", timezone_identity(timezone))]
    for name, relative in ROOTS.items():
        root = overrides.get(name, home / relative)
        rows.append((name, str(root), root.exists()))
        paths = []
        for path in source_paths(name, root):
            paths.extend(dependencies(name, path))
        if name == 'cursor-db':
            paths.extend(workspace_dependencies(root))
        rows.extend(file_state(paths))
    # A parser fix must invalidate old evidence even if raw logs did not change.
    rows.append(('parser', parser_version()))
    # Coverage discovery is relevant evidence too, even without transcript changes.
    from local_stores import inventory
    rows.append(('inventory', inventory(home)))
    return digest(rows)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def evidence_signature(index_path):
    index = json.loads(index_path.read_text(encoding='utf-8'))
    sessions = [json.loads(Path(s["evidence"]).read_text(encoding='utf-8')) for s in index["sessions"]]
    sessions.sort(key=lambda s: (s["agent"], str(s["session_id"]), s["source"]))
    # Ignore scan time, extraction directory, and activity in unrelated sessions.
    coverage = {name: {k: s.get(k) for k in ("status", "errors", "undated_messages")}
                for name, s in index["coverage"].items()}
    return digest({"sessions": sessions, "coverage": coverage,
                   "warnings": index["warnings"], "additional": index.get("additional_candidates", [])})
