"""Path resolution for mailmind data, mirroring ingester/src/lib/paths.mjs.

Default lives under macOS Application Support. Override with MAILMIND_DATA_DIR
for tests, demo mode, or non-default install locations.

Env vars are read on every call so tests using `monkeypatch.setenv` work
without re-importing this module.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    if env := os.environ.get("MAILMIND_DATA_DIR"):
        p = Path(env).expanduser().resolve()
    else:
        p = Path.home() / "Library" / "Application Support" / "mailmind"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_dir() -> Path:
    p = data_dir() / "db"
    p.mkdir(parents=True, exist_ok=True)
    return p


def tokens_dir() -> Path:
    p = data_dir() / "tokens"
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    p = data_dir() / "config"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir() -> Path:
    p = data_dir() / "reports"
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
