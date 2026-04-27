"""P0 smoke tests for agents service. Skips real Gemini calls via env flag."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MAILMIND_DATA_DIR to a temp dir for every test."""
    tmp = Path(tempfile.mkdtemp(prefix="mailmind-test-"))
    monkeypatch.setenv("MAILMIND_DATA_DIR", str(tmp))
    return tmp


def test_paths_module_resolves_under_override(isolated_data_dir: Path) -> None:
    # Re-import to pick up the env var.
    from importlib import reload

    from lib import paths as paths_mod

    reload(paths_mod)
    # Resolve both sides — macOS symlinks /var → /private/var.
    assert paths_mod.data_dir().resolve() == isolated_data_dir.resolve()
    assert paths_mod.db_dir().is_dir()
    assert paths_mod.tokens_dir().is_dir()


def test_vertex_config_detects_auth_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from lib import vertex_config

    monkeypatch.delenv("GOOGLE_GENAI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    auth = vertex_config.detect_auth_mode()
    assert auth.kind == "adc"

    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake")
    auth = vertex_config.detect_auth_mode()
    assert auth.kind == "api_key"


def test_health_endpoint_returns_ok_with_skipped_gemini(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    monkeypatch.setenv("MAILMIND_HEALTH_SKIP_GEMINI", "1")

    # Re-import service so the env var is read by paths in case of caching.
    from importlib import reload

    import service

    reload(service)
    client = TestClient(service.app)

    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["sidecar"] == "mailmind-agents"
    assert body["gemini"]["status"] == "skipped"


def test_gemini_runner_uses_stub_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubbed Gemini call should return the canned response without network."""
    from importlib import reload

    from lib import gemini_runner, vertex_config

    monkeypatch.setenv("GOOGLE_GENAI_API_KEY", "fake-key-for-stub")
    reload(gemini_runner)

    prompt = "Reply with exactly: PONG"
    model = vertex_config.GEMINI_FLASH
    import hashlib

    h = hashlib.sha256(f"{model}::{prompt}".encode()).hexdigest()

    stub = {
        h: {
            "output": "PONG",
            "input_tokens": 5,
            "output_tokens": 1,
            "latency_ms": 1,
        }
    }
    stub_path = Path(tempfile.mkdtemp()) / "stub.json"
    stub_path.write_text(json.dumps(stub))
    monkeypatch.setenv("MAILMIND_STUB_RESPONSES", str(stub_path))

    result = gemini_runner.health_check()
    assert result.text == "PONG"
    assert result.stubbed is True
    assert result.input_tokens == 5
