import { useEffect, useState } from "react";
import Setup from "./tabs/Setup";
import Inbox from "./tabs/Inbox";
import Triage from "./tabs/Triage";
import Tags from "./tabs/Tags";
import Followups from "./tabs/Followups";
import Contacts from "./tabs/Contacts";
import Ask from "./tabs/Ask";
import Drafts from "./tabs/Drafts";
import Sent from "./tabs/Sent";
import Observability from "./tabs/Observability";
import Eval from "./tabs/Eval";
import { getAuthStatus, type AuthStatus } from "./api/mailmind";

const TABS = [
  { id: "setup", label: "Settings", component: Setup },
  { id: "inbox", label: "Inbox", component: Inbox },
  { id: "triage", label: "Triage", component: Triage },
  { id: "tags", label: "Tags", component: Tags },
  { id: "followups", label: "Follow-ups", component: Followups },
  { id: "contacts", label: "Contacts", component: Contacts },
  { id: "ask", label: "Ask", component: Ask },
  { id: "drafts", label: "Drafts", component: Drafts },
  { id: "sent", label: "Sent", component: Sent },
  { id: "observability", label: "Observability", component: Observability },
  { id: "eval", label: "Eval", component: Eval },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [active, setActive] = useState<TabId>("setup");
  const [authStatus, setAuthStatus] = useState<AuthStatus | null>(null);
  const Active = TABS.find((t) => t.id === active)!.component;

  // Poll the auth status so the first-run banner reflects reality and
  // updates the moment OAuth completes (tab change OR background poll).
  useEffect(() => {
    let cancelled = false;
    const tick = () =>
      getAuthStatus()
        .then((s) => {
          if (!cancelled) setAuthStatus(s);
        })
        .catch(() => {
          /* ingester might be down — banner stays hidden */
        });
    tick();
    const interval = setInterval(tick, 5000);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, []);

  // First-run banner conditions:
  //   - We have an authStatus payload (i.e. the ingester is reachable)
  //   - Either no OAuth client OR no Gmail tokens yet
  //   - User isn't already on Settings (no point pointing them where
  //     they already are)
  const showFirstRunBanner =
    authStatus !== null &&
    (!authStatus.configured || !authStatus.has_tokens) &&
    active !== "setup";

  const bannerCopy = !authStatus
    ? ""
    : !authStatus.configured
    ? "Welcome — start by adding your Gmail credentials in Settings."
    : "Almost there — connect Gmail in Settings to start syncing.";

  return (
    <div className="app">
      <header className="app__header">
        <span className="app__title">mailmind</span>
        <span className="app__pill">P5b · eval + labeling + LLM-as-judge</span>
        <nav className="tabs">
          {TABS.map((t) => (
            <button
              key={t.id}
              className={`tab ${t.id === active ? "tab--active" : ""}`}
              onClick={() => setActive(t.id)}
            >
              {t.label}
            </button>
          ))}
        </nav>
      </header>
      {showFirstRunBanner && (
        <div className="app__banner">
          <span>{bannerCopy}</span>
          <button
            className="button button--accent app__banner-cta"
            onClick={() => setActive("setup")}
          >
            Open Settings →
          </button>
        </div>
      )}
      <main className="app__main">
        <Active />
      </main>
    </div>
  );
}
