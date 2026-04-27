"""Path resolution for mailmind data, mirroring ingester/src/lib/paths.mjs.

Default lives under macOS Application Support. Override with MAILMIND_DATA_DIR
for tests, demo mode, or non-default install locations.
"""

from __future__ import annotations

import os
from pathlib import Path


def _default_data_dir() -> Path:
    if env := os.environ.get("MAILMIND_DATA_DIR"):
        return Path(env).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "mailmind"


DATA_DIR = _default_data_dir()


def data_dir() -> Path:
    return DATA_DIR


def db_dir() -> Path:
    p = DATA_DIR / "db"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tokens_dir() -> Path:
    p = DATA_DIR / "tokens"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    p = DATA_DIR / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir() -> Path:
    p = DATA_DIR / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def logs_dir() -> Path:
    if env := os.environ.get("MAILMIND_LOGS_DIR"):
        p = Path(env).expanduser().resolve()
    else:
        p = Path.home() / "Library" / "Logs" / "mailmind"
    p.mkdir(parents=True, exist_ok=True)
    return p


def raw_db_path() -> Path:
    return db_dir() / "raw.sqlite"


def derived_db_path() -> Path:
    return db_dir() / "derived.sqlite"


def agent_runs_db_path() -> Path:
    return db_dir() / "agent_runs.sqlite"
