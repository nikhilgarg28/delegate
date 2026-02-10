import { useMemo } from "preact/hooks";
import { currentTeam, tasks, agents, agentStatsMap, activeTab, taskPanelId, diffPanelMode, diffPanelTarget } from "../state.js";
import {
  cap, esc, fmtTokensShort, fmtCost, fmtDuration, fmtRelativeTime, taskIdStr,
  roleBadgeMap, getAgentDotClass, getAgentDotTooltip,
} from "../utils.js";

export function AgentsPanel() {
  const allAgents = agents.value;
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;

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

  return (
    <div class="panel" style={{ display: activeTab.value === "agents" ? "" : "none" }}>
      {allAgents.map(a => {
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
            onClick={(e) => { e.stopPropagation(); taskPanelId.value = currentTask.id; }}
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
            onClick={() => { diffPanelMode.value = "agent"; diffPanelTarget.value = a.name; }}
          >
            <div class="agent-card-row1">
              <span class={"agent-card-dot " + dotClass} title={dotTooltip}></span>
              <span class="agent-card-name">{cap(a.name)}</span>
              <span class={"agent-card-role badge-role-" + (a.role || "worker")}>{roleBadge}</span>
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
      })}
    </div>
  );
}
