import { useState, useMemo, useEffect, useCallback } from "preact/hooks";
import { currentTeam, teams, tasks, agents, agentStatsMap, activeTab, openPanel, agentActivityLog } from "../state.js";
import {
  cap, prettyName, fmtTokensShort, fmtRelativeTime, taskIdStr,
  roleBadgeMap, getAgentDotClass,
} from "../utils.js";
import { fetchAgentsCrossTeam } from "../api.js";
import { PillSelect } from "./PillSelect.jsx";

export function AgentsPanel() {
  const team = currentTeam.value;
  const teamList = teams.value || [];
  const allAgents = agents.value;
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;

  const [selectedTeam, setSelectedTeam] = useState("all");
  const [crossTeamAgents, setCrossTeamAgents] = useState([]);
  const [collapsedTeams, setCollapsedTeams] = useState({});

  // Fetch cross-team agents when "All teams" is selected
  useEffect(() => {
    if (selectedTeam === "all") {
      fetchAgentsCrossTeam().then(setCrossTeamAgents);
    }
  }, [selectedTeam]);

  const handleTeamChange = useCallback((t) => {
    setSelectedTeam(t);
  }, []);

  // Determine which agents to show
  const displayAgents = selectedTeam === "all" ? crossTeamAgents : allAgents;
  const activityLog = agentActivityLog.value || [];

  // Group agents by team if showing all teams
  const agentsByTeam = useMemo(() => {
    if (selectedTeam !== "all") return null;
    const grouped = {};
    for (const a of displayAgents) {
      const t = a.team || "unknown";
      if (!grouped[t]) grouped[t] = [];
      grouped[t].push(a);
    }
    return grouped;
  }, [selectedTeam, displayAgents]);

  // Tasks counts
  const { inProgressTasks, doneTodayByAgent } = useMemo(() => {
    const now = new Date();
    const oneDayAgo = new Date(now - 24 * 60 * 60 * 1000);
    const ipTasks = allTasks.filter(t => t.status === "in_progress");
    const dtByAgent = {};
    for (const t of allTasks) {
      if (t.completed_at && new Date(t.completed_at) > oneDayAgo && t.status === "done" && t.assignee) {
        dtByAgent[t.assignee] = (dtByAgent[t.assignee] || 0) + 1;
      }
    }
    return { inProgressTasks: ipTasks, doneTodayByAgent: dtByAgent };
  }, [allTasks]);

  // Render agent row (row-based layout)
  const renderAgentRow = (a) => {
    const stats = (statsMap[a.team] || {})[a.name] || {};
    const currentTask = inProgressTasks.find(t => t.assignee === a.name && t.team === a.team);
    const roleBadge = roleBadgeMap[a.role] || cap(a.role || "engineer");
    const doneToday = doneTodayByAgent[a.name] || 0;

    // Count assigned tasks (non-done, non-cancelled)
    const assignedCount = allTasks.filter(t =>
      t.assignee === a.name &&
      t.team === a.team &&
      t.status !== "done" &&
      t.status !== "cancelled"
    ).length;

    const sidebarDot = getAgentDotClass(a, allTasks, stats);
    const dotClass = "agent-dot agent-" + sidebarDot;

    // Last seen: use last_active_at or most recent task updated_at
    let lastSeen = "never";
    if (a.last_active_at) {
      lastSeen = fmtRelativeTime(a.last_active_at);
    } else {
      const agentTasks = allTasks.filter(t => t.assignee === a.name && t.team === a.team)
        .sort((x, y) => (y.updated_at || "").localeCompare(x.updated_at || ""));
      if (agentTasks.length > 0 && agentTasks[0].updated_at) {
        lastSeen = fmtRelativeTime(agentTasks[0].updated_at);
      }
    }

    const totalTokens = (stats.total_tokens_in || 0) + (stats.total_tokens_out || 0);
    const cost = stats.total_cost_usd != null ? "$" + Number(stats.total_cost_usd).toFixed(2) : "$0.00";

    // Get latest activity for line 2 (log is ordered oldest-first, so search from end)
    const latestActivity = activityLog.findLast(log => log.agent === a.name);
    const activityFresh = latestActivity &&
      (Date.now() - new Date(latestActivity.timestamp).getTime()) < 2 * 60 * 1000;
    const showLine2 = a.pid && (currentTask || activityFresh);

    return (
      <div
        key={a.name}
        class="agent-row"
        onClick={() => { openPanel("agent", a.name); }}
      >
        {/* Line 1: dot | name | role | spacer | last-seen | stats */}
        <div class="agent-row-line1">
          <span class={dotClass}></span>
          <span class="agent-name">{a.name}</span>
          <span class={"agent-role badge-role-" + (a.role || "engineer")}>{roleBadge}</span>
          <span class="agent-spacer"></span>
          <span class="agent-last-seen">{lastSeen}</span>
          <span class="agent-stats">
            <span class="agent-stat">
              <span class="agent-stat-value">{assignedCount}</span>
              <span class="agent-stat-label"> assigned</span>
            </span>
            <span class="agent-stat">
              <span class="agent-stat-value">{doneToday}</span>
              <span class="agent-stat-label"> done</span>
            </span>
            <span class="agent-stat">
              <span class="agent-stat-value">{fmtTokensShort(totalTokens)}</span>
              <span class="agent-stat-label"> tok</span>
            </span>
            <span class="agent-stat">
              <span class="agent-stat-value">{cost}</span>
              <span class="agent-stat-label"> cost</span>
            </span>
          </span>
        </div>

        {/* Line 2: task + tool call (only for active agents) */}
        {showLine2 && latestActivity?.detail && (
          <div class="agent-row-line2">
            {currentTask ? (
              <span
                class="agent-task-id"
                onClick={(e) => { e.stopPropagation(); openPanel("task", currentTask.id); }}
              >
                {taskIdStr(currentTask.id, currentTask.prefix, currentTask.seq)}
              </span>
            ) : (
              <span class="agent-task-title" style="color: var(--text-muted);">
                {a.role === "manager" ? "Managing tasks" : "Working"}
              </span>
            )}
            {latestActivity?.detail && (
              <>
                <span class="agent-tool-sep">|</span>
                <span class="agent-tool-call">
                  {latestActivity.tool}: {latestActivity.detail}
                </span>
              </>
            )}
          </div>
        )}
      </div>
    );
  };

  const toggleTeam = useCallback((teamName) => {
    setCollapsedTeams(prev => ({ ...prev, [teamName]: !prev[teamName] }));
  }, []);

  return (
    <div class={`panel${activeTab.value === "agents" ? " active" : ""}`}>
      {/* Team filter */}
      <div class="agents-team-filter-wrap">
        <PillSelect
          label="Project"
          value={selectedTeam}
          options={[
            { value: "all", label: "All" },
            ...teamList.map(t => {
              const name = typeof t === "object" ? t.name : t;
              return { value: name, label: prettyName(name) };
            })
          ]}
          onChange={handleTeamChange}
        />
      </div>

      {/* Agent list */}
      {selectedTeam === "all" && agentsByTeam ? (
        // Group-by-team view with collapsible headers
        Object.keys(agentsByTeam).sort().map(teamName => {
          const teamAgents = agentsByTeam[teamName];
          const isCollapsed = collapsedTeams[teamName];
          return (
            <div key={teamName} class="agent-team-group">
              <div
                class="agent-team-header"
                onClick={() => toggleTeam(teamName)}
              >
                <span class="agent-team-toggle">{isCollapsed ? "▸" : "▾"}</span>
                <span class="agent-team-name">{prettyName(teamName)}</span>
                <span class="agent-team-count">{teamAgents.length}</span>
              </div>
              {!isCollapsed && (
                <div class="agent-list">
                  {teamAgents.map(a => renderAgentRow(a))}
                </div>
              )}
            </div>
          );
        })
      ) : (
        // Single team view
        <div class="agent-list">
          {displayAgents.map(a => renderAgentRow(a))}
        </div>
      )}
    </div>
  );
}
