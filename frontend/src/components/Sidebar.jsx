import { useState, useCallback, useEffect, useRef } from "preact/hooks";
import {
  currentTeam, teams, tasks, agents, agentStatsMap,
  activeTab, taskPanelId, diffPanelMode, diffPanelTarget,
  agentLastActivity, sidebarCollapsed,
} from "../state.js";
import {
  cap, fmtStatus, fmtRelativeTimeShort,
  taskTier, taskIdStr, getAgentDotClass, getAgentDotTooltip,
} from "../utils.js";

// ── SVG Icons ──
function ChatIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 3h12a1 1 0 011 1v8a1 1 0 01-1 1H6l-3 3V4a1 1 0 011-1z" />
    </svg>
  );
}
function AgentsIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="9" cy="6" r="3" /><path d="M3 16v-1a4 4 0 014-4h4a4 4 0 014 4v1" />
    </svg>
  );
}
function TasksIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 18 18" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="12" height="12" rx="1" /><path d="M6 9l2 2 4-4" />
    </svg>
  );
}
function CollapseIcon({ collapsed }) {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      {collapsed
        ? <polyline points="6,3 11,8 6,13" />
        : <polyline points="10,3 5,8 10,13" />}
    </svg>
  );
}

const NAV_ITEMS = [
  { key: "chat", label: "Chat", Icon: ChatIcon },
  { key: "tasks", label: "Tasks", Icon: TasksIcon },
  { key: "agents", label: "Agents", Icon: AgentsIcon },
];

// ── Logo (grayscale) ──
function Logo() {
  return (
    <div class="sb-logo">
      <svg viewBox="0 0 288 92.8" width="110" height="36" aria-label="delegate">
        <g transform="translate(24,60.8) scale(0.04,-0.04)" fill="#808080">
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

// ── Team selector ──
function TeamSelector() {
  const [open, setOpen] = useState(false);
  const ref = useRef();
  const team = currentTeam.value;
  const teamList = teams.value;
  const isSingle = teamList.length <= 1;

  useEffect(() => {
    if (!open) return;
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("click", handler);
    return () => document.removeEventListener("click", handler);
  }, [open]);

  return (
    <div
      ref={ref}
      class={"sb-team" + (isSingle ? " single" : "")}
      onClick={(e) => { e.stopPropagation(); if (!isSingle) setOpen(!open); }}
    >
      <span class="sb-team-name">{team || "No team"}</span>
      {!isSingle && (
        <span class="sb-team-chevron" style={open ? { transform: "rotate(180deg)" } : {}}>&#9662;</span>
      )}
      {open && (
        <div class="sb-team-dropdown">
          {teamList.map(t => (
            <div
              key={t}
              class={"sb-team-option" + (t === team ? " active" : "")}
              onClick={(e) => { e.stopPropagation(); setOpen(false); if (t !== currentTeam.value) currentTeam.value = t; }}
            >
              {t}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ── Agent widget ──
const MAX_AGENTS = 6;
function AgentsWidget({ collapsed }) {
  const allAgents = agents.value;
  const allTasks = tasks.value;
  const statsMap = agentStatsMap.value;

  const sorted = [...allAgents].sort((a, b) => {
    const aOn = a.pid ? 0 : 1;
    const bOn = b.pid ? 0 : 1;
    if (aOn !== bOn) return aOn - bOn;
    return (a.name || "").localeCompare(b.name || "");
  }).slice(0, MAX_AGENTS);

  if (collapsed || !sorted.length) return null;

  return (
    <div class="sb-widget">
      <div class="sb-widget-header">Agents</div>
      {sorted.map(a => {
        const dotClass = getAgentDotClass(a, allTasks, statsMap[a.name]);
        const currentTask = allTasks.find(t => t.assignee === a.name && t.status === "in_progress");
        const lastAct = agentLastActivity.value[a.name];

        // Last check-in time
        let lastTime = "";
        const assignedTask = allTasks.find(t => t.assignee === a.name);
        if (assignedTask && assignedTask.updated_at) {
          lastTime = fmtRelativeTimeShort(assignedTask.updated_at);
        }

        // Second line: task + last tool
        let line2 = null;
        if (currentTask) {
          const toolStr = lastAct && lastAct.tool
            ? ` \u00b7 ${lastAct.tool.toLowerCase()}${lastAct.detail ? ": " + lastAct.detail.split("/").pop().substring(0, 24) : ""}`
            : "";
          line2 = (
            <div class="sb-agent-line2">
              <span class="sb-agent-task">{taskIdStr(currentTask.id)}</span>
              {toolStr && <span class="sb-agent-tool">{toolStr}</span>}
            </div>
          );
        }

        return (
          <div
            key={a.name}
            class="sb-agent-row"
            onClick={() => { diffPanelMode.value = "agent"; diffPanelTarget.value = a.name; }}
          >
            <div class="sb-agent-line1">
              <span class={"sb-dot " + dotClass}></span>
              <span class="sb-agent-name">{cap(a.name)}</span>
              {lastTime && <span class="sb-agent-time">{lastTime}</span>}
            </div>
            {line2}
          </div>
        );
      })}
    </div>
  );
}

// ── Task widget ──
const MAX_TASKS = 6;
function TasksWidget({ collapsed }) {
  const allTasks = tasks.value;

  const eligible = allTasks.filter(t => t.status !== "rejected" && t.status !== "cancelled");
  const tier0 = eligible.filter(t => taskTier(t) === 0).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const tier1 = eligible.filter(t => taskTier(t) === 1).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
  const tier2 = eligible.filter(t => taskTier(t) === 2).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
  const tier3 = eligible.filter(t => taskTier(t) === 3).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).slice(0, 3);
  const sorted = [...tier0, ...tier1, ...tier2, ...tier3].slice(0, MAX_TASKS);

  if (collapsed || !sorted.length) return null;

  return (
    <div class="sb-widget">
      <div class="sb-widget-header">Tasks</div>
      {sorted.map(t => {
        const lastAct = t.assignee ? agentLastActivity.value[t.assignee] : null;
        let line2 = null;
        if (t.assignee && t.status === "in_progress") {
          const toolStr = lastAct && lastAct.tool
            ? ` \u00b7 ${lastAct.tool.toLowerCase()}${lastAct.detail ? ": " + lastAct.detail.split("/").pop().substring(0, 24) : ""}`
            : "";
          line2 = (
            <div class="sb-task-line2">
              <span class="sb-task-assignee">{cap(t.assignee)}</span>
              {toolStr && <span class="sb-task-tool">{toolStr}</span>}
            </div>
          );
        }

        return (
          <div
            key={t.id}
            class="sb-task-row"
            onClick={() => { taskPanelId.value = t.id; }}
          >
            <div class="sb-task-line1">
              <span class={"sb-dot dot-" + t.status}></span>
              <span class="sb-task-id">{taskIdStr(t.id)}</span>
              <span class="sb-task-title">{t.title}</span>
              <span class="sb-task-time">{t.updated_at ? fmtRelativeTimeShort(t.updated_at) : ""}</span>
            </div>
            {line2}
          </div>
        );
      })}
    </div>
  );
}

// ── Main Sidebar ──
export function Sidebar() {
  const collapsed = sidebarCollapsed.value;
  const tab = activeTab.value;

  const toggle = useCallback(() => {
    const next = !sidebarCollapsed.value;
    sidebarCollapsed.value = next;
    localStorage.setItem("delegate-sidebar-collapsed", next ? "true" : "false");
  }, []);

  const switchTab = useCallback((key) => {
    activeTab.value = key;
    window.history.pushState({}, "", "/" + key);
  }, []);

  return (
    <div class={"sb" + (collapsed ? " sb-collapsed" : "")}>
      {/* Top: collapse toggle + logo */}
      <div class="sb-top">
        {!collapsed && <Logo />}
        <button class="sb-toggle" onClick={toggle} title={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          <CollapseIcon collapsed={collapsed} />
        </button>
      </div>

      {/* Nav */}
      <nav class="sb-nav">
        {NAV_ITEMS.map(({ key, label, Icon }) => (
          <button
            key={key}
            class={"sb-nav-btn" + (tab === key ? " active" : "")}
            onClick={() => switchTab(key)}
            title={collapsed ? label : undefined}
          >
            <Icon />
            {!collapsed && <span class="sb-nav-label">{label}</span>}
          </button>
        ))}
      </nav>

      {/* Widgets */}
      <div class="sb-widgets">
        <AgentsWidget collapsed={collapsed} />
        <TasksWidget collapsed={collapsed} />
      </div>

      {/* Team selector at bottom */}
      {!collapsed && <TeamSelector />}
    </div>
  );
}
