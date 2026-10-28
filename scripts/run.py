#!/usr/bin/env python3
"""One command: reuse a completed report or prepare a bounded briefing and fixed template."""
from pathlib import Path
import subprocess
import sys

base = Path(__file__).resolve().parent
subprocess.run([sys.executable, str(base / "reports.py"), "prepare", *sys.argv[1:]], check=True)
