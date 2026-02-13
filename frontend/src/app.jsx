import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { batch, useSignalEffect } from "@preact/signals";
import {
  currentTeam, teams, humanName, hcHome, tasks, agents, agentStatsMap, messages,
  activeTab, knownAgentNames,
  panelStack, popPanel, closeAllPanels,
  agentLastActivity, agentActivityLog, agentTurnState, managerTurnContext,
  helpOverlayOpen, sidebarCollapsed, bellPopoverOpen, isMuted, teamSwitcherOpen, commandMode,
  syncFromUrl, navigate, navigateTab, taskTeamFilter,
  actionItemCount, awaySummary, getLastSeen, updateLastSeen,
  fetchWorkflows,
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
import { NotificationBell } from "./components/NotificationBell.jsx";
import { NotificationPopover } from "./components/NotificationPopover.jsx";
import { TeamSwitcher } from "./components/TeamSwitcher.jsx";
import { showToast, showActionToast, showReturnToast } from "./toast.js";

// ── Per-team backing stores (plain objects, not signals) ──
// SSE events for ALL teams are buffered here.  Only the current-team
// slice is pushed into the reactive signals so non-active teams don't
// trigger re-renders.
const _pt = {
  activity:    {},   // team → { agentName: entry }
  activityLog: {},   // team → [entry, …]
  managerCtx:  {},   // team → ctx | null
  managerName: {},   // team → managerAgentName | null
  turnState:   {},   // team → { agentName: { inTurn: bool, taskId: num|null } }
};
const MAX_LOG_ENTRIES = 500;

// Track which teams have been greeted in this browser session
const _greetedTeams = new Set();

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
      agentTurnState.value     = _pt.turnState[t]   ? { ..._pt.turnState[t] }   : {};
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
    agentTurnState.value     = _pt.turnState[team]   ? { ..._pt.turnState[team] }   : {};
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
      // Help overlay blocks all shortcuts (user is reading help)
      const isHelpOpen = () => helpOverlayOpen.value;

      if (e.key === "Escape") {
        if (teamSwitcherOpen.value) { teamSwitcherOpen.value = false; return; }
        if (bellPopoverOpen.value) { bellPopoverOpen.value = false; return; }
        if (helpOverlayOpen.value) { helpOverlayOpen.value = false; return; }
        if (panelStack.value.length > 0) { popPanel(); return; }
        if (commandMode.value) { commandMode.value = false; return; }
        if (isInputFocused()) { document.activeElement.blur(); return; }
        return;
      }
      if (isInputFocused()) return;
      // / should only work when no overlays (to avoid conflicts with typing)
      if (e.key === "/" && !isHelpOpen() && panelStack.value.length === 0) {
        e.preventDefault();
        const chatInput = document.querySelector(".chat-input-box textarea");
        if (chatInput) chatInput.focus();
        return;
      }
      // Tab navigation and sidebar toggle work even with side panels open
      if (e.key === "s" && !isHelpOpen()) {
        sidebarCollapsed.value = !sidebarCollapsed.value;
        localStorage.setItem("delegate-sidebar-collapsed", sidebarCollapsed.value ? "true" : "false");
        return;
      }
      if (e.key === "n" && !isHelpOpen()) {
        bellPopoverOpen.value = !bellPopoverOpen.value;
        return;
      }
      if (e.key === "c" && !isHelpOpen()) { navigateTab("chat"); return; }
      if (e.key === "t" && !isHelpOpen()) { navigateTab("tasks"); return; }
      if (e.key === "a" && !isHelpOpen()) { navigateTab("agents"); return; }
      if (e.key === "m" && !isHelpOpen()) {
        e.preventDefault();
        isMuted.value = !isMuted.value;
        localStorage.setItem("delegate-muted", isMuted.value ? "true" : "false");
        return;
      }
      if (e.key === "?" && !isInputFocused()) { helpOverlayOpen.value = !helpOverlayOpen.value; return; }
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        teamSwitcherOpen.value = !teamSwitcherOpen.value;
        return;
      }
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, []);

  // ── URL routing: /{team}/{tab} ──
  useEffect(() => {
    const onPopState = () => syncFromUrl();
    window.addEventListener("popstate", onPopState);
    // Parse initial URL
    syncFromUrl();
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // ── Bootstrap: fetch config + teams, then fix URL if needed ──
  useEffect(() => {
    (async () => {
      try {
        const cfg = await api.fetchConfig();
        if (cfg.human_name) humanName.value = cfg.human_name;
        else if (cfg.boss_name) humanName.value = cfg.boss_name;
        if (cfg.hc_home) hcHome.value = cfg.hc_home;
      } catch (e) { }
      try {
        const teamList = await api.fetchTeams();
        teams.value = teamList;

        // If URL didn't set a valid team, check localStorage or navigate to the first team
        if (teamList.length > 0 && !currentTeam.value) {
          const lastTeam = localStorage.getItem("delegate-last-team");
          const targetTeam = lastTeam && teamList.includes(lastTeam) ? lastTeam : teamList[0];
          navigate(targetTeam, "chat");
        } else if (teamList.length > 0 && !teamList.includes(currentTeam.value)) {
          // URL had an invalid team — fix it
          navigate(teamList[0], activeTab.value || "chat");
        }
      } catch (e) { }
    })();
  }, []);

  // ── Polling loop (reads currentTeam.value and taskTeamFilter dynamically each cycle) ──
  useEffect(() => {
    let active = true;
    const poll = async () => {
      if (!active) return;
      const t = currentTeam.value;
      const filter = taskTeamFilter.value;
      if (!t) return; // No team yet — bootstrap will set one

      try {
        const taskDataPromise = filter === "all"
          ? api.fetchAllTasks()
          : filter === "current"
            ? api.fetchTasks(t)
            : api.fetchTasks(filter);

        const [taskData, agentData] = await Promise.all([
          taskDataPromise,
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

        if (active && t === currentTeam.value && filter === taskTeamFilter.value) {
          batch(() => {
            tasks.value = taskData;
            agents.value = agentData;
            agentStatsMap.value = statsMap;
            knownAgentNames.value = agentData.map(a => a.name);
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

  // ── Team switch: clear data + re-fetch ──
  // Uses useSignalEffect (from @preact/signals) which auto-tracks
  // signal reads and re-runs when they change.  This is more reliable
  // than useEffect([team]) which depends on Preact's dep comparison
  // during signal-triggered re-renders.
  useSignalEffect(() => {
    const t = currentTeam.value;           // ← auto-tracked
    if (!t) return;

    // Persist last-selected team to localStorage
    localStorage.setItem("delegate-last-team", t);

    // Clear stale data from previous team
    batch(() => {
      tasks.value = [];
      agents.value = [];
      agentStatsMap.value = {};
      messages.value = [];
      taskTeamFilter.value = "current";  // Reset to current team on team switch
      _syncSignalsNow(t);
    });

    // Fetch data for new team (messages handled by ChatPanel)
    (async () => {
      try {
        // Fetch workflows (cached — won't refetch if already loaded)
        fetchWorkflows(t);

        const [taskData, agentData] = await Promise.all([
          api.fetchTasks(t),
          api.fetchAgents(t),
        ]);
        // Guard: only apply if the team hasn't changed while we were fetching
        if (t !== currentTeam.value) return;
        batch(() => {
          tasks.value = taskData;
          agents.value = agentData;
        });
        const mgr = agentData.find(a => a.role === "manager");
        _pt.managerName[t] = mgr?.name ?? null;

        // Send welcome greeting if this is the first time viewing this team
        if (!_greetedTeams.has(t)) {
          _greetedTeams.add(t);
          api.greetTeam(t).catch(() => {});
        }
      } catch (e) { }
    })();
  });

  // ── Tab badge: update document.title with action item count ──
  useSignalEffect(() => {
    const count = actionItemCount.value;
    document.title = count > 0 ? `(${count}) delegate` : "delegate";
  });

  // ── Last-seen tracking: heartbeat + initial update ──
  useEffect(() => {
    // Update last-seen on page load
    updateLastSeen();

    // Heartbeat: update every 60s while page is visible
    const heartbeat = setInterval(() => {
      if (!document.hidden) {
        updateLastSeen();
      }
    }, 60000);

    return () => clearInterval(heartbeat);
  }, []);

  // ── Visibility/away detection: return-from-away flow ──
  useEffect(() => {
    let lastVisibleTime = Date.now();

    const handleVisibilityChange = () => {
      if (document.hidden) {
        // Tab going hidden -- record time
        lastVisibleTime = Date.now();
      } else {
        // Tab becoming visible -- check if away long enough
        const awayMs = Date.now() - lastVisibleTime;
        const AWAY_THRESHOLD = 5 * 60 * 1000; // 5 minutes

        if (awayMs >= AWAY_THRESHOLD) {
          // Compute away summary
          const lastSeen = getLastSeen();
          const awayMinutes = Math.floor(awayMs / 60000);
          const hours = Math.floor(awayMinutes / 60);
          const minutes = awayMinutes % 60;
          const awayDuration = hours > 0
            ? `${hours}h ${minutes}m`
            : `${minutes}m`;

          // Get action items (already filtered to in_approval, merge_failed)
          const currentActionItems = tasks.value.filter(t =>
            t.assignee && t.assignee.toLowerCase() === humanName.value.toLowerCase() &&
            ["in_approval", "merge_failed"].includes(t.status)
          );

          // Get completed tasks since lastSeen
          const completed = lastSeen
            ? tasks.value.filter(t =>
                t.status === "done" &&
                t.completed_at &&
                t.completed_at > lastSeen
              )
            : [];

          // Get unread message count
          const unreadCount = lastSeen
            ? messages.value.filter(m =>
                m.recipient === humanName.value &&
                m.created_at > lastSeen
              ).length
            : 0;

          // Populate awaySummary signal
          awaySummary.value = {
            awayDuration,
            actionItems: currentActionItems,
            completed,
            unreadCount
          };

          // Show toast if there are items to report
          if (currentActionItems.length > 0 || completed.length > 0 || unreadCount > 0) {
            showReturnToast(awaySummary.value);
          }
        }

        // Always update last-seen when tab becomes visible
        updateLastSeen();
      }
    };

    document.addEventListener("visibilitychange", handleVisibilityChange);
    return () => document.removeEventListener("visibilitychange", handleVisibilityChange);
  }, []);

  // ── Clear awaySummary when bell popover closes ──
  useSignalEffect(() => {
    if (bellPopoverOpen.value === false && awaySummary.value !== null) {
      awaySummary.value = null;
    }
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
      if (!_pt.turnState[team])   _pt.turnState[team]   = {};

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
            // Track turn state for all agents
            if (!_pt.turnState[team]) _pt.turnState[team] = {};
            _pt.turnState[team][entry.agent] = {
              inTurn: true,
              taskId: entry.task_id ?? null,
              sender: entry.sender ?? ""
            };

            const mgrName = _pt.managerName[team];
            if (mgrName && mgrName === entry.agent) {
              _pt.managerCtx[team] = entry;
              if (isCurrent) managerTurnContext.value = entry;
            }

            if (isCurrent) _syncSignals(team);
            return;
          }

          // ── turn_ended ──
          if (entry.type === "turn_ended") {
            // Update turn state for all agents
            if (_pt.turnState[team] && _pt.turnState[team][entry.agent]) {
              _pt.turnState[team][entry.agent] = {
                inTurn: false,
                taskId: _pt.turnState[team][entry.agent].taskId
              };
            }

            // Clear activity log entries for this agent
            const log = _pt.activityLog[team];
            if (log) {
              _pt.activityLog[team] = log.filter(e => e.agent !== entry.agent);
            }

            const ctx = _pt.managerCtx[team];
            if (ctx && ctx.agent === entry.agent) {
              _pt.managerCtx[team] = null;
              if (isCurrent) managerTurnContext.value = null;
            }

            if (isCurrent) _syncSignals(team);
            return;
          }

          // ── task_update ──
          if (entry.type === "task_update") {
            if (isCurrent) {
              const tid = entry.task_id;
              const cur = tasks.value;
              const idx = cur.findIndex(t => t.id === tid);
              if (idx !== -1) {
                const task = cur[idx];
                const updated = { ...task };
                if (entry.status !== undefined) updated.status = entry.status;
                if (entry.assignee !== undefined) updated.assignee = entry.assignee;
                const next = [...cur];
                next[idx] = updated;
                tasks.value = next;

                // Fire toasts for task status changes
                const human = humanName.value;

                // Task assigned to human (in_approval or merge_failed)
                if (entry.assignee && entry.assignee.toLowerCase() === human.toLowerCase() &&
                    (entry.status === "in_approval" || entry.status === "merge_failed")) {
                  const title = `T${String(tid).padStart(4, "0")} "${task.title}"`;
                  const body = entry.status === "in_approval"
                    ? "Needs your approval"
                    : "Merge failed -- needs resolution";
                  showActionToast({ title, body, taskId: tid, type: "info" });
                }

                // Task completed
                if (entry.status === "done") {
                  const title = `T${String(tid).padStart(4, "0")} "${task.title}"`;
                  const body = "Merged successfully";
                  showActionToast({ title, body, taskId: tid, type: "success" });
                }
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
        <div class="main-header">
          <NotificationBell />
        </div>
        <div class="content">
          <ChatPanel />
          <TasksPanel />
          <AgentsPanel />
        </div>
      </div>
      <TaskSidePanel />
      <DiffPanel />
      <HelpOverlay />
      <NotificationPopover />
      <TeamSwitcher open={teamSwitcherOpen.value} onClose={() => teamSwitcherOpen.value = false} />
      <ToastContainer />
    </>
  );
}

// ── Mount ──
render(<App />, document.getElementById("app"));

// ── Test exports (for Playwright E2E tests) ──
if (typeof window !== "undefined") {
  window.__test__ = {
    showToast,
    showActionToast,
    showReturnToast,
  };
}
