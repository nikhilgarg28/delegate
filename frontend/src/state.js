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

// ── API data (refreshed by polling) ──
export const tasks = signal([]);
export const agents = signal([]);
export const agentStatsMap = signal({}); // { agentName: statsObj }
export const messages = signal([]);

// ── UI state ──
export const activeTab = signal("chat");
export const isMuted = signal(localStorage.getItem("delegate-muted") === "true");
export const sidebarCollapsed = signal(localStorage.getItem("delegate-sidebar-collapsed") === "true");

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

// ── Agent activity (live tool usage from SSE) ──
// { agentName: { tool, detail, timestamp } } — last activity per agent
export const agentLastActivity = signal({});
// Full activity stream for the activity tab in agent side panel
export const agentActivityLog = signal([]); // [{agent, tool, detail, timestamp}]

// ── Manager turn context (ephemeral turn lifecycle state) ──
// {agent, task_id, sender, timestamp} or null — indicates an active turn
export const managerTurnContext = signal(null);

// ── Computed helpers ──
export const actionItems = computed(() => {
  const boss = bossName.value.toLowerCase();
  return tasks.value
    .filter(t => t.assignee && t.assignee.toLowerCase() === boss && t.status !== "done")
    .sort((a, b) => (a.updated_at || "").localeCompare(b.updated_at || ""));
});

export const openTaskCount = computed(() =>
  tasks.value.filter(t =>
    t.status === "todo" || t.status === "in_progress" || t.status === "in_review"
  ).length
);
