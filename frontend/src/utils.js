/**
 * Pure formatting & utility helpers.
 * No side effects, no DOM access, no state.
 */
import { html as diff2HtmlRender, parse as diff2HtmlParse } from "diff2html";
import { marked } from "marked";
import DOMPurify from "dompurify";

// Configure marked for GFM
marked.setOptions({ gfm: true, breaks: true });

// ── Markdown ──
export function renderMarkdown(text) {
  if (!text) return "";
  return DOMPurify.sanitize(marked.parse(text));
}

// ── Formatting ──
export function cap(s) {
  if (!s) return "";
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export function fmtStatus(s) {
  if (!s) return "";
  return s.split("_").map(w => cap(w)).join(" ");
}

export function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

export function relativeTimeParts(iso) {
  if (!iso) return null;
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  const min = Math.floor(sec / 60);
  const hr = Math.floor(min / 60);
  const days = Math.floor(hr / 24);
  return { sec, min, hr, days };
}

export function fmtTimestamp(iso) {
  if (!iso) return "\u2014";
  const t = relativeTimeParts(iso);
  if (!t) return "\u2014";
  if (t.sec < 60) return "Just now";
  if (t.min < 60) return t.min + " min ago";
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  if (t.hr < 24) return time;
  const mon = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return mon + ", " + time;
}

export function fmtRelativeTime(iso) {
  const t = relativeTimeParts(iso);
  if (!t) return "";
  if (t.sec < 60) return "Just now";
  if (t.min < 60) return t.min + "m ago";
  if (t.hr < 24) return t.hr + "h ago";
  return t.days + "d ago";
}

export function fmtRelativeTimeShort(iso) {
  const t = relativeTimeParts(iso);
  if (!t) return "";
  if (t.sec < 60) return "<1m";
  if (t.min < 60) return t.min + "m";
  if (t.hr < 24) return t.hr + "h";
  return t.days + "d";
}

export function fmtElapsed(sec) {
  if (sec == null) return "\u2014";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? m + "m " + s + "s" : s + "s";
}

export function fmtTokens(tin, tout) {
  if (tin == null && tout == null) return "\u2014";
  return Number(tin || 0).toLocaleString() + " / " + Number(tout || 0).toLocaleString();
}

export function fmtTokensShort(n) {
  if (n == null || n === 0) return "0";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
}

export function fmtCost(usd) {
  if (usd == null) return "\u2014";
  return "$" + Number(usd).toFixed(2);
}

export function fmtDuration(sec) {
  if (sec == null || sec === 0) return "\u2014";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

// ── Escaping ──
const _escDiv = typeof document !== "undefined" ? document.createElement("div") : null;
export function esc(s) {
  if (!_escDiv) return String(s || "");
  _escDiv.textContent = s;
  return _escDiv.innerHTML;
}

// ── Avatars ──
const _avatarColors = [
  "#e11d48", "#7c3aed", "#2563eb", "#0891b2",
  "#059669", "#d97706", "#dc2626", "#4f46e5",
];
export function avatarColor(name) {
  let h = 0;
  for (let i = 0; i < (name || "").length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
  return _avatarColors[Math.abs(h) % _avatarColors.length];
}
export function avatarInitial(name) {
  return (name || "?").charAt(0).toUpperCase();
}

// ── Multi-repo helpers ──
export function flattenDiffDict(diff) {
  if (!diff) return "";
  if (typeof diff === "string") return diff;
  if (typeof diff !== "object") return "";
  const keys = Object.keys(diff);
  if (keys.length === 0) return "";
  if (keys.length === 1) return diff[keys[0]] || "";
  return keys.map(repo => "# \u2500\u2500 " + repo + " \u2500\u2500\n" + (diff[repo] || "(no diff)")).join("\n\n");
}

export function flattenCommitsDict(commits) {
  if (!commits) return [];
  if (Array.isArray(commits)) return commits;
  if (typeof commits !== "object") return [];
  const all = [];
  Object.keys(commits).forEach(repo => {
    (commits[repo] || []).forEach(c => all.push(c));
  });
  return all;
}

// ── Linkify helpers (produce HTML strings for dangerouslySetInnerHTML) ──
export function linkifyTaskRefs(html) {
  return html.replace(/(^[^<]+|>[^<]*)/g, match =>
    match.replace(/(?<!\/)T(\d{4})\b/g, (full, digits) => {
      const id = parseInt(digits, 10);
      return '<span class="task-link" data-task-id="' + id + '">' + full + "</span>";
    })
  );
}

export function linkifyFilePaths(html) {
  return html.replace(/(^[^<]+|>[^<]*)/g, match =>
    match.replace(/\bshared\/[\w\-\.\/]+\.[\w]+/g, path =>
      '<span class="file-link" data-file-path="' + esc(path) + '">' + esc(path) + "</span>"
    )
  );
}

export function agentifyRefs(html, agentNames) {
  if (!agentNames || !agentNames.length) return html;
  const pattern = new RegExp(
    "\\b(" + agentNames.map(n => n.charAt(0).toUpperCase() + n.slice(1)).join("|") + ")(?!/)",
    "g"
  );
  return html.replace(/(^[^<]+|>[^<]*)/g, match =>
    match.replace(pattern, full =>
      '<span class="agent-link" data-agent-name="' + full.toLowerCase() + '">' + full + "</span>"
    )
  );
}

// ── diff2html wrappers ──
export { diff2HtmlRender, diff2HtmlParse };

// ── Task sorting ──
export function taskTier(t) {
  if (t.status === "in_approval") return 0;
  if (t.status === "in_progress" || t.status === "in_review") return 1;
  if (t.status === "todo") return 2;
  if (t.status === "done") return 3;
  return 4;
}

export function taskIdStr(id) {
  return "T" + String(id).padStart(4, "0");
}

// ── Roles ──
export const roleBadgeMap = {
  worker: "Worker", manager: "Manager", qa: "QA",
  design: "Design", backend: "Backend", frontend: "Frontend",
};

// ── Agent dot helpers ──
export function getAgentDotClass(agent, tasksList, stats) {
  if (!agent.pid) return "dot-offline";
  const assignedTask = tasksList.find(t => t.assignee === agent.name && t.status === "in_progress");
  const taskUpdated = assignedTask ? new Date(assignedTask.updated_at) : null;
  const lastActive = stats && stats.last_active ? new Date(stats.last_active) : null;
  const timestamps = [taskUpdated, lastActive].filter(Boolean);
  if (timestamps.length === 0) return "dot-active";
  const mostRecent = new Date(Math.max(...timestamps));
  const minutesAgo = (Date.now() - mostRecent.getTime()) / 60000;
  if (minutesAgo <= 5) return "dot-active";
  if (minutesAgo <= 30) return "dot-stale";
  return "dot-stuck";
}

export function getAgentDotTooltip(dotClass, agent, tasksList) {
  if (dotClass === "dot-offline") return "Offline";
  const assignedTask = tasksList.find(t => t.assignee === agent.name && t.status === "in_progress");
  const lastTs = assignedTask && assignedTask.updated_at ? assignedTask.updated_at : null;
  const timeStr = lastTs ? fmtRelativeTime(lastTs) : "";
  if (dotClass === "dot-active") return "Active" + (timeStr ? " \u2014 last activity " + timeStr : "");
  if (dotClass === "dot-stale") return "May be stuck" + (timeStr ? " \u2014 last activity " + timeStr : "");
  if (dotClass === "dot-stuck") return "Likely stuck" + (timeStr ? " \u2014 last activity " + timeStr : "");
  return "";
}

// ── Message status icon (HTML string) ──
export function msgStatusIcon(m) {
  if (m.processed_at) return '<span class="msg-status msg-processed" title="Processed">\u2713\u2713</span>';
  if (m.seen_at) return '<span class="msg-status msg-seen" title="Seen">\u2713\u2713</span>';
  if (m.delivered_at) return '<span class="msg-status msg-delivered" title="Delivered">\u2713</span>';
  return '<span class="msg-status msg-pending" title="Sending\u2026">\u23F3</span>';
}
