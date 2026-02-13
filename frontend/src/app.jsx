import { render } from "preact";
import { useEffect, useCallback } from "preact/hooks";
import { batch } from "@preact/signals";
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

/** Sync the reactive signals from the backing store for *team*. */
function _syncSignals(team) {
  batch(() => {
    agentLastActivity.value  = _pt.activity[team]    ? { ..._pt.activity[team] }    : {};
    agentActivityLog.value   = _pt.activityLog[team] ? [..._pt.activityLog[team]] : [];
    managerTurnContext.value = _pt.managerCtx[team]  ?? null;
  });
}

// ── Main App ──
function App() {
  const tab = activeTab.value;

  // Keyboard handler
  useEffect(() => {
    const handler = (e) => {
      // Helper to check if we're in an input
      const isInputFocused = () => {
        const el = document.activeElement;
        if (!el) return false;
        const tag = el.tagName.toLowerCase();
        return tag === "input" || tag === "textarea" || tag === "select" || el.contentEditable === "true";
      };

      // Helper to check if any overlay is open
      const isOverlayOpen = () => {
        return panelStack.value.length > 0 || helpOverlayOpen.value;
      };

      // Escape: pop one panel level (or close last), close overlays, or blur input
      if (e.key === "Escape") {
        if (helpOverlayOpen.value) { helpOverlayOpen.value = false; return; }
        if (panelStack.value.length > 0) { popPanel(); return; }
        if (isInputFocused()) { document.activeElement.blur(); return; }
        return;
      }

      // All other shortcuts require no input focus
      if (isInputFocused()) return;

      // / (slash): focus chat input
      if (e.key === "/" && !isOverlayOpen()) {
        e.preventDefault();
        const chatInput = document.querySelector(".chat-input-box textarea");
        if (chatInput) chatInput.focus();
        return;
      }

      // s: toggle sidebar
      if (e.key === "s" && !isOverlayOpen()) {
        sidebarCollapsed.value = !sidebarCollapsed.value;
        localStorage.setItem("delegate-sidebar-collapsed", sidebarCollapsed.value ? "true" : "false");
        return;
      }

      // c: go to chat
      if (e.key === "c" && !isOverlayOpen()) {
        activeTab.value = "chat";
        window.history.pushState({}, "", "/chat");
        return;
      }

      // t: go to tasks
      if (e.key === "t" && !isOverlayOpen()) {
        activeTab.value = "tasks";
        window.history.pushState({}, "", "/tasks");
        return;
      }

      // a: go to agents
      if (e.key === "a" && !isOverlayOpen()) {
        activeTab.value = "agents";
        window.history.pushState({}, "", "/agents");
        return;
      }

      // ?: toggle help overlay
      if (e.key === "?" && !isInputFocused()) {
        helpOverlayOpen.value = !helpOverlayOpen.value;
        return;
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // Path routing
  useEffect(() => {
    const onPath = () => {
      const path = window.location.pathname.replace(/^\//, "");
      const valid = ["chat", "tasks", "agents"];
      if (valid.includes(path)) {
        activeTab.value = path;
      } else {
        activeTab.value = "chat";
        if (path !== "") {
          window.history.replaceState(null, "", "/chat");
        }
      }
    };
    window.addEventListener("popstate", onPath);
    onPath();
    return () => window.removeEventListener("popstate", onPath);
  }, []);

  // Initial bootstrap: fetch config + teams
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

  // Polling loop — fetches data every 2s and updates signals
  useEffect(() => {
    let active = true;
    const poll = async () => {
      if (!active) return;
      const team = currentTeam.value;
      if (!team) {
        try {
          const teamList = await api.fetchTeams();
          if (teamList.length) {
            teams.value = teamList;
            if (!currentTeam.value) currentTeam.value = teamList[0];
          }
        } catch (e) { }
        return;
      }

      try {
        const [taskData, agentData] = await Promise.all([
          api.fetchTasks(team),
          api.fetchAgents(team),
        ]);

        const statsMap = {};
        await Promise.all(
          agentData.map(async (a) => {
            try {
              const s = await api.fetchAgentStats(team, a.name);
              if (s) statsMap[a.name] = s;
            } catch (e) { }
          })
        );

        let msgData = messages.value;
        if (activeTab.value === "chat") {
          try {
            msgData = await api.fetchMessages(team, {});
          } catch (e) { }
        }

        if (active) {
          batch(() => {
            tasks.value = taskData;
            agents.value = agentData;
            agentStatsMap.value = statsMap;
            knownAgentNames.value = agentData.map(a => a.name);
            if (activeTab.value === "chat") {
              messages.value = msgData;
            }
          });
        }
      } catch (e) {
        showToast("Failed to refresh data", "error");
      }
    };

    poll();
    const interval = setInterval(poll, 2000);
    return () => { active = false; clearInterval(interval); };
  }, []);

  // When team changes, restore ephemeral state from backing store and re-poll
  useEffect(() => {
    const team = currentTeam.value;
    if (!team) return;
    batch(() => {
      tasks.value = [];
      agents.value = [];
      agentStatsMap.value = {};
      messages.value = [];
      // Restore ephemeral activity state from the per-team backing store
      // (instant — no network round-trip needed).
      _syncSignals(team);
    });
    (async () => {
      try {
        const [taskData, agentData] = await Promise.all([
          api.fetchTasks(team),
          api.fetchAgents(team),
        ]);
        batch(() => {
          tasks.value = taskData;
          agents.value = agentData;
        });
        // Update cached manager name for this team
        const mgr = agentData.find(a => a.role === "manager");
        _pt.managerName[team] = mgr?.name ?? null;
      } catch (e) { }
    })();
  }, [currentTeam.value]);

  // SSE: live agent activity streams — one connection per team.
  // Events are buffered in the per-team backing store (_pt).
  // Only events for currentTeam are pushed into the reactive signals.
  useEffect(() => {
    const teamList = teams.value;
    if (!teamList || !teamList.length) return;

    const connections = {};

    for (const team of teamList) {
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

          // ── task_update (only for current team — tasks signal is single-team) ──
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

          // Activity log (capped)
          const log = _pt.activityLog[team];
          if (log.length >= MAX_LOG_ENTRIES) log.splice(0, log.length - MAX_LOG_ENTRIES + 1);
          log.push(entry);

          // Manager context — bump timestamp or recover if missed turn_started
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

          // Push to reactive signals only for the current team
          if (isCurrent) _syncSignals(team);
        } catch (e) { /* ignore malformed events */ }
      };

      es.onerror = () => {};
      connections[team] = es;
    }

    return () => {
      for (const es of Object.values(connections)) es.close();
    };
  }, [teams.value]);

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
