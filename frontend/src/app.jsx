import { render } from "preact";
import { useEffect, useCallback } from "preact/hooks";
import { batch } from "@preact/signals";
import {
  currentTeam, teams, bossName, tasks, agents, agentStatsMap, messages,
  activeTab, knownAgentNames,
  taskPanelId, diffPanelMode, diffPanelTarget,
} from "./state.js";
import * as api from "./api.js";
import { Sidebar } from "./components/Sidebar.jsx";
import { Header } from "./components/Header.jsx";
import { ChatPanel } from "./components/ChatPanel.jsx";
import { TasksPanel } from "./components/TasksPanel.jsx";
import { AgentsPanel } from "./components/AgentsPanel.jsx";
import { TaskSidePanel } from "./components/TaskSidePanel.jsx";
import { DiffPanel } from "./components/DiffPanel.jsx";

// ── Main App ──
function App() {
  const tab = activeTab.value;

  // Keyboard handler
  useEffect(() => {
    const handler = (e) => {
      if (e.key === "Escape") {
        if (taskPanelId.value !== null) { taskPanelId.value = null; return; }
        if (diffPanelMode.value !== null) { diffPanelMode.value = null; diffPanelTarget.value = null; return; }
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // Hash routing
  useEffect(() => {
    const onHash = () => {
      const hash = window.location.hash.replace("#", "");
      const valid = ["chat", "tasks", "agents"];
      if (valid.includes(hash)) activeTab.value = hash;
    };
    window.addEventListener("hashchange", onHash);
    // Init from hash
    onHash();
    return () => window.removeEventListener("hashchange", onHash);
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
        // Try loading teams if not available
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
        // Fetch tasks and agents in parallel (needed by sidebar + active tab)
        const [taskData, agentData] = await Promise.all([
          api.fetchTasks(team),
          api.fetchAgents(team),
        ]);

        // Fetch agent stats in parallel
        const statsMap = {};
        await Promise.all(
          agentData.map(async (a) => {
            try {
              const s = await api.fetchAgentStats(team, a.name);
              if (s) statsMap[a.name] = s;
            } catch (e) { }
          })
        );

        // Fetch messages only if chat tab is active
        let msgData = messages.value;
        if (activeTab.value === "chat") {
          try {
            msgData = await api.fetchMessages(team, {});
          } catch (e) { }
        }

        // Batch update signals (single re-render)
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
        console.warn("Poll error:", e);
      }
    };

    // Initial poll
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
    });
  }, [currentTeam.value]);

  return (
    <>
      <Sidebar />
      <div class="main">
        <Header />
        <div class="content">
          <ChatPanel />
          <TasksPanel />
          <AgentsPanel />
        </div>
      </div>
      <TaskSidePanel />
      <DiffPanel />
    </>
  );
}

// ── Mount ──
render(<App />, document.getElementById("app"));
