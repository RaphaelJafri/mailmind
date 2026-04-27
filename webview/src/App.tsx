import { useState } from "react";
import Setup from "./tabs/Setup";
import Inbox from "./tabs/Inbox";
import Triage from "./tabs/Triage";
import Tags from "./tabs/Tags";
import Contacts from "./tabs/Contacts";

const TABS = [
  { id: "setup", label: "Setup", component: Setup },
  { id: "inbox", label: "Inbox", component: Inbox },
  { id: "triage", label: "Triage", component: Triage },
  { id: "tags", label: "Tags", component: Tags },
  { id: "contacts", label: "Contacts", component: Contacts },
] as const;

type TabId = (typeof TABS)[number]["id"];

export default function App() {
  const [active, setActive] = useState<TabId>("setup");
  const Active = TABS.find((t) => t.id === active)!.component;
  return (
    <div className="app">
      <header className="app__header">
        <span className="app__title">mailmind</span>
        <span className="app__pill">P1 · agents</span>
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
