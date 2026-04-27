"""Deterministic follow-up / cadence classification.

Pure functions — no DB handles, no I/O. Given thread + rollup inputs and the
parsed user-context, classify each (contact, thread) pair as overdue / waiting
/ cold and stale next_steps as stale / not.

Ported from v1 ``scripts/lib/followup-core.mjs`` with the category vocabulary
swapped to match v2's `tag.schema.json` enum
(recruiting / legal / personal / finance / vendor / newsletter / transactional
/ other), and the loose-regex parser kept so user-context.md authors aren't
locked into a strict grammar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

DEFAULT_THRESHOLDS = {
    "default_latency_days": 3,
    "cold_threshold_days": 30,
    "overdue_multiplier": 1.5,
    "stale_pending_step_days": 14,
}


def resolve_thresholds(config_block: dict | None) -> dict:
    out = dict(DEFAULT_THRESHOLDS)
    if not config_block or not isinstance(config_block, dict):
        return out
    for k in DEFAULT_THRESHOLDS:
        v = config_block.get(k)
        if isinstance(v, (int, float)) and v > 0:
            out[k] = v
    return out


def _parse_iso(s) -> datetime | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s if s.tzinfo else s.replace(tzinfo=timezone.utc)
    try:
        # Accept "...Z" suffix.
        txt = str(s).replace("Z", "+00:00")
        dt = datetime.fromisoformat(txt)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def days_between(from_iso, to_iso) -> int | None:
    a = _parse_iso(from_iso)
    b = _parse_iso(to_iso)
    if a is None or b is None:
        return None
    diff = (b - a).total_seconds() // 86400
    return max(0, int(diff))


def classify_urgency(days_stale: int, expected_latency_days: int | None, thresholds: dict) -> str:
    """Pure rule — bumped to keep behavior identical to v1's classifyUrgency."""
    if not isinstance(days_stale, (int, float)):
        return "waiting"
    if days_stale > thresholds["cold_threshold_days"]:
        return "cold"
    eff = expected_latency_days if (
        isinstance(expected_latency_days, (int, float)) and expected_latency_days > 0
    ) else thresholds["default_latency_days"]
    if days_stale > eff * thresholds["overdue_multiplier"]:
        return "overdue"
    return "waiting"


_NUM_UNIT_RE = re.compile(r"(\d+)\s*(day|days|d|hour|hours|h|week|weeks|wk)", re.I)


def parse_latency_string(s: str | None) -> int | None:
    """Forgiving parser. "1d", "within 1 day", "2 days", "1 week" → days."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip().lower()
    if t in {"never", "no reply expected", "n/a"}:
        return None
    if t == "same day" or re.search(r"within\s+(the\s+)?(same\s+)?day", t):
        return 1
    m = _NUM_UNIT_RE.search(t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            return max(1, -(-n // 24))  # ceil(n/24)
        if unit.startswith("w"):
            return n * 7
        return n
    if re.search(r"within\s+a\s+week", t):
        return 7
    if re.search(r"within\s+a\s+day", t):
        return 1
    return None


# v2 categories — match `tag.schema.json` category enum.
_CATEGORY_KEYS = {
    "recruiting", "legal", "personal", "finance",
    "vendor", "newsletter", "transactional", "other", "default",
}


def parse_reply_latencies(user_context_md: str) -> dict:
    """Parse the `## Reply-latency expectations` section.

    Returns a dict with keys from `_CATEGORY_KEYS`. Missing keys absent.
    """
    out: dict[str, int] = {}
    section = _slice_section(user_context_md, "Reply-latency expectations")
    if not section:
        return out
    for line in section.split("\n"):
        m = re.match(r"^\s*[-*]\s*([A-Za-z][A-Za-z \/\-&,+]*?)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        days = parse_latency_string(value)
        if days is None:
            continue
        # Map loose labels to canonical keys.
        canonical = _canonicalize_category(label)
        if canonical:
            out[canonical] = days
    return out


def _canonicalize_category(label: str) -> str | None:
    label = label.lower()
    if "recruit" in label:
        return "recruiting"
    if "legal" in label or "compliance" in label:
        return "legal"
    if "personal" in label or "friend" in label or "family" in label:
        return "personal"
    if "finance" in label or "billing" in label or "invoice" in label:
        return "finance"
    if "vendor" in label or "saas" in label or "provider" in label or "tool" in label:
        return "vendor"
    if "newsletter" in label or "digest" in label:
        return "newsletter"
    if "transactional" in label or "receipt" in label or "automated" in label:
        return "transactional"
    if "default" in label or "catch" in label or "other" in label or "fallback" in label:
        return "default"
    return None


@dataclass
class IgnoreList:
    emails: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)


def parse_ignore_list(user_context_md: str) -> IgnoreList:
    """Pull ignore emails/domains from `## Boundaries` (the
    "Senders I never want surfaced as follow-ups" line) plus any `## Things
    to ignore` section, matching v1 semantics."""
    ignore = IgnoreList()
    for header in ("Things to ignore", "Boundaries"):
        section = _slice_section(user_context_md, header)
        if not section:
            continue
        for raw in section.split("\n"):
            line = raw.strip()
            if not (line.startswith("-") or line.startswith("*")):
                continue
            stripped = re.sub(r"^[-*]\s*", "", line)
            # Explicit "Domain: foo.bar"
            m = re.match(r"^domain\s*:\s*`?([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})`?", stripped, re.I)
            if m:
                ignore.domains.add(m.group(1).lower())
                continue
            # Email — anywhere on the line, with optional backticks.
            m = re.search(r"`?([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})`?", stripped, re.I)
            if m:
                ignore.emails.add(m.group(1).lower())
                continue
            # Bare domain in backticks at the start of the bullet.
            m = re.match(r"^`?([a-z0-9][a-z0-9.\-]*\.[a-z]{2,})`?", stripped, re.I)
            if m and "@" not in m.group(1):
                ignore.domains.add(m.group(1).lower())
    return ignore


def is_ignored(email: str | None, ignore: IgnoreList) -> bool:
    if not email:
        return False
    lower = email.lower()
    if lower in ignore.emails:
        return True
    at = lower.rfind("@")
    if at == -1:
        return False
    return lower[at + 1 :] in ignore.domains


def parse_important_contact_overrides(user_context_md: str) -> dict[str, dict]:
    """`## Important contacts` bullets carry optional inline overrides:

      - alice@acme.com (Alice) — co-founder [priority: 1] [reply-latency: 1d]
    """
    out: dict[str, dict] = {}
    section = _slice_section(user_context_md, "Important contacts")
    if not section:
        return out
    for line in section.split("\n"):
        # Email in backticks OR bare angle brackets OR plain — match all.
        m = re.search(r"`?([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})`?", line, re.I)
        if not m:
            continue
        email = m.group(1).lower()
        entry: dict = {}
        pri = re.search(r"\[priority:\s*([123])\]", line, re.I)
        if pri:
            entry["priority"] = int(pri.group(1))
        lat = re.search(r"\[reply-latency:\s*([^\]]+)\]", line, re.I)
        if lat:
            days = parse_latency_string(lat.group(1)) or parse_latency_string(lat.group(1) + " days")
            if days is not None:
                entry["reply_latency_days"] = days
        if entry:
            out[email] = entry
    return out


def infer_category(tags) -> str | None:
    """Map rollup tags to a canonical reply-latency category. Loose match."""
    if not isinstance(tags, list):
        return None
    norm = {str(t).lower() for t in tags}

    def has(needle: str) -> bool:
        return any(needle in t for t in norm)

    if has("recruit"):
        return "recruiting"
    if has("legal"):
        return "legal"
    if has("personal") or has("friend") or has("family"):
        return "personal"
    if has("finance") or has("billing"):
        return "finance"
    if has("vendor") or has("saas") or has("provider"):
        return "vendor"
    if has("newsletter") or has("digest"):
        return "newsletter"
    if has("transactional") or has("automated"):
        return "transactional"
    return None


def pick_latency_days(
    *,
    contact_email: str | None,
    category: str | None,
    latencies: dict,
    contact_overrides: dict,
    thresholds: dict,
) -> int:
    if contact_email and contact_overrides:
        ov = contact_overrides.get(contact_email.lower())
        if ov and isinstance(ov.get("reply_latency_days"), int):
            return ov["reply_latency_days"]
    if category and isinstance(latencies.get(category), int):
        return latencies[category]
    if isinstance(latencies.get("default"), int):
        return latencies["default"]
    return thresholds["default_latency_days"]


def compare_for_display(entry: dict) -> tuple:
    rank = {"overdue": 0, "waiting": 1, "cold": 2}
    return (rank.get(entry.get("urgency"), 3), -(entry.get("days_stale") or 0))


# ---- internal -------------------------------------------------------------

def _slice_section(md: str | None, header: str) -> str | None:
    if not md:
        return None
    pat = re.compile(rf"^##\s+{re.escape(header)}\s*$", re.I | re.M)
    m = pat.search(md)
    if not m:
        return None
    rest = md[m.end():]
    nxt = re.search(r"^##\s+", rest, re.M)
    return rest if nxt is None else rest[: nxt.start()]
