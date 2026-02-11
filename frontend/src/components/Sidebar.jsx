import { useState, useCallback, useEffect, useRef } from "preact/hooks";
import {
  currentTeam, teams, bossName, tasks, agents, agentStatsMap,
  activeTab, openTaskCount, taskPanelId, diffPanelMode, diffPanelTarget,
  agentLastActivity,
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

          // Live tool activity from SSE
          const lastAct = agentLastActivity.value[a.name];
          let activityLine = null;
          if (lastAct && lastAct.tool) {
            const toolLower = lastAct.tool.toLowerCase();
            const det = lastAct.detail ? lastAct.detail.split("/").pop().substring(0, 30) : "";
            activityLine = (
              <span class="sidebar-agent-tool" title={lastAct.detail || lastAct.tool}>
                {toolLower}{det ? ": " + det : ""}
              </span>
            );
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
              <span class="sidebar-agent-activity">
                {activityLine || taskDisplay}
              </span>
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

// ── Logo ──
function Logo() {
  return (
    <div class="sidebar-logo">
      <svg viewBox="0 0 288 92.8" width="152" height="49" aria-label="delegate">
        <g transform="translate(24,60.8) scale(0.04,-0.04)" fill="#4ADE80">
          <path d="M85 65V152L395 304Q414 313 430.5 319.5Q447 326 455 328Q446 330 429 337Q412 344 395 352L85 505V595L515 380V280Z"/>
          <path transform="translate(1200,0)" d="M268-10Q186-10 136.5 45Q87 100 87 194V355Q87 450 136 505Q185 560 268 560Q330 560 370.5 529Q411 498 419 445H420L418 570V730H508V0H418V105H417Q410 51 370 20.5Q330-10 268-10ZM298 68Q354 68 386 103Q418 138 418 200V350Q418 412 386 447Q354 482 298 482Q241 482 209 452.5Q177 423 177 355V195Q177 128 209 98Q241 68 298 68Z"/>
          <path transform="translate(1800,0)" d="M300-10Q203-10 143.5 48.5Q84 107 84 210V340Q84 443 143.5 501.5Q203 560 300 560Q365 560 413.5 534Q462 508 489 461Q516 414 516 350V252H172V200Q172 139 207 103.5Q242 68 300 68Q350 68 382.5 87.5Q415 107 422 140H512Q503 71 445 30.5Q387-10 300-10ZM172 322H428V350Q428 415 394.5 450.5Q361 486 300 486Q239 486 205.5 450.5Q172 415 172 350Z"/>
          <path transform="translate(2400,0)" d="M380 0Q307 0 263.5 42.5Q220 85 220 155V648H30V730H310V155Q310 121 329 101.5Q348 82 380 82H550V0Z"/>
          <path transform="translate(3000,0)" d="M300-10Q203-10 143.5 48.5Q84 107 84 210V340Q84 443 143.5 501.5Q203 560 300 560Q365 560 413.5 534Q462 508 489 461Q516 414 516 350V252H172V200Q172 139 207 103.5Q242 68 300 68Q350 68 382.5 87.5Q415 107 422 140H512Q503 71 445 30.5Q387-10 300-10ZM172 322H428V350Q428 415 394.5 450.5Q361 486 300 486Q239 486 205.5 450.5Q172 415 172 350Z"/>
          <path transform="translate(3600,0)" d="M161-180V-98H316Q363-98 390-71.5Q417-45 417 0V50L419 140H416Q408 91 369 64.5Q330 38 271 38Q186 38 137 92Q88 146 88 240V356Q88 450 137 505Q186 560 271 560Q330 560 369 532Q408 504 416 455H418V550H507V0Q507-83 455.5-131.5Q404-180 315-180ZM298 113Q354 113 386 148Q418 183 418 245V350Q418 412 386 447Q354 482 298 482Q241 482 209.5 449Q178 416 178 360V235Q178 179 209.5 146Q241 113 298 113Z"/>
          <path transform="translate(4200,0)" d="M252-10Q167-10 117 37.5Q67 85 67 162Q67 213 90 251Q113 289 154 310.5Q195 332 248 332H418V375Q418 482 301 482Q249 482 217 463Q185 444 183 410H93Q98 475 153.5 517.5Q209 560 301 560Q401 560 454.5 512Q508 464 508 378V0H419V100H417Q409 49 366 19.5Q323-10 252-10ZM274 66Q340 66 379 98Q418 130 418 185V262H258Q214 262 186.5 235.5Q159 209 159 165Q159 119 189.5 92.5Q220 66 274 66Z"/>
          <path transform="translate(4800,0)" d="M355 0Q287 0 246 39.5Q205 79 205 145V468H47V550H205V705H295V550H520V468H295V145Q295 117 311.5 99.5Q328 82 355 82H515V0Z"/>
          <path transform="translate(5400,0)" d="M300-10Q203-10 143.5 48.5Q84 107 84 210V340Q84 443 143.5 501.5Q203 560 300 560Q365 560 413.5 534Q462 508 489 461Q516 414 516 350V252H172V200Q172 139 207 103.5Q242 68 300 68Q350 68 382.5 87.5Q415 107 422 140H512Q503 71 445 30.5Q387-10 300-10ZM172 322H428V350Q428 415 394.5 450.5Q361 486 300 486Q239 486 205.5 450.5Q172 415 172 350Z"/>
        </g>
      </svg>
    </div>
  );
}

// ── Main Sidebar ──
export function Sidebar() {
  const oCount = openTaskCount.value;

  return (
    <div class="sidebar">
      <Logo />
      <div class="sidebar-boss-bar">
        <span class={"sidebar-status-dot" + (oCount > 0 ? " active" : "")}>&#9679;</span>
        <span class="sidebar-boss-label">Boss</span>
      </div>
      <SummaryWidget />
      <AgentsWidget />
      <TasksWidget />
      <TeamSelector />
    </div>
  );
}
