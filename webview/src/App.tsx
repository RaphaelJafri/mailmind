import { useState } from "react";
import Setup from "./tabs/Setup";
import Inbox from "./tabs/Inbox";
import Triage from "./tabs/Triage";
import Tags from "./tabs/Tags";
import Followups from "./tabs/Followups";
import Contacts from "./tabs/Contacts";
import Ask from "./tabs/Ask";
import Drafts from "./tabs/Drafts";
import Sent from "./tabs/Sent";

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
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [active, setActive] = useState<TabId>("setup");
  const Active = TABS.find((t) => t.id === active)!.component;
  return (
    <div className="app">
      <header className="app__header">
        <span className="app__title">mailmind</span>
        <span className="app__pill">P4 · drafts + sends with approval gate</span>
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
      <main className="app__main">
        <Active />
      </main>
    </div>
  );
}
