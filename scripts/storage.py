"""Shared storage policy: private UTF-8 files, recoverable metadata and atomic writes."""
import json
import os
from pathlib import Path
import sys
import tempfile


def default_store():
    legacy = Path.home() / '.local/share/daily-recap'
    return legacy if legacy.is_dir() else Path.home() / '.local/share/daybyday'


def metadata(path):
    """Invalid metadata is a cache miss; preserve the damaged file for diagnosis."""
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError('expected JSON object')
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        print(f'元数据不可用，按无缓存处理：{path} ({type(exc).__name__})', file=sys.stderr)
        return {}


def atomic_text(path, text):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # Persist the rename as well as the content on local POSIX filesystems.
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + '\n')
