import { useCallback } from "preact/hooks";
import { activeTab, isMuted, themePref } from "../state.js";

// ── Theme helpers ──
function applyTheme(pref) {
  const root = document.documentElement;
  root.classList.remove("light", "dark");
  if (pref === "light") root.classList.add("light");
  else if (pref === "dark") root.classList.add("dark");
}

function themeIcon(pref) {
  if (pref === "light")
    return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="8" cy="8" r="3" /><line x1="8" y1="1" x2="8" y2="3" /><line x1="8" y1="13" x2="8" y2="15" /><line x1="1" y1="8" x2="3" y2="8" /><line x1="13" y1="8" x2="15" y2="8" /><line x1="3.05" y1="3.05" x2="4.46" y2="4.46" /><line x1="11.54" y1="11.54" x2="12.95" y2="12.95" /><line x1="3.05" y1="12.95" x2="4.46" y2="11.54" /><line x1="11.54" y1="4.46" x2="12.95" y2="3.05" /></svg>;
  if (pref === "dark")
    return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M14 8.5A6 6 0 0 1 7.5 2 6 6 0 1 0 14 8.5z" /></svg>;
  return <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="12" height="9" rx="1" /><line x1="5" y1="14" x2="11" y2="14" /><line x1="8" y1="12" x2="8" y2="14" /></svg>;
}

function themeTitle(pref) {
  if (pref === "light") return "Theme: Light (click to switch)";
  if (pref === "dark") return "Theme: Dark (click to switch)";
  return "Theme: System (click to switch)";
}

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
  const theme = themePref.value;

  const switchTab = useCallback((name) => {
    activeTab.value = name;
    window.location.hash = name;
  }, []);

  const cycleTheme = useCallback(() => {
    const cur = themePref.value;
    let next;
    if (cur === null || cur === undefined) next = "light";
    else if (cur === "light") next = "dark";
    else next = null;
    if (next) localStorage.setItem("delegate-theme", next);
    else localStorage.removeItem("delegate-theme");
    themePref.value = next;
    applyTheme(next);
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
          <button
            class="theme-toggle"
            onClick={cycleTheme}
            title={themeTitle(theme)}
          >
            {themeIcon(theme)}
          </button>
        </div>
      </div>
    </div>
  );
}
