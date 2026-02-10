/**
 * Shared reactive state using @preact/signals.
 * All components read from these signals and automatically re-render on change.
 */
import { signal, computed } from "@preact/signals";

// ── Core data ──
export const currentTeam = signal("");
export const teams = signal([]);
export const bossName = signal("boss");

// ── API data (refreshed by polling) ──
export const tasks = signal([]);
export const agents = signal([]);
export const agentStatsMap = signal({}); // { agentName: statsObj }
export const messages = signal([]);

// ── UI state ──
export const activeTab = signal("chat");
export const isMuted = signal(localStorage.getItem("delegate-muted") === "true");

// ── Panel state ──
export const taskPanelId = signal(null);       // numeric task id or null
export const diffPanelMode = signal(null);     // "diff" | "agent" | "file" | null
export const diffPanelTarget = signal(null);   // taskId, agentName, or filePath

// ── Known agent names (for linkify / agentify references) ──
export const knownAgentNames = signal([]);

// ── Chat filter direction ──
export const chatFilterDirection = signal("one-way"); // "one-way" | "bidi"

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
