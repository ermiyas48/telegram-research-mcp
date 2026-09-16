#!/usr/bin/env python3
"""Assemble Telegram Research API."""
from pathlib import Path
_code = Path(__file__).with_name("_app_a.py").read_text() + Path(__file__).with_name("_app_b.py").read_text()
exec(compile(_code, "app_assembled.py", "exec"), globals())
