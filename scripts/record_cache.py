"""Reuse normalized, redacted records; never skip evidence merely because a file is old."""
import json
from pathlib import Path
from sources import dependencies, workspace_dependencies, file_state, digest
from storage import atomic_json


class RecordCache:
    def __init__(self, directory, version):
        self.directory = directory
        self.version = version
        self.hits = 0
        self.misses = 0

    def records(self, source, path, loader, warnings, stamp, redact):
        deps = dependencies(source, path)
        if source == 'cursor-db':
            deps.extend(workspace_dependencies(path))
        signature = digest([self.version, source, file_state(deps)])
        target = self.directory / (digest([source, str(path)]) + '.json')
        try:
            cached = json.loads(target.read_text(encoding='utf-8'))
            if cached.get('signature') == signature:
                rows = cached['records']
                if not isinstance(rows, list) or any(not isinstance(r, list) or len(r) != 6 for r in rows):
                    raise ValueError('invalid cached rows')
                self.hits += 1
                for sid, cwd, role, ts, text, ref in rows:
                    yield sid, cwd, role, stamp(ts), text, ref
                return
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        self.misses += 1
        before = len(warnings)
        rows = []
        for sid, cwd, role, ts, text, ref in loader():
            if role not in ('user', 'assistant') or not isinstance(text, str) or not text.strip():
                continue
            text = redact(text)
            rows.append([sid, cwd, role, ts.isoformat() if ts else None, text, ref])
            yield sid, cwd, role, ts, text, ref
        # Never turn a partial/failed parse or an in-flight append into a valid cache hit.
        if len(warnings) == before and signature == digest([self.version, source, file_state(deps)]):
            atomic_json(target, {'signature': signature, 'records': rows})
