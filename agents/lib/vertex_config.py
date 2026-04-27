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
    """Resolve the active GCP project for Vertex AI calls.

    Priority order:
    1. ADC `quota_project_id` (set explicitly by the user with
       `gcloud auth application-default set-quota-project ...`) — most reliable.
    2. GOOGLE_CLOUD_PROJECT env var — accepted, but loses to ADC if both set.
    3. GCLOUD_PROJECT env var — legacy fallback.

    ADC wins because env vars get set by adjacent projects (a workspace-wide
    .env, an exported shell var) and silently misroute calls.
    """
    return (
        _read_adc_project()
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("GCLOUD_PROJECT")
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
