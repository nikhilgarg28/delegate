import { useState, useMemo, useEffect, useCallback } from "preact/hooks";
import { currentTeam, teams, tasks, agents, agentStatsMap, activeTab, openPanel } from "../state.js";
import {
  cap, esc, fmtTokensShort, fmtCost, fmtDuration, fmtRelativeTime, taskIdStr,
  roleBadgeMap, getAgentDotClass, getAgentDotTooltip,
} from "../utils.js";
import { CopyBtn } from "./CopyBtn.jsx";
import { fetchAgentsCrossTeam } from "../api.js";
import { PillSelect } from "./PillSelect.jsx";

export function AgentsPanel() {
  const team = currentTeam.value;
  const teamList = teams.value;
  const allAgents = agents.value;
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;

  const [selectedTeam, setSelectedTeam] = useState(team);
  const [crossTeamAgents, setCrossTeamAgents] = useState([]);

  // Reset selectedTeam when currentTeam changes
  useEffect(() => {
    setSelectedTeam(team);
  }, [team]);

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

  // Render agent card (extracted to avoid duplication)
  const renderAgentCard = (a, showTeamBadge = false) => {
    const stats = statsMap[a.name] || {};
    const currentTask = inProgressTasks.find(t => t.assignee === a.name);
    const roleBadge = roleBadgeMap[a.role] || cap(a.role || "worker");
    const doneToday = doneTodayByAgent[a.name] || 0;

    const sidebarDot = getAgentDotClass(a, allTasks, stats);
    const dotClass = "agent-card-" + sidebarDot;
    const dotTooltip = getAgentDotTooltip(sidebarDot, a, allTasks);
    const statusLabel = sidebarDot === "dot-offline" ? "Offline" : (currentTask ? sidebarDot.replace("dot-", "") : "Idle");

    const taskLink = currentTask ? (
      <span
        class="agent-card-task-link"
        onClick={(e) => { e.stopPropagation(); openPanel("task", currentTask.id); }}
      >
        {taskIdStr(currentTask.id)}
      </span>
    ) : (
      <span class="agent-card-idle-label">{statusLabel}</span>
    );

    let activityText = "Offline";
    if (a.pid && currentTask) activityText = "Working on " + currentTask.title;
    else if (a.pid) activityText = "Idle";
    else if (a.unread_inbox > 0) activityText = a.unread_inbox + " message" + (a.unread_inbox !== 1 ? "s" : "") + " waiting";

    let lastActivity = "";
    const agentTasks = allTasks.filter(t => t.assignee === a.name).sort((x, y) => (y.updated_at || "").localeCompare(x.updated_at || ""));
    if (agentTasks.length > 0 && agentTasks[0].updated_at) {
      lastActivity = "Last active " + fmtRelativeTime(agentTasks[0].updated_at);
    }

    const totalTokens = (stats.total_tokens_in || 0) + (stats.total_tokens_out || 0);
    const cost = stats.total_cost_usd != null ? "$" + Number(stats.total_cost_usd).toFixed(2) : "$0.00";
    const agentTime = fmtDuration(stats.agent_time_seconds);

    return (
      <div
        key={a.name}
        class="agent-card-rich"
        onClick={() => { openPanel("agent", a.name); }}
      >
        <div class="agent-card-row1">
          <span class={"agent-card-dot " + dotClass} title={dotTooltip}></span>
          <span class="agent-card-name copyable">{cap(a.name)}<CopyBtn text={a.name} /></span>
          <span class={"agent-card-role badge-role-" + (a.role || "worker")}>{roleBadge}</span>
          {showTeamBadge && a.team && <span class="agent-card-team-badge">{a.team}</span>}
          <span class="agent-card-task-col">{taskLink}</span>
        </div>
        <div class="agent-card-row2">
          <span class="agent-card-activity">{activityText}</span>
          {lastActivity && <span class="agent-card-last-active">{lastActivity}</span>}
        </div>
        <div class="agent-card-row3">
          <span class="agent-card-stat"><span class="agent-card-stat-value">{doneToday}</span><span class="agent-card-stat-label">done today</span></span>
          <span class="agent-card-stat"><span class="agent-card-stat-value">{fmtTokensShort(totalTokens)}</span><span class="agent-card-stat-label">tokens</span></span>
          <span class="agent-card-stat"><span class="agent-card-stat-value">{cost}</span><span class="agent-card-stat-label">cost</span></span>
          <span class="agent-card-stat"><span class="agent-card-stat-value">{agentTime}</span><span class="agent-card-stat-label">uptime</span></span>
        </div>
      </div>
    );
  };

  return (
    <div class={`panel${activeTab.value === "agents" ? " active" : ""}`}>
      {/* Team filter */}
      <div class="agents-team-filter-wrap">
        <PillSelect
          label="Team"
          value={selectedTeam}
          options={[
            { value: "all", label: "All teams" },
            ...teamList.map(t => {
              const name = typeof t === "object" ? t.name : t;
              return { value: name, label: name };
            })
          ]}
          onChange={handleTeamChange}
        />
      </div>

      {/* Agent list */}
      {selectedTeam === "all" && agentsByTeam ? (
        // Group-by-team view
        Object.keys(agentsByTeam).sort().map(teamName => (
          <div key={teamName} class="agents-team-group">
            <div class="agents-team-group-header">{teamName}</div>
            {agentsByTeam[teamName].map(a => renderAgentCard(a, true))}
          </div>
        ))
      ) : (
        // Single team view
        displayAgents.map(a => renderAgentCard(a, true))
      )}
    </div>
  );
}
