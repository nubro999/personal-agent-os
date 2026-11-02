import { useEffect, useState } from "react";
import { Board } from "./Board";
import { Chat, ChatProvider } from "./Chat";
import { GraphView } from "./GraphView";

const TABS = [
  { id: "board", label: "보드" },
  { id: "graph", label: "그래프" },
  { id: "chat", label: "채팅" },
] as const;
type TabId = (typeof TABS)[number]["id"];

function tabFromHash(): TabId {
  const hash = window.location.hash.slice(1);
  return TABS.find((item) => item.id === hash)?.id ?? "board";
}

export function App() {
  const [tab, setTab] = useState<TabId>(tabFromHash);
  useEffect(() => {
    const onHashChange = () => setTab(tabFromHash());
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);
  return (
    <ChatProvider>
      <div className="flex h-full flex-col md:flex-row">
        <nav className="flex shrink-0 gap-1 border-b border-line bg-panel p-2 md:w-24 md:flex-col md:border-b-0 md:border-r">
          {TABS.map((item) => (
            <button
              key={item.id}
              onClick={() => {
                window.location.hash = item.id;
              }}
              aria-current={tab === item.id ? "page" : undefined}
              className={`rounded px-2 py-1.5 text-left text-sm ${
                tab === item.id ? "bg-raised text-accent" : "text-muted hover:text-text"
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>
        <main className="min-h-0 min-w-0 flex-1">
          {tab === "board" && <Board />}
          {tab === "graph" && <GraphView />}
          {/* 대화 상태는 Provider에 있어서 탭을 옮겨도 남는다. */}
          {tab === "chat" && <Chat />}
        </main>
      </div>
    </ChatProvider>
  );
}
