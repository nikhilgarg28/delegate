import { useState, useCallback, useEffect, useRef } from "preact/hooks";
import {
  currentTeam, teams, bossName, tasks, agents, agentStatsMap,
  activeTab, actionItems, openTaskCount, taskPanelId, diffPanelMode, diffPanelTarget,
} from "../state.js";
import {
  cap, esc, fmtStatus, fmtCost, fmtRelativeTime, fmtRelativeTimeShort,
  taskTier, taskIdStr, getAgentDotClass, getAgentDotTooltip,
} from "../utils.js";

// ── Team selector ──
function TeamSelector() {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  const team = currentTeam.value;
  const teamList = teams.value;
  const isSingle = teamList.length <= 1;

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("click", handler);
    return () => document.removeEventListener("click", handler);
  }, [open]);

  const selectTeam = useCallback((t) => {
    setOpen(false);
    if (t !== currentTeam.value) currentTeam.value = t;
  }, []);

  return (
    <div
      ref={ref}
      class={"sidebar-team-selector" + (isSingle ? " single-team" : "")}
      onClick={(e) => { e.stopPropagation(); if (!isSingle) setOpen(!open); }}
    >
      <span class="sidebar-team-name">{team || "No team"}</span>
      <span class="sidebar-team-chevron" style={open ? { transform: "rotate(180deg)" } : {}}>
        &#9662;
      </span>
      {open && (
        <div class="sidebar-team-dropdown">
          {teamList.map(t => (
            <div
              key={t}
              class={"sidebar-team-option" + (t === team ? " active" : "")}
              onClick={(e) => { e.stopPropagation(); selectTeam(t); }}
            >
              {t}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Action Required widget ──
function ActionWidget() {
  const items = actionItems.value;

  return (
    <div class="sidebar-widget sidebar-action-widget">
      <div class="sidebar-widget-header">
        <span class="sidebar-action-header">
          <span>Action Required</span>
          {items.length > 0 && (
            <span class="sidebar-action-count">{items.length}</span>
          )}
        </span>
        {items.length > 0 && (
          <a class="sidebar-see-all" onClick={() => { activeTab.value = "tasks"; }}>
            See All &rarr;
          </a>
        )}
      </div>
      <div class="sidebar-action-list">
        {items.length === 0 ? (
          <div class="sidebar-action-empty">
            <span style={{ color: "var(--accent-green)" }}>&#10003;</span>
            <span>No items need your attention</span>
          </div>
        ) : (
          items.map(t => {
            const icon = t.status === "in_approval" ? "\uD83D\uDD00"
              : t.status === "in_review" ? "\uD83D\uDC41" : "\u26A1";
            return (
              <div
                key={t.id}
                class="sidebar-action-row"
                onClick={() => { taskPanelId.value = t.id; }}
              >
                <span class="sidebar-action-type-icon">{icon}</span>
                <span class="sidebar-task-id">{taskIdStr(t.id)}</span>
                <span class="sidebar-action-desc">{t.title}</span>
                <span class="sidebar-action-time">{fmtRelativeTime(t.updated_at)}</span>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

// ── Summary widget ──
function SummaryWidget() {
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;
  const oCount = openTaskCount.value;

  const now = new Date();
  const oneDayAgo = new Date(now - 24 * 60 * 60 * 1000);
  const doneToday = allTasks.filter(
    t => t.completed_at && new Date(t.completed_at) > oneDayAgo && t.status === "done"
  ).length;

  let totalCost = 0;
  for (const name in statsMap) totalCost += (statsMap[name] && statsMap[name].total_cost_usd) || 0;

  return (
    <div class="sidebar-widget">
      <div class="sidebar-widget-header"><span>Summary</span></div>
      <div>
        <div class="sidebar-stat-row">
          <span class="sidebar-stat-label">Done today</span>
          <span class="stat-value">{doneToday}</span>
        </div>
        <div class="sidebar-stat-row">
          <span class="sidebar-stat-label">Active tasks</span>
          <span class="stat-value">{oCount}</span>
        </div>
        <div class="sidebar-stat-row">
          <span class="sidebar-stat-label">Spent lifetime</span>
          <span class="stat-value">${totalCost.toFixed(2)}</span>
        </div>
      </div>
    </div>
  );
}

// ── Agents widget ──
function AgentsWidget() {
  const allAgents = agents.value;
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;

  const sorted = [...allAgents].sort((a, b) => {
    const aOn = a.pid ? 0 : 1;
    const bOn = b.pid ? 0 : 1;
    if (aOn !== bOn) return aOn - bOn;
    return (a.name || "").localeCompare(b.name || "");
  });

  return (
    <div class="sidebar-widget">
      <div class="sidebar-widget-header">
        <span>Agents</span>
        <a class="sidebar-see-all" onClick={() => { activeTab.value = "agents"; }}>
          See All &rarr;
        </a>
      </div>
      <div class="sidebar-agent-list">
        {sorted.map(a => {
          const dotClass = getAgentDotClass(a, allTasks, statsMap[a.name]);
          const dotTooltip = getAgentDotTooltip(dotClass, a, allTasks);
          const currentTask = allTasks.find(t => t.assignee === a.name && t.status === "in_progress");

          let taskDisplay;
          if (currentTask) {
            taskDisplay = (
              <>
                <span class="sidebar-task-id">{taskIdStr(currentTask.id)}</span>
                <span class="sidebar-agent-task-sep">&mdash;</span>{" "}
                {currentTask.title}
              </>
            );
          } else if (a.pid) {
            taskDisplay = <span class="sidebar-agent-idle">Idle</span>;
          } else {
            taskDisplay = <span class="sidebar-agent-offline">Offline</span>;
          }

          let lastActiveDisplay = "";
          if (a.pid) {
            const assignedTask = allTasks.find(t => t.assignee === a.name);
            if (assignedTask && assignedTask.updated_at) {
              lastActiveDisplay = fmtRelativeTimeShort(assignedTask.updated_at);
            }
          }

          return (
            <div
              key={a.name}
              class="sidebar-agent-row"
              onClick={() => { diffPanelMode.value = "agent"; diffPanelTarget.value = a.name; }}
            >
              <span class={"sidebar-agent-dot " + dotClass} title={dotTooltip}></span>
              <span class="sidebar-agent-name">{cap(a.name)}</span>
              <span class="sidebar-agent-activity">{taskDisplay}</span>
              {lastActiveDisplay && <span class="sidebar-agent-time">{lastActiveDisplay}</span>}
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Recent Tasks widget ──
function TasksWidget() {
  const allTasks = tasks.value;

  const eligible = allTasks.filter(t => t.status !== "rejected");
  const tier0 = eligible.filter(t => taskTier(t) === 0).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const tier1 = eligible.filter(t => taskTier(t) === 1).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const tier2 = eligible.filter(t => taskTier(t) === 2).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const tier3 = eligible.filter(t => taskTier(t) === 3).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).slice(0, 3);
  const sorted = [...tier0, ...tier1, ...tier2, ...tier3].slice(0, 7);

  return (
    <div class="sidebar-widget">
      <div class="sidebar-widget-header">
        <span>Recent Tasks</span>
        <a class="sidebar-see-all" onClick={() => { activeTab.value = "tasks"; }}>
          See All &rarr;
        </a>
      </div>
      <div class="sidebar-task-list">
        {sorted.map(t => (
          <div
            key={t.id}
            class="sidebar-task-row"
            onClick={() => { taskPanelId.value = t.id; }}
          >
            <span class={"sidebar-task-dot dot-" + t.status} title={fmtStatus(t.status)}></span>
            <span class="sidebar-task-id">{taskIdStr(t.id)}</span>
            <span class="sidebar-task-name">{t.assignee ? cap(t.assignee) : ""}</span>
            <span class="sidebar-task-title">{t.title}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Main Sidebar ──
export function Sidebar() {
  const oCount = openTaskCount.value;

  return (
    <div class="sidebar">
      <div class="sidebar-boss-bar">
        <span class={"sidebar-status-dot" + (oCount > 0 ? " active" : "")}>&#9679;</span>
        <span class="sidebar-boss-label">Boss</span>
      </div>
      <ActionWidget />
      <SummaryWidget />
      <AgentsWidget />
      <TasksWidget />
      <TeamSelector />
    </div>
  );
}
