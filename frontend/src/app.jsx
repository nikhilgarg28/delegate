import { render } from "preact";
import { useEffect, useState, useRef } from "preact/hooks";
import { batch, useSignalEffect } from "@preact/signals";
import {
  currentTeam, teams, humanName, hcHome, tasks, agents, agentStatsMap, messages,
  activeTab, knownAgentNames,
  panelStack, popPanel, closeAllPanels,
  agentLastActivity, agentActivityLog, agentTurnState, managerTurnContext,
  helpOverlayOpen, sidebarCollapsed, bellPopoverOpen, isMuted, teamSwitcherOpen, commandMode,
  syncFromUrl, navigate, navigateTab, taskTeamFilter,
  actionItemCount, awaySummary, getLastSeen, updateLastSeen,
  getLastGreeted, updateLastGreeted,
  fetchWorkflows,
  isInputFocused,
  allTeamsAgents, allTeamsTurnState,
  agentThinking,
  applyBootstrapId, lsKey,
  msgStatusUpdate,
} from "./state.js";
import * as api from "./api.js";
import { cap, prettyName, registerTaskDisplayIds, taskIdStr } from "./utils.js";
import { Sidebar } from "./components/Sidebar.jsx";
import { ChatPanel } from "./components/ChatPanel.jsx";
import { TasksPanel } from "./components/TasksPanel.jsx";
import { AgentsPanel } from "./components/AgentsPanel.jsx";
import { TaskSidePanel, prefetchTaskPanelData, invalidateTaskCache, prefetchTaskData } from "./components/TaskSidePanel.jsx";
import { DiffPanel } from "./components/DiffPanel.jsx";
import { ToastContainer } from "./components/Toast.jsx";
import { HelpOverlay } from "./components/HelpOverlay.jsx";

import { NotificationPopover } from "./components/NotificationPopover.jsx";
import { TeamSwitcher } from "./components/TeamSwitcher.jsx";
import { NoTeamsModal } from "./components/NoTeamsModal.jsx";
import { PwaBanner } from "./components/PwaBanner.jsx";
import { MissionControl } from "./components/MissionControl.jsx";
import { NewProjectModal } from "./components/NewProjectModal.jsx";
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
  thinking:    {},   // team → { agentName: { text, timestamp } }
};
const MAX_LOG_ENTRIES = 500;

// Greeting threshold: only greet after meaningful absence (30 minutes)
const GREETING_THRESHOLD = 30 * 60 * 1000; // 30 minutes in milliseconds

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
      allTeamsTurnState.value  = { ..._pt.turnState };  // All teams' turn state
      agentThinking.value      = _pt.thinking[t]    ? { ..._pt.thinking[t] }    : {};
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
    allTeamsTurnState.value  = { ..._pt.turnState };  // All teams' turn state
    agentThinking.value      = _pt.thinking[team]    ? { ..._pt.thinking[team] }    : {};
  });
}

/**
 * Called after _pt.managerName[team] is resolved (from fetchAgents).
 * If the manager is already in an active turn (turn_started arrived before
 * the fetchAgents response), retroactively set _pt.managerCtx[team] so the
 * thinking box appears.  This fixes the race where a new team manager
 * starts a turn before we know who the manager is.
 */
function _onManagerNameResolved(team, mgrName) {
  if (!mgrName) return;
  const ts = _pt.turnState[team];
  if (!ts) return;
  const agentState = ts[mgrName];
  if (agentState && agentState.inTurn && !_pt.managerCtx[team]) {
    _pt.managerCtx[team] = {
      agent: mgrName,
      team,
      task_id: agentState.taskId ?? null,
      sender: agentState.sender ?? "",
      timestamp: agentState.startedAt ?? new Date().toISOString(),
    };
    if (team === currentTeam.value) {
      managerTurnContext.value = _pt.managerCtx[team];
      _syncSignals(team);
    }
  }
}

// ── Main App ──
function App() {
  // ── Prefetch tracking ──
  const hasPrefetched = useRef(false);

  // ── Keyboard shortcuts ──
  useEffect(() => {
    const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
    const handler = (e) => {
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

      // Scroll to bottom: Cmd+Down (macOS) or Ctrl+End (Windows/Linux)
      if (!isHelpOpen()) {
        if ((isMac && e.key === "ArrowDown" && e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey) ||
            (!isMac && e.key === "End" && e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey)) {
          e.preventDefault();
          const chatLog = document.querySelector(".chat-log");
          if (chatLog) {
            chatLog.scrollTop = chatLog.scrollHeight;
          }
          return;
        }
      }

      // Modifier-key combos (Cmd+K, Cmd+1-9) must fire even when an
      // input is focused — they are global navigation shortcuts.
      if (e.key === "k" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        teamSwitcherOpen.value = !teamSwitcherOpen.value;
        return;
      }
      if ((e.metaKey || e.ctrlKey) && e.key >= "1" && e.key <= "9") {
        const idx = parseInt(e.key) - 1;
        const teamArr = teams.value || [];
        if (idx < teamArr.length) {
          e.preventDefault();
          const teamName = typeof teamArr[idx] === "object" ? teamArr[idx].name : teamArr[idx];
          navigate(teamName, "chat");
          setTimeout(() => {
            const chatInput = document.querySelector(".chat-input");
            if (chatInput) chatInput.focus();
          }, 80);
        }
        return;
      }

      if (isInputFocused()) return;
      // r focuses chat input (when on Chat tab)
      if (e.key === "r" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey && !isHelpOpen() && panelStack.value.length === 0) {
        e.preventDefault();
        const chatInput = document.querySelector(".chat-input");
        if (chatInput) chatInput.focus();
        return;
      }
      // / focuses search — scope to the visible panel (ChatPanel always has .active but uses inline display:none)
      if (e.key === "/" && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey && !isHelpOpen() && panelStack.value.length === 0) {
        e.preventDefault();
        const scope = [...document.querySelectorAll(".panel.active")].find(p => p.offsetParent !== null) || document;
        const searchExpandBtn = scope.querySelector(".filter-search-icon-btn");
        const searchInput = scope.querySelector(".filter-search");
        if (searchExpandBtn) {
          searchExpandBtn.click();
          setTimeout(() => scope.querySelector(".filter-search")?.focus(), 50);
        } else if (searchInput) {
          searchInput.focus();
        }
        return;
      }
      // Tab navigation and sidebar toggle work even with side panels open
      if (e.key === "s" && !e.metaKey && !e.ctrlKey && !e.altKey && !isHelpOpen()) {
        sidebarCollapsed.value = !sidebarCollapsed.value;
        localStorage.setItem(lsKey("sidebar-collapsed"), sidebarCollapsed.value ? "true" : "false");
        return;
      }
      if (e.key === "n" && !e.metaKey && !e.ctrlKey && !e.altKey && !isHelpOpen()) {
        bellPopoverOpen.value = !bellPopoverOpen.value;
        return;
      }
      if (e.key === "t" && !e.metaKey && !e.ctrlKey && !e.altKey && !isHelpOpen()) { navigateTab("tasks"); return; }
      if (e.key === "a" && !e.metaKey && !e.ctrlKey && !e.altKey && !isHelpOpen()) { navigateTab("agents"); return; }
      if (e.key === "m" && !e.metaKey && !e.ctrlKey && !e.altKey && !isHelpOpen()) {
        e.preventDefault();
        const micBtn = document.querySelector(".chat-tool-btn[title*='recording'], .chat-tool-btn[title='Voice input']");
        if (micBtn) micBtn.click();
        return;
      }
      if (e.key === "?") { helpOverlayOpen.value = !helpOverlayOpen.value; return; }
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

  // ── Bootstrap: single request for config + teams + initial team data ──
  const bootstrapTeamRef = useRef(null);  // team whose data was pre-loaded

  useEffect(() => {
    // Fallback: individual fetches (used when /bootstrap isn't available)
    const _fallbackInit = async () => {
      try {
        const cfg = await api.fetchConfig().catch(() => ({}));
        // Apply bootstrap_id FIRST, before any localStorage reads
        if (cfg.bootstrap_id) applyBootstrapId(cfg.bootstrap_id);
        if (cfg.human_name) humanName.value = cfg.human_name;
        if (cfg.hc_home) hcHome.value = cfg.hc_home;
      } catch (e) { }

      // Now safe to read from localStorage
      const lastTeam = localStorage.getItem(lsKey("last-team"));

      try {
        const teamList = await api.fetchTeams().catch(() => []);
        teams.value = teamList;
        const names = teamList.map(t => typeof t === "object" ? t.name : t);
        if (names.length > 0 && !currentTeam.value) {
          currentTeam.value = (lastTeam && names.includes(lastTeam)) ? lastTeam : names[0];
        }
      } catch (e) { }
    };

    (async () => {
      // Try the single-request bootstrap first
      let boot = null;
      // Read lastTeam early but will re-read after bootstrapId is known
      const earlyLastTeam = localStorage.getItem("delegate-last-team");

      try {
        boot = await api.fetchBootstrap(earlyLastTeam);
      } catch (e) {
        // /bootstrap failed or returned non-JSON — fall back to individual fetches
        console.warn("Bootstrap failed, using fallback:", e.message || e);
      }

      if (!boot) {
        await _fallbackInit();
        return;
      }

      // Apply bootstrap_id FIRST, before any other localStorage reads
      const cfg = boot.config || {};
      if (cfg.bootstrap_id) applyBootstrapId(cfg.bootstrap_id);

      // Now apply rest of config
      if (cfg.human_name) humanName.value = cfg.human_name;
      else if (cfg.boss_name) humanName.value = cfg.boss_name;
      if (cfg.hc_home) hcHome.value = cfg.hc_home;

      // Re-read lastTeam with the correct namespace
      const lastTeam = localStorage.getItem(lsKey("last-team"));

      // Apply teams
      teams.value = boot.teams || [];
      const teamNames = (boot.teams || []).map(t => typeof t === "object" ? t.name : t);

      // Apply initial team data before setting currentTeam — this avoids
      // the team-switch useSignalEffect from re-fetching the same data.
      const initial = boot.initial_team;
      if (initial && boot.initial_data) {
        const d = boot.initial_data;
        bootstrapTeamRef.current = initial;  // mark so team-switch effect skips fetch
        batch(() => {
          tasks.value = d.tasks || [];
          registerTaskDisplayIds(tasks.value);
          agents.value = d.agents || [];
          agentStatsMap.value = d.agent_stats || {};
          knownAgentNames.value = (d.agents || []).map(a => a.name);
        });
        // Set manager name from bootstrap data
        const mgr = (d.agents || []).find(a => a.role === "manager");
        _pt.managerName[initial] = mgr?.name ?? null;
      }

      // Now set currentTeam — triggers the team-switch effect
      if (teamNames.length > 0 && !currentTeam.value) {
        currentTeam.value = initial || teamNames[0];
      } else if (teamNames.length > 0 && !teamNames.includes(currentTeam.value)) {
        currentTeam.value = teamNames[0];
      }
    })();
  }, []);

  // ── Polling loop (reads currentTeam.value and taskTeamFilter dynamically each cycle) ──
  // NOTE: Does NOT run immediately on mount — bootstrap already loaded initial data.
  // The first poll fires after 2 seconds, giving the UI time to become interactive.
  // Uses setTimeout chain (not setInterval) to prevent connection stacking when the
  // server is slow — only one poll cycle in-flight at a time.
  useEffect(() => {
    let active = true;
    let timer = null;
    const poll = async () => {
      if (!active) return;
      const t = currentTeam.value;
      const filter = taskTeamFilter.value;
      if (!t) {
        // No team yet — retry after delay
        if (active) timer = setTimeout(poll, 2000);
        return;
      }

      try {
        const taskDataPromise = filter === "all"
          ? api.fetchAllTasks()
          : filter === "current"
            ? api.fetchTasks(t)
            : api.fetchTasks(filter);

        const [taskData, agentData, allAgentData] = await Promise.all([
          taskDataPromise,
          api.fetchAgents(t),
          api.fetchAgentsCrossTeam(),
        ]);

        // Only fetch stats when on the agents tab — saves N+1 DB queries otherwise
        let statsMapByTeam = agentStatsMap.value; // keep previous
        if (activeTab.value === "agents") {
          try {
            // Fetch stats for all teams to support cross-team agent views
            const teamList = teams.value || [];
            const statsPromises = teamList.map(async (team) => {
              const teamName = typeof team === 'string' ? team : team.name;
              try {
                const stats = await api.fetchAllAgentStats(teamName);
                return [teamName, stats];
              } catch (e) {
                return [teamName, {}];
              }
            });
            const statsResults = await Promise.all(statsPromises);
            statsMapByTeam = Object.fromEntries(statsResults);
          } catch (e) { }
        }

        if (active && t === currentTeam.value && filter === taskTeamFilter.value) {
          registerTaskDisplayIds(taskData);
          batch(() => {
            tasks.value = taskData;
            agents.value = agentData;
            agentStatsMap.value = statsMapByTeam;
            knownAgentNames.value = agentData.map(a => a.name);
            allTeamsAgents.value = allAgentData;
          });

          // Prefetch task panel data for recent tasks (once per session, deferred)
          if (!hasPrefetched.current && taskData.length > 0) {
            hasPrefetched.current = true;
            const recentTaskIds = taskData.slice(0, 50).map(t => t.id);
            // Defer prefetch by 5s so it doesn't compete with interactive requests
            setTimeout(() => prefetchTaskPanelData(recentTaskIds).catch(() => {}), 5000);
          }
        }
      } catch (e) {
        showToast("Failed to refresh data", "error");
      }
      // Schedule next poll only after this one completes
      if (active) timer = setTimeout(poll, 2000);
    };

    // Start polling after delay — initial data comes from /bootstrap
    timer = setTimeout(poll, 2000);
    return () => { active = false; clearTimeout(timer); };
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
    localStorage.setItem(lsKey("last-team"), t);

    // If this team was pre-loaded by /bootstrap, skip the fetch — data is
    // already in the signals.  Only do lightweight housekeeping.
    if (t === bootstrapTeamRef.current) {
      bootstrapTeamRef.current = null;  // consume — only skip once
      _syncSignalsNow(t);
      fetchWorkflows(t);

      // Send return-from-away greeting if the user hasn't been greeted
      // recently.  First-run welcomes are sent server-side during project
      // creation, so we only greet here when lastGreetedTime is known (i.e.
      // the user has visited before) and enough time has passed.
      const now = Date.now();
      const lastGreeted = getLastGreeted(t);
      const lastGreetedTime = lastGreeted ? new Date(lastGreeted).getTime() : null;
      const shouldGreet = lastGreetedTime && (now - lastGreetedTime) >= GREETING_THRESHOLD;
      if (shouldGreet) {
        (async () => {
          try {
            await api.greetTeam(t, getLastSeen());
            updateLastGreeted(t);
          } catch (e) {
            console.warn("greetTeam failed:", e);
          }
        })();
      }
      return;
    }

    // Clear stale data from previous team
    batch(() => {
      tasks.value = [];
      agents.value = [];
      agentStatsMap.value = {};
      messages.value = [];
      taskTeamFilter.value = "all";  // Reset to all teams on team switch
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
        registerTaskDisplayIds(taskData);
        batch(() => {
          tasks.value = taskData;
          agents.value = agentData;
        });
        const mgr = agentData.find(a => a.role === "manager");
        _pt.managerName[t] = mgr?.name ?? null;
        _onManagerNameResolved(t, _pt.managerName[t]);

        // Send return-from-away greeting if enough time has passed.
        // First-run welcomes are sent server-side during project creation,
        // so we only greet when lastGreetedTime is known and stale.
        const now = Date.now();
        const lastGreeted = getLastGreeted(t);
        const lastGreetedTime = lastGreeted ? new Date(lastGreeted).getTime() : null;
        const shouldGreet = lastGreetedTime && (now - lastGreetedTime) >= GREETING_THRESHOLD;

        if (shouldGreet) {
          try {
            await api.greetTeam(t, getLastSeen());
            updateLastGreeted(t);
          } catch (e) {
            console.warn("greetTeam failed:", e);
          }
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

          // Send greeting if away time exceeds greeting threshold
          const now = Date.now();
          const team = currentTeam.value;
          const lastGreeted = getLastGreeted(team);
          const lastGreetedTime = lastGreeted ? new Date(lastGreeted).getTime() : null;
          const shouldGreet = awayMs >= GREETING_THRESHOLD &&
                              (!lastGreetedTime || (now - lastGreetedTime) >= GREETING_THRESHOLD);

          if (shouldGreet) {
            api.greetTeam(team, lastSeen).catch(() => {});
            updateLastGreeted(team);
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

  // ── SSE: start connection once on mount, unconditionally ──
  // SharedWorker multiplexes a single /stream connection across all tabs.
  // Falls back to a direct EventSource if SharedWorker is unavailable.
  //
  // NOTE: The connection is established regardless of whether `teams` is
  // populated.  This ensures `teams_refresh` events are received on a
  // fresh install (zero teams) so the first project creation is handled
  // live without a page reload.
  useEffect(() => {
    // Handle an SSE event.  All signal reads happen at call time so this
    // closure does NOT need to be recreated when teams change.
    const handleSSE = (entry) => {
      if (entry.type === "connected") return;

      const team = entry.team;
      if (!team) return;  // skip events without team field

      const isCurrent = (team === currentTeam.value);

      // ── turn_started ──
      if (entry.type === "turn_started") {
        if (!_pt.turnState[team]) _pt.turnState[team] = {};
        _pt.turnState[team][entry.agent] = {
          inTurn: true,
          taskId: entry.task_id ?? null,
          sender: entry.sender ?? "",
          startedAt: new Date().toISOString()
        };

        // Push a visual separator at the START of each turn
        if (!_pt.activityLog[team]) _pt.activityLog[team] = [];
        _pt.activityLog[team].push({
          type: "turn_separator",
          agent: entry.agent,
          timestamp: new Date().toISOString(),
          task_id: entry.task_id ?? null,
          sender: entry.sender ?? ""
        });

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
        if (_pt.turnState[team] && _pt.turnState[team][entry.agent]) {
          _pt.turnState[team][entry.agent] = {
            inTurn: false,
            taskId: null
          };
        }

        // Clear thinking for this agent (turn is over)
        if (_pt.thinking[team]) {
          delete _pt.thinking[team][entry.agent];
        }

        const ctx = _pt.managerCtx[team];
        if (ctx && ctx.agent === entry.agent) {
          _pt.managerCtx[team] = null;
          if (isCurrent) managerTurnContext.value = null;
        }

        if (isCurrent) _syncSignals(team);
        return;
      }

      // ── agent_thinking (Mission Control live text) ──
      if (entry.type === "agent_thinking") {
        if (!_pt.thinking[team]) _pt.thinking[team] = {};
        _pt.thinking[team][entry.agent] = {
          text: entry.text,
          timestamp: entry.timestamp,
        };
        if (isCurrent) _syncSignals(team);
        return;
      }

      // ── rate_limit (Claude API throttling) ──
      if (entry.type === "rate_limit") {
        if (isCurrent) {
          showToast(`${entry.agent} is being rate-limited by Claude — retrying automatically`, "warning");
        }
        return;
      }

      // ── teams_refresh (new project created) ──
      if (entry.type === "teams_refresh") {
        api.fetchTeams().then(t => { teams.value = t; }).catch(() => {});
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
            registerTaskDisplayIds([updated]);
            tasks.value = next;

            // Invalidate task panel cache so reopening shows fresh data
            invalidateTaskCache(tid);
            // Prefetch fresh data for all tabs
            prefetchTaskData(tid);

            const human = humanName.value;

            if (entry.assignee && entry.assignee.toLowerCase() === human.toLowerCase() &&
                (entry.status === "in_approval" || entry.status === "merge_failed")) {
              const title = `${taskIdStr(tid)} "${task.title}"`;
              const body = entry.status === "in_approval"
                ? "Needs your approval"
                : "Merge failed -- needs resolution";
              showActionToast({ title, body, taskId: tid, type: "info" });
            }

            if (entry.status === "done") {
              const title = `${taskIdStr(tid)} "${task.title}"`;
              const body = "Merged successfully";
              showActionToast({ title, body, taskId: tid, type: "success" });
            }
          }
        }
        return;
      }

      // ── msg_status (seen / processed) ──
      if (entry.type === "msg_status") {
        if (isCurrent) {
          // Bump the signal so ChatPanel can patch its local msgs
          msgStatusUpdate.value = {
            seq: (msgStatusUpdate.value?.seq || 0) + 1,
            msg_ids: entry.msg_ids,
            field: entry.field,
            value: entry.value,
          };
        }
        return;
      }

      // ── agent_activity ──
      if (!_pt.activity[team]) _pt.activity[team] = {};
      _pt.activity[team][entry.agent] = entry;

      if (!_pt.activityLog[team]) _pt.activityLog[team] = [];
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

      if (!_pt.turnState[team]) _pt.turnState[team] = {};
      if (!_pt.turnState[team][entry.agent] || !_pt.turnState[team][entry.agent].inTurn) {
        _pt.turnState[team][entry.agent] = {
          inTurn: true,
          taskId: entry.task_id ?? null,
          sender: "",
          startedAt: new Date().toISOString()
        };
      }

      if (isCurrent) _syncSignals(team);
    };

    // Recover turn state on SSE (re)connect — fills _pt.turnState
    // with whatever turns are currently active server-side.
    const recoverTurnState = () => {
      api.fetchActiveTurns().then(turns => {
        if (!Array.isArray(turns) || turns.length === 0) return;
        for (const t of turns) {
          const team = t.team;
          if (!team) continue;
          if (!_pt.turnState[team]) _pt.turnState[team] = {};
          _pt.turnState[team][t.agent] = {
            inTurn: true,
            taskId: t.task_id ?? null,
            sender: t.sender ?? "",
            startedAt: t.timestamp ?? new Date().toISOString(),
          };

          // Set manager context if applicable
          const mgrName = _pt.managerName[team];
          if (mgrName && mgrName === t.agent) {
            _pt.managerCtx[team] = t;
            if (team === currentTeam.value) {
              managerTurnContext.value = t;
            }
          }

          if (team === currentTeam.value) _syncSignals(team);
        }
      }).catch(() => {});
    };

    // Try SharedWorker first (shares 1 SSE connection across all tabs)
    let cleanup;
    if (typeof SharedWorker !== "undefined") {
      try {
        const worker = new SharedWorker("/static/sse-worker.js", { name: "delegate-sse" });
        worker.port.onmessage = (evt) => {
          if (evt.data.type === "sse") handleSSE(evt.data.data);
          // Recover turn state on (re)connect
          if (evt.data.type === "status" && evt.data.connected) recoverTurnState();
        };
        worker.port.start();
        worker.port.postMessage({ type: "init" });
        cleanup = () => worker.port.close();
      } catch (_) {
        // SharedWorker failed (e.g. security restriction) — fall through to direct
      }
    }

    // Fallback: direct EventSource (1 connection per tab)
    if (!cleanup) {
      const es = new EventSource("/stream");
      es.onmessage = (evt) => {
        try { handleSSE(JSON.parse(evt.data)); } catch (_) {}
      };
      es.onopen = () => recoverTurnState();
      es.onerror = () => {};
      cleanup = () => es.close();
    }

    return cleanup;
  }, []);

  // ── Per-team backing stores: init when teams list changes ──
  // Separate from SSE setup so teams changes don't tear down the connection.
  useSignalEffect(() => {
    const list = teams.value;              // ← auto-tracked
    if (!list || list.length === 0) return;

    for (const teamObj of list) {
      const team = typeof teamObj === "object" ? teamObj.name : teamObj;
      if (!_pt.activity[team])    _pt.activity[team]    = {};
      if (!_pt.activityLog[team]) _pt.activityLog[team] = [];
      if (!_pt.turnState[team])   _pt.turnState[team]   = {};
      if (!_pt.thinking[team])    _pt.thinking[team]    = {};

      // Fetch manager name for this team (one-time, best-effort).
      // Also retroactively sets managerCtx if manager is already in a turn.
      if (_pt.managerName[team] === undefined) {
        api.fetchAgents(team).then(agentData => {
          const mgr = agentData.find(a => a.role === "manager");
          _pt.managerName[team] = mgr?.name ?? null;
          _onManagerNameResolved(team, _pt.managerName[team]);
        }).catch(() => { _pt.managerName[team] = null; });
      }
    }
  });

  const projectName = currentTeam.value || "";

  return (
    <>
      <div style="display:flex;flex-direction:column;width:100%;height:100%">
        <PwaBanner />
        <div style="display:flex;flex-direction:row;flex:1;min-height:0">
          <Sidebar />
          <div class="workspace">
            <div class="workspace-header">
              <span class="workspace-project-name">{prettyName(projectName)}</span>
            </div>
            <div class="workspace-body">
              <div class="main">
                <div class="content">
                  <ChatPanel />
                  <TasksPanel />
                  <AgentsPanel />
                </div>
              </div>
              {activeTab.value === "chat" && <MissionControl />}
            </div>
          </div>
        </div>
      </div>
      <TaskSidePanel />
      <DiffPanel />
      <HelpOverlay />
      <NotificationPopover />
      <TeamSwitcher open={teamSwitcherOpen.value} onClose={() => teamSwitcherOpen.value = false} />
      <NoTeamsModal />
      <NewProjectModal />
      <ToastContainer />
    </>
  );
}

// ── Register PWA service worker ──
if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
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
