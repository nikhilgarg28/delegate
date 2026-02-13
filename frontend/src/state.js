/**
 * Shared reactive state using @preact/signals.
 * All components read from these signals and automatically re-render on change.
 */
import { signal, computed } from "@preact/signals";

// ── Core data ──
export const currentTeam = signal("");
export const teams = signal([]);
export const bossName = signal("boss");
export const hcHome = signal("");   // absolute path to delegate home (e.g. /Users/x/.delegate)

// ── Task team filter ──
export const taskTeamFilter = signal("current"); // "current" | "all" | specific team name

// ── API data (refreshed by polling) ──
export const tasks = signal([]);
export const agents = signal([]);
export const agentStatsMap = signal({}); // { agentName: statsObj }
export const messages = signal([]);

// ── UI state ──
export const activeTab = signal("chat");
export const isMuted = signal(localStorage.getItem("delegate-muted") === "true");
export const sidebarCollapsed = signal(localStorage.getItem("delegate-sidebar-collapsed") === "true");

// ── URL-based navigation ──
// URL format: /{team}/{tab}  e.g. /self/chat, /myteam/tasks
const VALID_TABS = ["chat", "tasks", "agents"];

/** Parse the current URL and update currentTeam + activeTab signals. */
export function syncFromUrl() {
  const parts = window.location.pathname.split("/").filter(Boolean);
  if (parts.length >= 2 && VALID_TABS.includes(parts[1])) {
    currentTeam.value = parts[0];
    activeTab.value = parts[1];
  } else if (parts.length === 1) {
    // Bare /{team} or legacy /{tab}
    if (VALID_TABS.includes(parts[0])) {
      // Legacy URL like /chat — keep team, fix URL if possible
      activeTab.value = parts[0];
      if (currentTeam.value) {
        window.history.replaceState(null, "", `/${currentTeam.value}/${parts[0]}`);
      }
    } else {
      // Bare team like /self — set team, default tab
      currentTeam.value = parts[0];
      activeTab.value = "chat";
      window.history.replaceState(null, "", `/${parts[0]}/chat`);
    }
  }
}

/** Navigate to a team + tab (pushState + update signals). */
export function navigate(team, tab) {
  const t = tab || activeTab.value || "chat";
  window.history.pushState({}, "", `/${team}/${t}`);
  currentTeam.value = team;
  activeTab.value = t;
}

/** Switch tab within the current team. */
export function navigateTab(tab) {
  const team = currentTeam.value;
  if (!team) return;
  window.history.pushState({}, "", `/${team}/${tab}`);
  activeTab.value = tab;
}

// ── Panel stack ──
// Each entry: { type: "task"|"agent"|"file"|"diff", target: any }
export const panelStack = signal([]);
export const helpOverlayOpen = signal(false);  // keyboard shortcuts help overlay

/** Open a panel from the main UI — replaces the entire stack. */
export function openPanel(type, target) {
  panelStack.value = [{ type, target }];
}
/** Push a panel from inside another panel — stacks on top. */
export function pushPanel(type, target) {
  panelStack.value = [...panelStack.value, { type, target }];
}
/** Go back one level (closes if only one panel). */
export function popPanel() {
  const s = panelStack.value;
  panelStack.value = s.length > 1 ? s.slice(0, -1) : [];
}
/** Close all panels. */
export function closeAllPanels() {
  panelStack.value = [];
}

// Backward-compatible computed views of the top panel entry.
export const taskPanelId = computed(() => {
  const s = panelStack.value;
  if (!s.length) return null;
  const top = s[s.length - 1];
  return top.type === "task" ? top.target : null;
});
export const diffPanelMode = computed(() => {
  const s = panelStack.value;
  if (!s.length) return null;
  const top = s[s.length - 1];
  return top.type !== "task" ? top.type : null;
});
export const diffPanelTarget = computed(() => {
  const s = panelStack.value;
  if (!s.length) return null;
  const top = s[s.length - 1];
  return top.type !== "task" ? top.target : null;
});

// ── Known agent names (for linkify / agentify references) ──
export const knownAgentNames = signal([]);

// ── Chat filter direction ──
export const chatFilterDirection = signal("one-way"); // "one-way" | "bidi"

// ── Expanded messages (for collapsible long messages) ──
export const expandedMessages = signal(new Set());

// ── Command mode (magic commands) ──
export const commandMode = signal(false);
export const commandCwd = signal('');  // current working directory for shell commands

// ── Agent activity (live tool usage from SSE) ──
// { agentName: { tool, detail, timestamp } } — last activity per agent
export const agentLastActivity = signal({});
// Full activity stream for the activity tab in agent side panel
export const agentActivityLog = signal([]); // [{agent, tool, detail, timestamp}]
// Agent turn state: { agentName: { inTurn: boolean, taskId: number|null } }
export const agentTurnState = signal({});

// ── Manager turn context (ephemeral turn lifecycle state) ──
// {agent, task_id, sender, timestamp} or null — indicates an active turn
export const managerTurnContext = signal(null);

// ── Computed helpers ──
export const actionItems = computed(() => {
  const boss = bossName.value.toLowerCase();
  return tasks.value
    .filter(t =>
      t.assignee &&
      t.assignee.toLowerCase() === boss &&
      (t.status === "in_approval" || t.status === "merge_failed")
    )
    .sort((a, b) => (a.updated_at || "").localeCompare(b.updated_at || ""));
});

export const actionItemCount = computed(() => actionItems.value.length);

export const openTaskCount = computed(() =>
  tasks.value.filter(t =>
    t.status === "todo" || t.status === "in_progress" || t.status === "in_review"
  ).length
);

// Cross-team active agents: agents from other teams that are currently working/in-turn
export const crossTeamActiveAgents = computed(() => {
  const current = currentTeam.value;
  const turnState = agentTurnState.value;
  return agents.value.filter(a => {
    // Only include agents from other teams
    if (!a.team || a.team === current) return false;
    // Only include if they're in a turn (active)
    const turn = turnState[a.name];
    return turn?.inTurn ?? false;
  });
});

// ── Notification bell state ──
export const bellPopoverOpen = signal(false);

// ── Away summary (populated by activity catchup feature) ──
// Shape: { awayDuration, actionItems: [...], completed: [...], unreadCount } | null
export const awaySummary = signal(null);
