"""Held-out labeled set — append-only JSONL of ground-truth labels.

Per BUILD §23 + EXECUTION P5 item 1. The labels live in
`{data_dir}/evals/labeled.jsonl` (one JSON object per line) so they
survive DB resets and stay diff-friendly.

Three label kinds:

- ``thread`` — extraction ground truth. Compared to extract_agent's
  ThreadFacts output. Scored on commitment precision/recall +
  next_step_priority F1.
- ``rollup`` — relationship ground truth. Compared to relationship_agent's
  ContactRollup output. Scored on tone/cadence/status exact-match +
  tags Jaccard.
- ``draft`` — draft style/correctness ground truth. Compared to
  draft_agent's output via LLM-as-judge (evals/judge_prompts/v1.md).

Labels are append-only — to "edit" a label, delete it (which writes a
soft-delete row) and add a new one. Versioning lives at the row level so
a baseline run pinned to v1 of a label still scores against that
specific snapshot.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal

import ulid

from . import paths


LabelKind = Literal["thread", "rollup", "draft"]


def labels_path() -> Path:
    """`{data_dir}/evals/labeled.jsonl`. Created on first write.

    Tests + acceptance can override the entire data dir via
    MAILMIND_DATA_DIR; no separate env var here on purpose."""
    p = paths.data_dir() / "evals" / "labeled.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---- record shape --------------------------------------------------------


@dataclass
class LabelRow:
    """One labeled.jsonl line.

    `expected` is the ground-truth payload — shape depends on `kind`.
    For ``thread``: a dict matching ThreadFacts essentials (summary,
        commitments_by_user/others, next_step_priority).
    For ``rollup``: tone/cadence/status/tags + summary.
    For ``draft``: tone, must_mention, factually_correct expectation,
        optional gold-body for the LLM judge.
    """

    id: str
    kind: LabelKind
    target_id: str  # thread_id | contact_email | f"{thread_id}::{intent}"
    expected: dict
    labeled_at: str
    labeled_by: str
    version: int = 1
    notes: str | None = None
    deleted: bool = False
    extra: dict = field(default_factory=dict)

    def to_json(self) -> str:
        d = {
            "id": self.id,
            "kind": self.kind,
            "target_id": self.target_id,
            "expected": self.expected,
            "labeled_at": self.labeled_at,
            "labeled_by": self.labeled_by,
            "version": self.version,
        }
        if self.notes:
            d["notes"] = self.notes
        if self.deleted:
            d["deleted"] = True
        if self.extra:
            d["extra"] = self.extra
        return json.dumps(d, default=str)

    @classmethod
    def from_dict(cls, d: dict) -> LabelRow:
        return cls(
            id=d["id"],
            kind=d["kind"],
            target_id=d["target_id"],
            expected=d.get("expected") or {},
            labeled_at=d.get("labeled_at") or "",
            labeled_by=d.get("labeled_by") or "",
            version=int(d.get("version") or 1),
            notes=d.get("notes"),
            deleted=bool(d.get("deleted", False)),
            extra=d.get("extra") or {},
        )


# ---- public API ----------------------------------------------------------


def add_label(
    *,
    kind: LabelKind,
    target_id: str,
    expected: dict,
    labeled_by: str,
    notes: str | None = None,
    extra: dict | None = None,
) -> LabelRow:
    """Append a new label. Returns the LabelRow.

    `target_id` is the join key into the agent's output:
      - kind=thread   → thread_id
      - kind=rollup   → contact_email
      - kind=draft    → "{thread_id}::{intent}" (so the eval can
                        re-generate the same draft deterministically)

    Multiple labels for the same (kind, target_id) are allowed — eval
    uses the most recent non-deleted row."""
    if kind not in ("thread", "rollup", "draft"):
        raise ValueError(f"unknown label kind: {kind!r}")
    if not target_id:
        raise ValueError("target_id is required")
    if not expected:
        raise ValueError("expected payload is required")

    row = LabelRow(
        id=f"lbl_{ulid.new()}",
        kind=kind,
        target_id=target_id,
        expected=expected,
        labeled_at=datetime.now(timezone.utc).isoformat(),
        labeled_by=labeled_by or "owner@mailmind.local",
        notes=notes,
        extra=extra or {},
    )
    _append(row)
    return row


def soft_delete(label_id: str, *, by: str) -> bool:
    """Append a tombstone row pointing at `label_id`. Returns True if a
    matching live row existed; False if the id is unknown or already
    soft-deleted.

    Append-only means we never rewrite history — a delete is just a new
    row with the same id and `deleted=true`. `list_labels` filters them
    out. We pick the *latest* row for the id (in file order); if it's
    already a tombstone, this call is a no-op."""
    latest: LabelRow | None = None
    for row in _iter_rows():
        if row.id == label_id:
            latest = row
    if latest is None or latest.deleted:
        return False
    target = latest
    tomb = LabelRow(
        id=label_id,
        kind=target.kind,
        target_id=target.target_id,
        expected=target.expected,
        labeled_at=datetime.now(timezone.utc).isoformat(),
        labeled_by=by or "owner@mailmind.local",
        version=target.version + 1,
        deleted=True,
        notes="soft delete",
    )
    _append(tomb)
    return True


def list_labels(*, kind: LabelKind | None = None) -> list[dict]:
    """Return live labels (deleted ones filtered).

    Resolves the latest non-deleted row per `id`. Sorted by labeled_at
    DESC so the UI renders newest first.
    """
    by_id: dict[str, LabelRow] = {}
    for row in _iter_rows():
        # Order matters — the file is append-only, last write wins.
        by_id[row.id] = row

    out = [r for r in by_id.values() if not r.deleted]
    if kind is not None:
        out = [r for r in out if r.kind == kind]
    out.sort(key=lambda r: r.labeled_at, reverse=True)
    return [json.loads(r.to_json()) for r in out]


def get_label(label_id: str) -> dict | None:
    """Return the latest non-deleted LabelRow with this id, or None."""
    target: LabelRow | None = None
    for row in _iter_rows():
        if row.id == label_id:
            target = row
    if target is None or target.deleted:
        return None
    return json.loads(target.to_json())


def latest_per_target(kind: LabelKind | None = None) -> list[LabelRow]:
    """The eval-time view: at most one live label per (kind, target_id),
    using the most-recent labeled_at."""
    rows = [LabelRow.from_dict(d) for d in list_labels(kind=kind)]
    by_target: dict[tuple[str, str], LabelRow] = {}
    for r in rows:
        key = (r.kind, r.target_id)
        if key not in by_target or r.labeled_at > by_target[key].labeled_at:
            by_target[key] = r
    return list(by_target.values())


def counts() -> dict:
    """Quick "how full is the set?" tally for the UI badge."""
    rows = list_labels()
    out = {"thread": 0, "rollup": 0, "draft": 0, "total": 0}
    for r in rows:
        out[r["kind"]] += 1
        out["total"] += 1
    return out


# ---- internals -----------------------------------------------------------


def _append(row: LabelRow) -> None:
    """Atomically append one JSONL line. Uses an exclusive open + flush so
    two services writing concurrently don't interleave."""
    p = labels_path()
    line = row.to_json() + "\n"
    # macOS's stdlib append-mode fwrite is atomic for <PIPE_BUF bytes; our
    # lines are short. fsync to defeat the 30s default WAL flush.
    fd = os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _iter_rows() -> Iterator[LabelRow]:
    p = labels_path()
    if not p.exists():
        return
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield LabelRow.from_dict(json.loads(line))
            except Exception:  # noqa: BLE001 — bad rows shouldn't break the iterator
                continue
