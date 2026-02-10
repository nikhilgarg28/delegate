import { useCallback } from "preact/hooks";
import { activeTab, isMuted } from "../state.js";

// ── Mute icon ──
function muteIcon(muted) {
  if (muted)
    return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="2,6 2,10 5,10 9,13 9,3 5,6" /><line x1="12" y1="5" x2="15" y2="11" /><line x1="15" y1="5" x2="12" y2="11" /></svg>;
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><polygon points="2,6 2,10 5,10 9,13 9,3 5,6" /><path d="M11.5 5.5a3.5 3.5 0 0 1 0 5" /></svg>;
}

const TABS = ["chat", "tasks", "agents"];

export function Header() {
  const tab = activeTab.value;
  const muted = isMuted.value;

  const switchTab = useCallback((name) => {
    activeTab.value = name;
    window.location.hash = name;
  }, []);

  const toggleMute = useCallback(() => {
    const next = !isMuted.value;
    isMuted.value = next;
    localStorage.setItem("delegate-muted", next ? "true" : "false");
  }, []);

  return (
    <div class="header">
      <div class="header-inner">
        <div class="tabs">
          {TABS.map(t => (
            <button
              key={t}
              class={"tab" + (tab === t ? " active" : "")}
              onClick={() => switchTab(t)}
            >
              {t.charAt(0).toUpperCase() + t.slice(1)}
            </button>
          ))}
        </div>
        <div class="header-actions">
          <button
            class="mute-toggle"
            onClick={toggleMute}
            title={muted ? "Unmute notifications" : "Mute notifications"}
          >
            {muteIcon(muted)}
          </button>
        </div>
      </div>
    </div>
  );
}
