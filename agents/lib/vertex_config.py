"""Vertex AI / Gemini configuration.

Auth detection priority (per BUILD.md §3.3):
1. GOOGLE_GENAI_API_KEY  → direct Gemini API (no GCP)
2. GOOGLE_APPLICATION_CREDENTIALS → service-account JSON
3. ADC fallback → ~/.config/gcloud/application_default_credentials.json

Region: us-central1 (lowest latency, broadest model availability).
Model IDs: pinned constants. Update here when migrating model versions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

REGION = "us-central1"
GEMINI_FLASH = "gemini-2.5-flash"
GEMINI_PRO = "gemini-2.5-pro"


@dataclass(frozen=True)
class AuthMode:
    kind: str  # "api_key" | "service_account" | "adc"
    detail: str  # human-readable detail


def detect_auth_mode() -> AuthMode:
    if os.environ.get("GOOGLE_GENAI_API_KEY"):
        return AuthMode(
            kind="api_key", detail="GOOGLE_GENAI_API_KEY (direct Gemini API)"
        )
    if path := os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
        return AuthMode(
            kind="service_account",
            detail=f"GOOGLE_APPLICATION_CREDENTIALS={path}",
        )
    return AuthMode(kind="adc", detail="application_default_credentials")


def project_id() -> str | None:
    """Resolve the active GCP project for Vertex AI calls."""
    return (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
        or _read_adc_project()
    )


def _read_adc_project() -> str | None:
    import json
    from pathlib import Path

    p = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except Exception:
        return None
    return data.get("quota_project_id") or data.get("project_id")
