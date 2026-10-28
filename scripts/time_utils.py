"""Resolve a real local zone, preserving DST rules and explicit TZ overrides."""
from datetime import datetime
import os
from pathlib import Path
from zoneinfo import ZoneInfo


def local_zone(name=None):
    if name:
        return ZoneInfo(name)
    if os.environ.get('TZ'):
        value = os.environ['TZ'].lstrip(':')
        try:
            return ZoneInfo(value)
        except (ValueError, KeyError):
            if Path(value).is_file():
                with Path(value).open('rb') as handle:
                    return ZoneInfo.from_file(handle)
            raise ValueError('TZ 必须为 IANA 时区或 zoneinfo 文件路径；也可显式传 --timezone')
    try:
        with Path('/etc/localtime').open('rb') as handle:
            return ZoneInfo.from_file(handle, key='local')
    except OSError:
        return datetime.now().astimezone().tzinfo
