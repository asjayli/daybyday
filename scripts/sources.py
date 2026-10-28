"""Source discovery shared by collection, signatures and the parsed-file cache."""
import hashlib
import json
import os
from pathlib import Path

ROOTS = {
    'pi': '.pi/agent/sessions', 'claude': '.claude/projects', 'codex': '.codex/sessions',
    'gemini': '.gemini/tmp', 'cursor': '.cursor/projects',
    'cursor-db': '.config/Cursor/User/globalStorage/state.vscdb',
    'opencode': '.local/share/opencode/opencode.db', 'zcode': '.zcode/cli/db/db.sqlite',
    'zcode-desktop': '.zcode/v2/sessions', 'kimi': '.kimi/sessions',
    'kimi-code': '.kimi-code/sessions', 'deepseek-harness': '.dsh/sessions',
}
PATTERNS = {'gemini': 'session-*.json', 'zcode-desktop': 'session_*.json',
            'kimi': 'wire.jsonl', 'kimi-code': 'wire.jsonl', 'deepseek-harness': 'session*.jsonl*'}
DATABASES = {'cursor-db', 'opencode', 'zcode'}


def source_paths(source, root):
    if source in DATABASES:
        return [root] if root.exists() else []
    paths = sorted(root.rglob(PATTERNS.get(source, '*.jsonl')))
    return [p for p in paths if source != 'cursor' or 'agent-transcripts' in p.parts]


def dependencies(source, path):
    result = [path]
    if source in DATABASES:
        result.append(Path(str(path) + '-wal'))
    if source.startswith('kimi'):
        result.extend(parent / 'state.json' for parent in list(path.parents)[:3])
    return sorted(set(result))


def workspace_dependencies(root):
    base = root.parent.parent / 'workspaceStorage'
    result = []
    for workspace in sorted(base.glob('*/workspace.json')):
        result.extend([workspace, workspace.with_name('state.vscdb'), workspace.with_name('state.vscdb-wal')])
    return result


def file_state(paths):
    rows = []
    for path in sorted(set(paths)):
        try:
            stat = path.stat()
            rows.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
        except FileNotFoundError:
            rows.append((str(path), 'missing'))
    return rows


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def parser_version():
    return digest([(p.name, hashlib.sha256(p.read_bytes()).hexdigest())
                   for p in sorted(Path(__file__).parent.glob('*.py'))])


def timezone_identity(name):
    if name:
        return name
    return [os.environ.get('TZ'), file_state([Path('/etc/localtime'), Path('/etc/timezone')])]
