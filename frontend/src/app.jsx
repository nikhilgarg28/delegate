import { render } from "preact";
import { useEffect } from "preact/hooks";
import { batch, useSignalEffect } from "@preact/signals";
import {
  currentTeam, teams, bossName, hcHome, tasks, agents, agentStatsMap, messages,
  activeTab, knownAgentNames,
  panelStack, popPanel, closeAllPanels,
  agentLastActivity, agentActivityLog, managerTurnContext,
  helpOverlayOpen, sidebarCollapsed,
} from "./state.js";
import * as api from "./api.js";
import { Sidebar } from "./components/Sidebar.jsx";
import { ChatPanel } from "./components/ChatPanel.jsx";
import { TasksPanel } from "./components/TasksPanel.jsx";
import { AgentsPanel } from "./components/AgentsPanel.jsx";
import { TaskSidePanel } from "./components/TaskSidePanel.jsx";
import { DiffPanel } from "./components/DiffPanel.jsx";
import { ToastContainer } from "./components/Toast.jsx";
import { HelpOverlay } from "./components/HelpOverlay.jsx";
import { showToast } from "./toast.js";

// ── Per-team backing stores (plain objects, not signals) ──
// SSE events for ALL teams are buffered here.  Only the current-team
// slice is pushed into the reactive signals so non-active teams don't
// trigger re-renders.
const _pt = {
  activity:    {},   // team → { agentName: entry }
  activityLog: {},   // team → [entry, …]
  managerCtx:  {},   // team → ctx | null
  managerName: {},   // team → managerAgentName | null
};
const MAX_LOG_ENTRIES = 500;

/** Sync the reactive signals from the backing store for *team*.
 *  Throttled to at most one sync per animation frame to prevent
 *  SSE event floods from overwhelming the render loop.           */
let _syncRaf = 0;
let _syncTeam = null;

function _syncSignals(team) {
  _syncTeam = team;
  if (_syncRaf) return;               // already scheduled
  _syncRaf = requestAnimationFrame(() => {
    _syncRaf = 0;
    const t = _syncTeam;
    batch(() => {
      agentLastActivity.value  = _pt.activity[t]    ? { ..._pt.activity[t] }    : {};
      agentActivityLog.value   = _pt.activityLog[t] ? [..._pt.activityLog[t]] : [];
      managerTurnContext.value = _pt.managerCtx[t]  ?? null;
    });
  });
}

/** Immediate (non-throttled) sync — used on team switch so the UI
 *  reflects the stored state without a one-frame delay.            */
function _syncSignalsNow(team) {
  if (_syncRaf) { cancelAnimationFrame(_syncRaf); _syncRaf = 0; }
  batch(() => {
    agentLastActivity.value  = _pt.activity[team]    ? { ..._pt.activity[team] }    : {};
    agentActivityLog.value   = _pt.activityLog[team] ? [..._pt.activityLog[team]] : [];
    managerTurnContext.value = _pt.managerCtx[team]  ?? null;
  });
}

// ── Main App ──
function App() {
  // ── Keyboard shortcuts ──
  useEffect(() => {
    const handler = (e) => {
      const isInputFocused = () => {
        const el = document.activeElement;
        if (!el) return false;
        const tag = el.tagName.toLowerCase();
        return tag === "input" || tag === "textarea" || tag === "select" || el.contentEditable === "true";
      };
      const isOverlayOpen = () => panelStack.value.length > 0 || helpOverlayOpen.value;

      if (e.key === "Escape") {
        if (helpOverlayOpen.value) { helpOverlayOpen.value = false; return; }
        if (panelStack.value.length > 0) { popPanel(); return; }
        if (isInputFocused()) { document.activeElement.blur(); return; }
        return;
      }
      if (isInputFocused()) return;
      if (e.key === "/" && !isOverlayOpen()) {
        e.preventDefault();
        const chatInput = document.querySelector(".chat-input-box textarea");
        if (chatInput) chatInput.focus();
        return;
      }
      if (e.key === "s" && !isOverlayOpen()) {
        sidebarCollapsed.value = !sidebarCollapsed.value;
        localStorage.setItem("delegate-sidebar-collapsed", sidebarCollapsed.value ? "true" : "false");
        return;
      }
      if (e.key === "c" && !isOverlayOpen()) { activeTab.value = "chat"; window.history.pushState({}, "", "/chat"); return; }
      if (e.key === "t" && !isOverlayOpen()) { activeTab.value = "tasks"; window.history.pushState({}, "", "/tasks"); return; }
      if (e.key === "a" && !isOverlayOpen()) { activeTab.value = "agents"; window.history.pushState({}, "", "/agents"); return; }
      if (e.key === "?" && !isInputFocused()) { helpOverlayOpen.value = !helpOverlayOpen.value; return; }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // ── Path routing ──
  useEffect(() => {
    const onPath = () => {
      const path = window.location.pathname.replace(/^\//, "");
      const valid = ["chat", "tasks", "agents"];
      if (valid.includes(path)) {
        activeTab.value = path;
      } else {
        activeTab.value = "chat";
        if (path !== "") window.history.replaceState(null, "", "/chat");
      }
    };
    window.addEventListener("popstate", onPath);
    onPath();
    return () => window.removeEventListener("popstate", onPath);
  }, []);

  // ── Bootstrap: fetch config + teams ──
  useEffect(() => {
    (async () => {
      try {
        const cfg = await api.fetchConfig();
        if (cfg.boss_name) bossName.value = cfg.boss_name;
        if (cfg.hc_home) hcHome.value = cfg.hc_home;
      } catch (e) { }
      try {
        const teamList = await api.fetchTeams();
        teams.value = teamList;
        if (teamList.length > 0 && !currentTeam.value) {
          currentTeam.value = teamList[0];
        }
      } catch (e) { }
    })();
  }, []);

  // ── Polling loop (reads currentTeam.value dynamically each cycle) ──
  useEffect(() => {
    let active = true;
    const poll = async () => {
      if (!active) return;
      const t = currentTeam.value;
      if (!t) {
        try {
          const tl = await api.fetchTeams();
          if (tl.length) {
            teams.value = tl;
            if (!currentTeam.value) currentTeam.value = tl[0];
          }
        } catch (e) { }
        return;
      }

      try {
        const [taskData, agentData] = await Promise.all([
          api.fetchTasks(t),
          api.fetchAgents(t),
        ]);

        const statsMap = {};
        await Promise.all(
          agentData.map(async (a) => {
            try {
              const s = await api.fetchAgentStats(t, a.name);
              if (s) statsMap[a.name] = s;
            } catch (e) { }
          })
        );

        let msgData = messages.value;
        if (activeTab.value === "chat") {
          try { msgData = await api.fetchMessages(t, {}); } catch (e) { }
        }

        if (active) {
          // Guard: only apply if the team hasn't changed mid-flight
          if (t === currentTeam.value) {
            batch(() => {
              tasks.value = taskData;
              agents.value = agentData;
              agentStatsMap.value = statsMap;
              knownAgentNames.value = agentData.map(a => a.name);
              if (activeTab.value === "chat") messages.value = msgData;
            });
          }
        }
      } catch (e) {
        showToast("Failed to refresh data", "error");
      }
    };

    poll();
    const interval = setInterval(poll, 2000);
    return () => { active = false; clearInterval(interval); };
  }, []);

  // ── Team switch: clear data + re-fetch ──
  // Uses useSignalEffect (from @preact/signals) which auto-tracks
  // signal reads and re-runs when they change.  This is more reliable
  // than useEffect([team]) which depends on Preact's dep comparison
  // during signal-triggered re-renders.
  useSignalEffect(() => {
    const t = currentTeam.value;           // ← auto-tracked
    if (!t) return;

    // Clear stale data from previous team
    batch(() => {
      tasks.value = [];
      agents.value = [];
      agentStatsMap.value = {};
      messages.value = [];
      _syncSignalsNow(t);
    });

    // Fetch data for new team
    (async () => {
      try {
        const [taskData, agentData, msgData] = await Promise.all([
          api.fetchTasks(t),
          api.fetchAgents(t),
          api.fetchMessages(t, {}),
        ]);
        // Guard: only apply if the team hasn't changed while we were fetching
        if (t !== currentTeam.value) return;
        batch(() => {
          tasks.value = taskData;
          agents.value = agentData;
          messages.value = msgData;
        });
        const mgr = agentData.find(a => a.role === "manager");
        _pt.managerName[t] = mgr?.name ?? null;
      } catch (e) { }
    })();
  });

  // ── SSE: one connection per team ──
  // Also uses useSignalEffect so it auto-tracks teams.value.
  useSignalEffect(() => {
    const list = teams.value;              // ← auto-tracked
    if (!list || !list.length) return;

    const connections = {};

    for (const team of list) {
      // Ensure backing store slots exist
      if (!_pt.activity[team])    _pt.activity[team]    = {};
      if (!_pt.activityLog[team]) _pt.activityLog[team] = [];

      // Fetch manager name for this team (one-time, best-effort)
      if (_pt.managerName[team] === undefined) {
        api.fetchAgents(team).then(agentData => {
          const mgr = agentData.find(a => a.role === "manager");
          _pt.managerName[team] = mgr?.name ?? null;
        }).catch(() => { _pt.managerName[team] = null; });
      }

      const es = new EventSource(`/teams/${team}/activity/stream`);

      es.onmessage = (evt) => {
        try {
          const entry = JSON.parse(evt.data);
          if (entry.type === "connected") return;

          const isCurrent = (team === currentTeam.value);

          // ── turn_started ──
          if (entry.type === "turn_started") {
            const mgrName = _pt.managerName[team];
            if (mgrName && mgrName === entry.agent) {
              _pt.managerCtx[team] = entry;
              if (isCurrent) managerTurnContext.value = entry;
            }
            return;
          }

          // ── turn_ended ──
          if (entry.type === "turn_ended") {
            const ctx = _pt.managerCtx[team];
            if (ctx && ctx.agent === entry.agent) {
              _pt.managerCtx[team] = null;
              if (isCurrent) managerTurnContext.value = null;
            }
            return;
          }

          // ── task_update ──
          if (entry.type === "task_update") {
            if (isCurrent) {
              const tid = entry.task_id;
              const cur = tasks.value;
              const idx = cur.findIndex(t => t.id === tid);
              if (idx !== -1) {
                const updated = { ...cur[idx] };
                if (entry.status !== undefined) updated.status = entry.status;
                if (entry.assignee !== undefined) updated.assignee = entry.assignee;
                const next = [...cur];
                next[idx] = updated;
                tasks.value = next;
              }
            }
            return;
          }

          // ── agent_activity ──
          _pt.activity[team][entry.agent] = entry;

          const log = _pt.activityLog[team];
          if (log.length >= MAX_LOG_ENTRIES) log.splice(0, log.length - MAX_LOG_ENTRIES + 1);
          log.push(entry);

          const mgrName = _pt.managerName[team];
          if (mgrName && entry.agent === mgrName) {
            const ctx = _pt.managerCtx[team];
            if (ctx) {
              _pt.managerCtx[team] = { ...ctx, timestamp: entry.timestamp };
            } else {
              _pt.managerCtx[team] = {
                type: "turn_started",
                agent: entry.agent,
                team: team,
                task_id: entry.task_id ?? null,
                sender: "",
                timestamp: entry.timestamp,
              };
            }
          }

          if (isCurrent) _syncSignals(team);
        } catch (e) { /* ignore malformed events */ }
      };

      es.onerror = () => {};
      connections[team] = es;
    }

    return () => {
      for (const es of Object.values(connections)) es.close();
    };
  });

  return (
    <>
      <Sidebar />
      <div class="main">
        <div class="content">
          <ChatPanel />
          <TasksPanel />
          <AgentsPanel />
        </div>
      </div>
      <TaskSidePanel />
      <DiffPanel />
      <HelpOverlay />
      <ToastContainer />
    </>
  );
}

// ── Mount ──
render(<App />, document.getElementById("app"));
