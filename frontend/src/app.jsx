import { render } from "preact";
import { useEffect, useCallback } from "preact/hooks";
import { batch } from "@preact/signals";
import {
  currentTeam, teams, bossName, tasks, agents, agentStatsMap, messages,
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

  // When team changes, clear data and re-poll
  useEffect(() => {
    const team = currentTeam.value;
    if (!team) return;
    batch(() => {
      tasks.value = [];
      agents.value = [];
      agentStatsMap.value = {};
      messages.value = [];
      // Clear ephemeral activity state from the previous team
      managerTurnContext.value = null;
      agentLastActivity.value = {};
      agentActivityLog.value = [];
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
      } catch (e) { }
    })();
  }, [currentTeam.value]);

  // SSE: live agent activity stream
  useEffect(() => {
    const team = currentTeam.value;
    if (!team) return;

    const MAX_LOG_ENTRIES = 500;
    let es = null;

    const connect = () => {
      es = new EventSource(`/teams/${team}/activity/stream`);

      es.onmessage = (evt) => {
        try {
          const entry = JSON.parse(evt.data);
          if (entry.type === "connected") return;

          if (entry.type === "turn_started") {
            const managerAgent = agents.value?.find(a => a.role === "manager");
            if (managerAgent && managerAgent.name === entry.agent) {
              managerTurnContext.value = entry;
            }
            return;
          }

          if (entry.type === "turn_ended") {
            if (managerTurnContext.value && managerTurnContext.value.agent === entry.agent) {
              managerTurnContext.value = null;
            }
            return;
          }

          if (entry.type === "task_update") {
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
            return;
          }

          const prev = agentLastActivity.value;
          agentLastActivity.value = { ...prev, [entry.agent]: entry };

          // Keep manager activity bar alive: bump timestamp on every activity event
          // from the active manager so the safety timeout keeps resetting.
          const mtc = managerTurnContext.value;
          if (mtc && mtc.agent === entry.agent) {
            managerTurnContext.value = { ...mtc, timestamp: entry.timestamp };
          }

          const log = agentActivityLog.value;
          const next = log.length >= MAX_LOG_ENTRIES
            ? [...log.slice(log.length - MAX_LOG_ENTRIES + 1), entry]
            : [...log, entry];
          agentActivityLog.value = next;
        } catch (e) { /* ignore malformed events */ }
      };

      es.onerror = () => { };
    };

    connect();

    return () => {
      if (es) es.close();
    };
  }, [currentTeam.value]);

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
