import { useCallback, useEffect, useMemo, useState } from "react";
import {
  approveDraft,
  cancelApproval,
  editDraft,
  executeSaveAsDraft,
  generateDraft,
  getPermissions,
  listDrafts,
  rejectDraft,
  type ApprovalResponse,
  type DraftRow,
  type PermissionsRow,
} from "../api/mailmind";

type Loadable<T> =
  | { state: "loading" }
  | { state: "ok"; data: T }
  | { state: "err"; error: string };

interface Drafting {
  thread_id: string;
  intent: string;
  busy: boolean;
  error: string | null;
}

export default function Drafts() {
  const [drafts, setDrafts] = useState<Loadable<DraftRow[]>>({ state: "loading" });
  const [perms, setPerms] = useState<PermissionsRow | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [drafting, setDrafting] = useState<Drafting>({
    thread_id: "fix-thread-102",
    intent: "Reply to Adam Moore confirming the Thursday 2pm call",
    busy: false,
    error: null,
  });

  const refresh = useCallback(async () => {
    setDrafts({ state: "loading" });
    try {
      const [d, p] = await Promise.all([listDrafts(), getPermissions()]);
      setDrafts({ state: "ok", data: d.drafts });
      setPerms(p);
    } catch (err) {
      setDrafts({ state: "err", error: (err as Error).message });
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  async function onGenerate() {
    if (!drafting.thread_id.trim() || !drafting.intent.trim()) return;
    setDrafting({ ...drafting, busy: true, error: null });
    try {
      const res = await generateDraft(drafting.thread_id.trim(), drafting.intent.trim());
      setDrafting({ ...drafting, busy: false, error: null });
      setSelected(res.draft_id);
      await refresh();
    } catch (err) {
      setDrafting({ ...drafting, busy: false, error: (err as Error).message });
    }
  }

  const list = drafts.state === "ok" ? drafts.data : [];
  const current = useMemo(
    () => list.find((d) => d.id === selected) ?? null,
    [list, selected],
  );

  return (
    <>
      <section className="section">
        <h2 className="section__title">Drafts</h2>
        <p className="hint">
          The draft agent writes; you approve. Drafts live in the local{" "}
          <code>drafts</code> table — Gmail isn't touched until you click
          Save as Gmail Draft, and even then only after a 10s undo window. Send
          is gated behind <code>gmail.send</code> scope (P4b — toggle in
          Settings → Permissions).
        </p>
        <PermissionsBanner perms={perms} onGranted={refresh} />
      </section>

      <section className="section">
        <h3 className="section__title">Generate a draft</h3>
        <p className="hint">
          Pick a thread and tell the agent what to write. The fixture demo:{" "}
          <code>fix-thread-102</code> + "Reply to Adam Moore confirming the
          Thursday 2pm call".
        </p>
        <div className="drafts__generate">
          <label className="drafts__field">
            <span>thread_id</span>
            <input
              className="drafts__input"
              value={drafting.thread_id}
              onChange={(e) => setDrafting({ ...drafting, thread_id: e.target.value })}
              disabled={drafting.busy}
            />
          </label>
          <label className="drafts__field">
            <span>intent</span>
            <input
              className="drafts__input"
              value={drafting.intent}
              onChange={(e) => setDrafting({ ...drafting, intent: e.target.value })}
              disabled={drafting.busy}
            />
          </label>
          <button
            className="button"
            onClick={onGenerate}
            disabled={drafting.busy || !drafting.thread_id.trim() || !drafting.intent.trim()}
          >
            {drafting.busy ? "generating…" : "Generate"}
          </button>
        </div>
        {drafting.error && <div className="error">{drafting.error}</div>}
      </section>

      <section className="section drafts__split">
        <div className="drafts__list">
          <h3 className="section__title">Pending</h3>
          {drafts.state === "loading" && <p className="hint">loading…</p>}
          {drafts.state === "err" && <div className="error">{drafts.error}</div>}
          {drafts.state === "ok" && list.length === 0 && (
            <p className="hint">No drafts yet — generate one above.</p>
          )}
          {drafts.state === "ok" &&
            list.map((d) => (
              <button
                key={d.id}
                className={`drafts__list-item ${d.id === selected ? "drafts__list-item--active" : ""}`}
                onClick={() => setSelected(d.id)}
              >
                <div className="drafts__list-subject">{d.subject}</div>
                <div className="drafts__list-meta">
                  <span className={`pill pill--${d.status}`}>{d.status}</span>{" "}
                  <span className="cell--muted">{d.to_emails.join(", ")}</span>
                </div>
              </button>
            ))}
        </div>

        <div className="drafts__detail">
          {current === null && <p className="hint">Select a draft to review.</p>}
          {current !== null && (
            <DraftDetail
              draft={current}
              perms={perms}
              onChanged={refresh}
              key={current.id /* fully reset state when switching drafts */}
            />
          )}
        </div>
      </section>
    </>
  );
}

function PermissionsBanner({
  perms,
  onGranted,
}: {
  perms: PermissionsRow | null;
  onGranted: () => void;
}) {
  if (perms === null) return null;
  if (perms["gmail.compose"]) return null;
  return (
    <div className="drafts__banner">
      <strong>gmail.compose not granted yet.</strong> The Save as Gmail Draft
      button will refuse until you grant the scope in Settings → Permissions.{" "}
      You can still generate, edit, and reject drafts here.{" "}
      <button
        className="button button--ghost"
        onClick={async () => {
          await fetch("http://127.0.0.1:8765/permissions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ gmail_compose: true }),
          });
          onGranted();
        }}
      >
        Grant gmail.compose
      </button>
    </div>
  );
}

interface DraftDetailProps {
  draft: DraftRow;
  perms: PermissionsRow | null;
  onChanged: () => void;
}

function DraftDetail({ draft, perms, onChanged }: DraftDetailProps) {
  const [subject, setSubject] = useState(draft.subject);
  const [body, setBody] = useState(draft.body);
  const [to, setTo] = useState(draft.to_emails.join(", "));
  const [savingEdit, setSavingEdit] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const [pending, setPending] = useState<{
    approval: ApprovalResponse;
    countdownEnds: number;
    cancelled: boolean;
  } | null>(null);
  const [outcome, setOutcome] = useState<{ kind: "saved" | "rejected" | "error"; text: string } | null>(null);

  const isPending = draft.status === "pending";
  const isApproved = draft.status === "approved";
  const composeGranted = perms?.["gmail.compose"] === true;
  const sendGranted = perms?.["gmail.send"] === true;
  const dirty =
    subject !== draft.subject ||
    body !== draft.body ||
    to.replace(/\s+/g, "") !== draft.to_emails.join(",").replace(/\s+/g, "");

  async function onSaveEdit() {
    setSavingEdit(true);
    setEditError(null);
    try {
      await editDraft(draft.id, {
        subject,
        body,
        to_emails: to.split(",").map((s) => s.trim()).filter(Boolean),
      });
      onChanged();
    } catch (err) {
      setEditError((err as Error).message);
    } finally {
      setSavingEdit(false);
    }
  }

  async function onApproveSave() {
    setOutcome(null);
    try {
      const ap = await approveDraft(draft.id, { action: "save_as_draft" });
      setPending({
        approval: ap,
        countdownEnds: Date.now() + ap.undo_window_seconds * 1000,
        cancelled: false,
      });
    } catch (err) {
      setOutcome({ kind: "error", text: (err as Error).message });
    }
  }

  async function onCancelUndo() {
    if (!pending) return;
    setPending({ ...pending, cancelled: true });
    try {
      await cancelApproval(draft.id, pending.approval.approval_id, "user clicked cancel");
    } finally {
      setPending(null);
      onChanged();
    }
  }

  async function onReject() {
    setOutcome(null);
    try {
      await rejectDraft(draft.id);
      setOutcome({ kind: "rejected", text: "Draft rejected." });
      onChanged();
    } catch (err) {
      setOutcome({ kind: "error", text: (err as Error).message });
    }
  }

  // Drive the undo countdown. When it hits zero, fire the actual save and
  // surface success / error.
  useEffect(() => {
    if (!pending || pending.cancelled) return;
    const remain = pending.countdownEnds - Date.now();
    if (remain <= 0) {
      let cancelled = false;
      executeSaveAsDraft(draft.id, pending.approval.approval_id)
        .then((res) => {
          if (cancelled) return;
          setPending(null);
          setOutcome({
            kind: "saved",
            text: `Saved as Gmail draft ${res.gmail_draft_id ?? "(no id)"}.`,
          });
          onChanged();
        })
        .catch((err) => {
          if (cancelled) return;
          setPending(null);
          setOutcome({ kind: "error", text: (err as Error).message });
          onChanged();
        });
      return () => {
        cancelled = true;
      };
    }
    const t = setTimeout(() => {
      // force re-render so the countdown updates
      setPending({ ...pending });
    }, 250);
    return () => clearTimeout(t);
  }, [pending, draft.id, onChanged]);

  const remainingSec =
    pending && !pending.cancelled
      ? Math.max(0, Math.ceil((pending.countdownEnds - Date.now()) / 1000))
      : 0;

  return (
    <div>
      <div className="drafts__header">
        <span className={`pill pill--${draft.status}`}>{draft.status}</span>{" "}
        <span className={`pill pill--${draft.confidence}`}>{draft.confidence} confidence</span>{" "}
        <span className="cell--muted drafts__hash">draft_hash {draft.draft_hash.slice(0, 12)}…</span>
      </div>

      <label className="drafts__field">
        <span>To</span>
        <input
          className="drafts__input"
          value={to}
          onChange={(e) => setTo(e.target.value)}
          disabled={!isPending}
        />
      </label>
      <label className="drafts__field">
        <span>Subject</span>
        <input
          className="drafts__input"
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          disabled={!isPending}
        />
      </label>
      <label className="drafts__field">
        <span>Body</span>
        <textarea
          className="drafts__textarea"
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={10}
          disabled={!isPending}
        />
      </label>

      {dirty && isPending && (
        <div className="actions">
          <button className="button" onClick={onSaveEdit} disabled={savingEdit}>
            {savingEdit ? "saving…" : "Save edits"}
          </button>
          <button
            className="button button--ghost"
            onClick={() => {
              setSubject(draft.subject);
              setBody(draft.body);
              setTo(draft.to_emails.join(", "));
            }}
          >
            Discard edits
          </button>
        </div>
      )}
      {editError && <div className="error">{editError}</div>}

      <div className="drafts__rationale">
        <strong>Rationale:</strong> {draft.rationale}
      </div>

      <div className="drafts__cited">
        <strong>Cited facts ({draft.cited_facts.length})</strong>
        <ul>
          {draft.cited_facts.map((f, i) => (
            <li key={i}>
              <code>{f.fact_id}</code> — sources:{" "}
              {f.source_message_ids.map((m) => (
                <code key={m} className="drafts__msgid">
                  {m}
                </code>
              ))}
            </li>
          ))}
        </ul>
      </div>

      {pending && !pending.cancelled && (
        <div className="drafts__undo">
          <strong>Saving in {remainingSec}s</strong> — Click Cancel to abort.
          <button className="button button--warn" onClick={onCancelUndo}>
            Cancel
          </button>
        </div>
      )}

      {pending === null && isPending && (
        <div className="actions drafts__cta">
          <button
            className="button button--primary"
            onClick={onApproveSave}
            disabled={!composeGranted || dirty}
            title={
              dirty
                ? "Save your edits first."
                : composeGranted
                ? "Save this draft to Gmail Drafts (10s undo window)."
                : "Grant gmail.compose first."
            }
          >
            Save as Gmail Draft
          </button>
          <button
            className="button"
            disabled
            title={
              sendGranted
                ? "Send is wired in P4b only."
                : "gmail.send scope not granted (P4b)."
            }
          >
            Approve & Send (P4b)
          </button>
          <button className="button button--warn" onClick={onReject}>
            Reject
          </button>
        </div>
      )}

      {pending === null && isApproved && (
        <div className="hint">
          Approval in flight. Reload to see status — or wait for the audit row
          to land in the Sent tab.
        </div>
      )}

      {outcome && (
        <div className={outcome.kind === "error" ? "error" : "drafts__outcome"}>
          {outcome.text}
        </div>
      )}
    </div>
  );
}
