import { html as diff2HtmlRender, parse as diff2HtmlParse } from "diff2html";
import { marked } from "marked";
import DOMPurify from "dompurify";

// Configure marked for GitHub Flavored Markdown
marked.setOptions({
  gfm: true,
  breaks: true,
});

/**
 * Render markdown text to sanitized HTML.
 * Pipeline: markdown text -> marked.parse() -> DOMPurify.sanitize() -> safe HTML
 */
function renderMarkdown(text) {
  if (!text) return "";
  const rawHtml = marked.parse(text);
  return DOMPurify.sanitize(rawHtml);
}

// =====================================================================
// State
// =====================================================================
let _currentTeam = "";
let _teams = [];
let _isMuted = localStorage.getItem("boss-muted") === "true";
let _audioCtx = null;
let _lastMsgTimestamp = "";
let _prevTaskStatuses = {};
let _msgSendCooldown = false;
let _rejectReasonVisible = false;

// Panel state
let _panelMode = null;
let _panelAgent = null;
let _panelTask = null;
let _agentTabData = {};
let _agentCurrentTab = "inbox";
let _diffRawText = "";
let _diffCurrentTab = "files";
let _taskPanelDiffTab = "files";
let _taskPanelDiffRaw = "";

// Voice-to-text state
let _recognition = null;
let _micActive = false;
let _micStopping = false;
let _micBaseText = "";
let _micFinalText = "";

// =====================================================================
// Team selector
// =====================================================================
async function loadTeams() {
  try {
    const res = await fetch("/teams");
    if (!res.ok) return;
    _teams = await res.json();
    const sel = document.getElementById("teamSelector");
    const prev = _currentTeam;
    sel.innerHTML = _teams
      .map((t) => `<option value="${t}">${t}</option>`)
      .join("");
    if (prev && _teams.includes(prev)) {
      sel.value = prev;
    } else if (_teams.length > 0) {
      sel.value = _teams[0];
    }
    _currentTeam = sel.value;
  } catch (e) {
    console.warn("loadTeams failed:", e);
  }
}

function onTeamChange() {
  _currentTeam = document.getElementById("teamSelector").value;
  loadChat();
  loadAgents();
  loadSidebar();
}

// =====================================================================
// Audio / mute
// =====================================================================
function toggleMute() {
  _isMuted = !_isMuted;
  localStorage.setItem("boss-muted", _isMuted ? "true" : "false");
  _updateMuteBtn();
}

function _updateMuteBtn() {
  const btn = document.getElementById("muteToggle");
  if (!btn) return;
  if (_isMuted) {
    btn.innerHTML =
      "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><polygon points='2,6 2,10 5,10 9,13 9,3 5,6'/><line x1='12' y1='5' x2='15' y2='11'/><line x1='15' y1='5' x2='12' y2='11'/></svg>";
    btn.title = "Unmute notifications";
  } else {
    btn.innerHTML =
      "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><polygon points='2,6 2,10 5,10 9,13 9,3 5,6'/><path d='M11.5 5.5a3.5 3.5 0 0 1 0 5'/></svg>";
    btn.title = "Mute notifications";
  }
}

function _getAudioCtx() {
  if (!_audioCtx) {
    try {
      _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (e) {
      return null;
    }
  }
  return _audioCtx;
}

function playMsgSound() {
  if (_isMuted) return;
  const ctx = _getAudioCtx();
  if (!ctx) return;
  const now = ctx.currentTime;
  const g = ctx.createGain();
  g.connect(ctx.destination);
  g.gain.setValueAtTime(0.15, now);
  g.gain.exponentialRampToValueAtTime(0.001, now + 0.25);
  const o1 = ctx.createOscillator();
  o1.type = "sine";
  o1.frequency.value = 800;
  o1.connect(g);
  o1.start(now);
  o1.stop(now + 0.08);
  const o2 = ctx.createOscillator();
  o2.type = "sine";
  o2.frequency.value = 1000;
  o2.connect(g);
  o2.start(now + 0.1);
  o2.stop(now + 0.18);
}

function playTaskSound() {
  if (_isMuted) return;
  const ctx = _getAudioCtx();
  if (!ctx) return;
  const now = ctx.currentTime;
  [523.25, 659.25, 783.99].forEach((freq, i) => {
    const t = now + i * 0.15;
    const g = ctx.createGain();
    g.connect(ctx.destination);
    g.gain.setValueAtTime(0.12, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.15);
    const o = ctx.createOscillator();
    o.type = "sine";
    o.frequency.value = freq;
    o.connect(g);
    o.start(t);
    o.stop(t + 0.15);
  });
}

// =====================================================================
// Theme toggle (light/dark/system)
// =====================================================================
function initTheme() {
  const pref = localStorage.getItem("boss-theme"); // "light", "dark", or null (system)
  applyTheme(pref);
}

function cycleTheme() {
  const current = localStorage.getItem("boss-theme");
  let next;
  if (current === null) next = "light";
  else if (current === "light") next = "dark";
  else next = null; // back to system
  if (next) localStorage.setItem("boss-theme", next);
  else localStorage.removeItem("boss-theme");
  applyTheme(next);
}

function applyTheme(pref) {
  const root = document.documentElement;
  root.classList.remove("light", "dark");
  if (pref === "light") root.classList.add("light");
  else if (pref === "dark") root.classList.add("dark");
  // else: no class = system preference via media query
  updateThemeIcon(pref);
}

function updateThemeIcon(pref) {
  const btn = document.getElementById("themeToggle");
  if (!btn) return;
  if (pref === "light") {
    btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><circle cx='8' cy='8' r='3'/><line x1='8' y1='1' x2='8' y2='3'/><line x1='8' y1='13' x2='8' y2='15'/><line x1='1' y1='8' x2='3' y2='8'/><line x1='13' y1='8' x2='15' y2='8'/><line x1='3.05' y1='3.05' x2='4.46' y2='4.46'/><line x1='11.54' y1='11.54' x2='12.95' y2='12.95'/><line x1='3.05' y1='12.95' x2='4.46' y2='11.54'/><line x1='11.54' y1='4.46' x2='12.95' y2='3.05'/></svg>";
    btn.title = "Theme: Light (click to switch)";
  } else if (pref === "dark") {
    btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><path d='M14 8.5A6 6 0 0 1 7.5 2 6 6 0 1 0 14 8.5z'/></svg>";
    btn.title = "Theme: Dark (click to switch)";
  } else {
    btn.innerHTML = "<svg width='16' height='16' viewBox='0 0 16 16' fill='none' stroke='currentColor' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'><rect x='2' y='3' width='12' height='9' rx='1'/><line x1='5' y1='14' x2='11' y2='14'/><line x1='8' y1='12' x2='8' y2='14'/></svg>";
    btn.title = "Theme: System (click to switch)";
  }
}

// =====================================================================
// Helpers
// =====================================================================
function cap(s) {
  return s.charAt(0).toUpperCase() + s.slice(1);
}
function fmtStatus(s) {
  return s
    .split("_")
    .map((w) => cap(w))
    .join(" ");
}
function fmtTime(iso) {
  const d = new Date(iso);
  return d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}
function fmtTimestamp(iso) {
  if (!iso) return "\u2014";
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  const sec = Math.floor(diff / 1000);
  const min = Math.floor(sec / 60);
  const hr = Math.floor(min / 60);
  if (sec < 60) return "Just now";
  if (min < 60) return min + " min ago";
  const time = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  if (hr < 24) return time;
  const mon = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return mon + ", " + time;
}
function esc(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}
const _avatarColors = [
  "#e11d48",
  "#7c3aed",
  "#2563eb",
  "#0891b2",
  "#059669",
  "#d97706",
  "#dc2626",
  "#4f46e5",
];
function avatarColor(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = name.charCodeAt(i) + ((h << 5) - h);
  return _avatarColors[Math.abs(h) % _avatarColors.length];
}
function avatarInitial(name) {
  return name.charAt(0).toUpperCase();
}
function fmtElapsed(sec) {
  if (sec == null) return "\u2014";
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return m > 0 ? m + "m " + s + "s" : s + "s";
}
function fmtTokens(tin, tout) {
  if (tin == null && tout == null) return "\u2014";
  return (
    Number(tin || 0).toLocaleString() +
    " / " +
    Number(tout || 0).toLocaleString()
  );
}
function fmtCost(usd) {
  if (usd == null) return "\u2014";
  return "$" + Number(usd).toFixed(2);
}

// =====================================================================
// Tabs
// =====================================================================
function switchTab(name, pushHash) {
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
  document.getElementById(name).classList.add("active");
  document
    .querySelector('.tab[data-tab="' + name + '"]')
    .classList.add("active");
  if (pushHash !== false) window.location.hash = name;
  if (name === "tasks") loadTasks();
  if (name === "chat") loadChat();
  if (name === "agents") loadAgents();
}

// =====================================================================
// Tasks
// =====================================================================
function _taskRowHtml(t) {
  const tid = "T" + String(t.id).padStart(4, "0");
  return `<div class="task-row" data-id="${t.id}" onclick="openTaskPanel(${t.id})">
    <div class="task-summary">
      <span class="task-id">${tid}</span>
      <span class="task-title">${esc(t.title)}</span>
      <span><span class="badge badge-${t.status}">${fmtStatus(t.status)}</span></span>
      <span class="task-assignee">${t.assignee ? cap(t.assignee) : "\u2014"}</span>
      <span class="task-priority">${cap(t.priority)}</span>
    </div>
  </div>`;
}

function renderTaskApproval(task) {
  const status = task.status || "";
  const approvalStatus = task.approval_status || "";
  if (status === "merged" || approvalStatus === "approved") {
    return '<div class="task-inspector-approval"><div class="approval-badge approval-badge-approved">\u2714 Approved</div></div>';
  }
  if (status === "rejected" || approvalStatus === "rejected") {
    const reason = task.rejection_reason || "";
    return (
      '<div class="task-inspector-approval"><div class="approval-badge approval-badge-rejected">\u2716 Rejected</div>' +
      (reason
        ? '<div class="approval-rejection-reason">' + esc(reason) + "</div>"
        : "") +
      "</div>"
    );
  }
  if (status === "needs_merge") {
    let html = '<div class="task-inspector-approval">';
    html += '<div class="task-inspector-approval-actions">';
    html +=
      '<button class="btn-approve" onclick="event.stopPropagation();approveTask(' +
      task.id +
      ')">Approve Merge</button>';
    html +=
      '<button class="btn-reject" onclick="event.stopPropagation();toggleRejectReason(' +
      task.id +
      ')">Reject</button>';
    html += "</div>";
    if (_rejectReasonVisible) {
      html +=
        '<div class="reject-reason-row">' +
        '<input type="text" class="reject-reason-input" id="rejectReasonInput" placeholder="Reason for rejection..." onclick="event.stopPropagation()" onkeydown="event.stopPropagation();if(event.key===\'Enter\')rejectTask(' +
        task.id +
        ')">' +
        '<button class="btn-reject" onclick="event.stopPropagation();rejectTask(' +
        task.id +
        ')" style="flex-shrink:0">Confirm</button></div>';
    }
    html += "</div>";
    return html;
  }
  if (status === "conflict") {
    return '<div class="task-inspector-approval"><div class="approval-badge" style="background:rgba(251,146,60,0.12);color:#fb923c">\u26A0 Conflict</div></div>';
  }
  return "";
}

function toggleRejectReason(taskId) {
  _rejectReasonVisible = !_rejectReasonVisible;
  loadTasks();
  if (_rejectReasonVisible) {
    setTimeout(() => {
      const el = document.getElementById("rejectReasonInput");
      if (el) el.focus();
    }, 50);
  }
}

async function approveTask(taskId) {
  try {
    const res = await fetch("/tasks/" + taskId + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (res.ok) {
      _rejectReasonVisible = false;
      loadTasks();
      loadSidebar();
    } else {
      const err = await res.json().catch(() => ({}));
      alert("Failed to approve: " + (err.detail || res.statusText));
    }
  } catch (e) {
    alert("Failed to approve task: " + e.message);
  }
}

async function rejectTask(taskId) {
  const reasonEl = document.getElementById("rejectReasonInput");
  const reason = reasonEl ? reasonEl.value.trim() : "";
  try {
    const res = await fetch("/tasks/" + taskId + "/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || "(no reason)" }),
    });
    if (res.ok) {
      _rejectReasonVisible = false;
      loadTasks();
      loadSidebar();
    } else {
      const err = await res.json().catch(() => ({}));
      alert("Failed to reject: " + (err.detail || res.statusText));
    }
  } catch (e) {
    alert("Failed to reject task: " + e.message);
  }
}

async function loadTasks() {
  let res;
  try {
    res = await fetch("/tasks");
  } catch (e) {
    console.warn("loadTasks fetch failed:", e);
    return;
  }
  if (!res.ok) return;
  const allTasks = await res.json();
  const el = document.getElementById("taskTable");
  let taskSoundNeeded = false;
  for (const t of allTasks) {
    const prev = _prevTaskStatuses[t.id];
    if (
      prev &&
      prev !== t.status &&
      (t.status === "done" || t.status === "review")
    )
      taskSoundNeeded = true;
    _prevTaskStatuses[t.id] = t.status;
  }
  if (taskSoundNeeded) playTaskSound();
  if (!allTasks.length) {
    el.innerHTML =
      '<p style="color:var(--text-secondary)">No tasks yet.</p>';
    return;
  }
  const assignees = new Set();
  for (const t of allTasks) {
    if (t.assignee) assignees.add(t.assignee);
  }
  const assigneeSel = document.getElementById("taskFilterAssignee");
  const prevAssignee = assigneeSel.value;
  assigneeSel.innerHTML =
    '<option value="">All</option>' +
    [...assignees]
      .sort()
      .map((n) => `<option value="${n}">${cap(n)}</option>`)
      .join("");
  assigneeSel.value = prevAssignee;
  const filterStatus = document.getElementById("taskFilterStatus").value;
  const filterPriority = document.getElementById("taskFilterPriority").value;
  const filterAssignee = document.getElementById("taskFilterAssignee").value;
  let tasks = allTasks;
  if (filterStatus) tasks = tasks.filter((t) => t.status === filterStatus);
  if (filterPriority)
    tasks = tasks.filter((t) => t.priority === filterPriority);
  if (filterAssignee)
    tasks = tasks.filter((t) => t.assignee === filterAssignee);
  tasks.sort((a, b) => b.id - a.id);
  if (!tasks.length) {
    el.innerHTML =
      '<p style="color:var(--text-secondary)">No tasks match filters.</p>';
    return;
  }
  el.innerHTML =
    '<div class="task-list">' +
    tasks.map((t) => _taskRowHtml(t)).join("") +
    "</div>";
}

function toggleTask(id) {
  // Legacy — now opens the side panel
  openTaskPanel(id);
}

// =====================================================================
// Task detail side panel
// =====================================================================
/**
 * Post-process HTML to make TXXX patterns clickable.
 * Matches T followed by 4 digits (e.g. T0017) that are NOT already inside
 * an HTML tag or attribute.
 */
function linkifyTaskRefs(html) {
  // Replace TXXX in text nodes only (between > and <, or at start/end of string)
  // Split into segments: HTML tags vs text content
  return html.replace(/(^[^<]+|>[^<]*)/g, function (match) {
    return match.replace(/\bT(\d{4})\b/g, function (full, digits) {
      const id = parseInt(digits, 10);
      return '<span class="task-link" data-task-id="' + id + '" onclick="event.stopPropagation();openTaskPanel(' + id + ')">' + full + '</span>';
    });
  });
}

function switchTaskPanelDiffTab(tab) {
  _taskPanelDiffTab = tab;
  const container = document.getElementById("taskPanelBody");
  if (!container) return;
  container.querySelectorAll(".task-panel-diff-tabs .diff-tab").forEach(
    (t) => t.classList.toggle("active", t.dataset.dtab === tab)
  );
  const diffContent = document.getElementById("taskPanelDiffContent");
  if (!diffContent) return;
  if (tab === "files") {
    const files = diff2HtmlParse(_taskPanelDiffRaw);
    if (!files.length) {
      diffContent.innerHTML = '<div class="diff-empty">No files changed</div>';
      return;
    }
    let totalAdd = 0, totalDel = 0;
    for (const f of files) { totalAdd += f.addedLines; totalDel += f.deletedLines; }
    let h = '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">' +
      files.length + ' file' + (files.length !== 1 ? 's' : '') + ' changed, ' +
      '<span style="color:var(--diff-add-text)">+' + totalAdd + '</span> ' +
      '<span style="color:var(--diff-del-text)">\u2212' + totalDel + '</span></div>';
    h += '<div class="diff-file-list">';
    for (const f of files) {
      const name = (f.newName === '/dev/null' ? f.oldName : f.newName) || f.oldName || "unknown";
      h += '<div class="diff-file-list-item" onclick="switchTaskPanelDiffTab(\'diff\')"><span class="diff-file-list-name">' +
        esc(name) + '</span><span class="diff-file-stats"><span class="diff-file-add">+' +
        f.addedLines + '</span><span class="diff-file-del">-' +
        f.deletedLines + '</span></span></div>';
    }
    diffContent.innerHTML = h + '</div>';
  } else {
    if (!_taskPanelDiffRaw) {
      diffContent.innerHTML = '<div class="diff-empty">No changes</div>';
      return;
    }
    diffContent.innerHTML = diff2HtmlRender(_taskPanelDiffRaw, {
      outputFormat: "line-by-line",
      drawFileList: false,
      matching: "lines",
    });
  }
}

async function openTaskPanel(taskId) {
  // Close agent/diff panel if open (don't stack panels)
  if (_panelMode) closePanel();
  _panelTask = taskId;
  const panel = document.getElementById("taskPanel");
  const backdrop = document.getElementById("taskBackdrop");
  // Set loading state
  document.getElementById("taskPanelId").textContent = "T" + String(taskId).padStart(4, "0");
  document.getElementById("taskPanelTitle").textContent = "Loading...";
  document.getElementById("taskPanelStatus").innerHTML = "";
  document.getElementById("taskPanelAssignee").textContent = "";
  document.getElementById("taskPanelPriority").textContent = "";
  document.getElementById("taskPanelBody").innerHTML = '<div class="diff-empty">Loading...</div>';
  panel.classList.add("open");
  backdrop.classList.add("open");
  try {
    // Fetch task list (we need the full task object)
    const tasksRes = await fetch("/tasks");
    const allTasks = await tasksRes.json();
    const task = allTasks.find((t) => t.id === taskId);
    if (!task) {
      document.getElementById("taskPanelBody").innerHTML = '<div class="diff-empty">Task not found</div>';
      return;
    }
    // Fetch stats
    let stats = null;
    try {
      const sRes = await fetch("/tasks/" + taskId + "/stats");
      if (sRes.ok) stats = await sRes.json();
    } catch (e) {}
    // Populate header
    document.getElementById("taskPanelTitle").textContent = task.title;
    document.getElementById("taskPanelStatus").innerHTML =
      '<span class="badge badge-' + task.status + '">' + fmtStatus(task.status) + '</span>';
    document.getElementById("taskPanelAssignee").textContent =
      task.assignee ? cap(task.assignee) : "";
    document.getElementById("taskPanelPriority").textContent =
      task.priority ? cap(task.priority) : "";
    // Build body
    let body = "";
    // Metadata grid
    body += '<div class="task-panel-meta-grid">';
    body += '<div class="task-panel-meta-item"><div class="task-detail-label">Assignee</div><div class="task-detail-value">' + (task.assignee ? cap(task.assignee) : "\u2014") + '</div></div>';
    body += '<div class="task-panel-meta-item"><div class="task-detail-label">Reviewer</div><div class="task-detail-value">' + (task.reviewer ? cap(task.reviewer) : "\u2014") + '</div></div>';
    body += '<div class="task-panel-meta-item"><div class="task-detail-label">Priority</div><div class="task-detail-value">' + cap(task.priority) + '</div></div>';
    body += '<div class="task-panel-meta-item"><div class="task-detail-label">Time</div><div class="task-detail-value">' + (stats ? fmtElapsed(stats.elapsed_seconds) : "\u2014") + '</div></div>';
    body += '</div>';
    // Stats row
    if (stats) {
      body += '<div class="task-panel-meta-grid">';
      body += '<div class="task-panel-meta-item"><div class="task-detail-label">Tokens (in/out)</div><div class="task-detail-value">' + fmtTokens(stats.total_tokens_in, stats.total_tokens_out) + '</div></div>';
      body += '<div class="task-panel-meta-item"><div class="task-detail-label">Cost</div><div class="task-detail-value">' + fmtCost(stats.total_cost_usd) + '</div></div>';
      body += '</div>';
    }
    // Dates
    body += '<div class="task-panel-dates">';
    body += '<span>Created: <span class="ts" data-ts="' + (task.created_at || "") + '">' + fmtTimestamp(task.created_at) + '</span></span>';
    body += '<span>Updated: <span class="ts" data-ts="' + (task.updated_at || "") + '">' + fmtTimestamp(task.updated_at) + '</span></span>';
    if (task.completed_at) {
      body += '<span>Completed: <span class="ts" data-ts="' + task.completed_at + '">' + fmtTimestamp(task.completed_at) + '</span></span>';
    }
    body += '</div>';
    // VCS info
    if (stats && stats.branch) {
      body += '<div class="task-panel-vcs-row">';
      body += '<span class="task-branch" title="' + esc(stats.branch) + '">' + esc(stats.branch) + '</span>';
      if (stats.commits && stats.commits.length) {
        stats.commits.forEach(function (c) {
          body += '<span class="diff-panel-commit">' + esc(String(c).substring(0, 7)) + '</span>';
        });
      }
      body += '</div>';
    }
    // Base SHA
    if (task.base_sha) {
      body += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">Base SHA: <code style="font-family:SF Mono,Fira Code,monospace;background:var(--bg-active);padding:2px 6px;border-radius:3px">' + esc(task.base_sha.substring(0, 10)) + '</code></div>';
    }
    // Dependencies
    if (task.depends_on && task.depends_on.length) {
      body += '<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Depends on: ';
      task.depends_on.forEach(function (d) {
        const depStatus = (task._dep_statuses && task._dep_statuses[d]) || "open";
        body += '<span class="task-link" data-task-id="' + d + '" onclick="event.stopPropagation();openTaskPanel(' + d + ')"><span class="badge badge-' + depStatus + '" style="font-size:11px;margin-right:4px;cursor:pointer">T' + String(d).padStart(4, "0") + '</span></span>';
      });
      body += '</div>';
    }
    // Description
    if (task.description) {
      body += '<div class="task-panel-section"><div class="task-panel-section-label">Description</div>';
      body += '<div class="task-panel-desc md-content">' + linkifyTaskRefs(renderMarkdown(task.description)) + '</div>';
      body += '</div>';
    }
    // Approval actions
    body += renderTaskApproval(task);
    // Diff section placeholder
    body += '<div class="task-panel-diff-section" id="taskPanelDiffSection">';
    body += '<div class="task-panel-section-label">Changes</div>';
    body += '<div class="task-panel-diff-tabs"><button class="diff-tab active" data-dtab="files" onclick="switchTaskPanelDiffTab(\'files\')">Files Changed</button><button class="diff-tab" data-dtab="diff" onclick="switchTaskPanelDiffTab(\'diff\')">Full Diff</button></div>';
    body += '<div id="taskPanelDiffContent"><div class="diff-empty">Loading diff...</div></div>';
    body += '</div>';
    document.getElementById("taskPanelBody").innerHTML = body;
    // Load diff asynchronously
    _taskPanelDiffRaw = "";
    _taskPanelDiffTab = "files";
    try {
      const diffRes = await fetch("/tasks/" + taskId + "/diff");
      const diffData = await diffRes.json();
      _taskPanelDiffRaw = diffData.diff || "";
      switchTaskPanelDiffTab("files");
    } catch (e) {
      const dc = document.getElementById("taskPanelDiffContent");
      if (dc) dc.innerHTML = '<div class="diff-empty">Failed to load diff</div>';
    }
  } catch (e) {
    document.getElementById("taskPanelBody").innerHTML =
      '<div class="diff-empty">Failed to load task</div>';
  }
}

function closeTaskPanel() {
  document.getElementById("taskPanel").classList.remove("open");
  document.getElementById("taskBackdrop").classList.remove("open");
  _panelTask = null;
  _taskPanelDiffRaw = "";
}

// =====================================================================
// Chat
// =====================================================================
async function loadChat() {
  try {
    await _loadChatInner();
  } catch (e) {
    console.warn("loadChat failed:", e);
  }
}

async function _loadChatInner() {
  const showEvents = document.getElementById("chatShowEvents").checked;
  const filterFrom = document.getElementById("chatFilterFrom").value;
  const filterTo = document.getElementById("chatFilterTo").value;
  const params = new URLSearchParams();
  if (!showEvents) params.set("type", "chat");
  const res = await fetch(
    "/messages" + (params.toString() ? "?" + params : "")
  );
  if (!res.ok) return;
  let msgs = await res.json();
  const senders = new Set();
  const recipients = new Set();
  for (const m of msgs) {
    if (m.type === "chat") {
      senders.add(m.sender);
      recipients.add(m.recipient);
    }
  }
  const fromSel = document.getElementById("chatFilterFrom");
  const toSel = document.getElementById("chatFilterTo");
  const prevFrom = fromSel.value;
  const prevTo = toSel.value;
  if (fromSel.options.length <= 1 || toSel.options.length <= 1) {
    fromSel.innerHTML =
      '<option value="">Anyone</option>' +
      [...senders]
        .sort()
        .map((n) => `<option value="${n}">${cap(n)}</option>`)
        .join("");
    toSel.innerHTML =
      '<option value="">Anyone</option>' +
      [...recipients]
        .sort()
        .map((n) => `<option value="${n}">${cap(n)}</option>`)
        .join("");
  }
  fromSel.value = prevFrom;
  toSel.value = prevTo;
  const between = document.getElementById("chatBetween").checked;
  if (filterFrom || filterTo) {
    msgs = msgs.filter((m) => {
      if (m.type === "event") return true;
      if (between && filterFrom && filterTo)
        return (
          (m.sender === filterFrom && m.recipient === filterTo) ||
          (m.sender === filterTo && m.recipient === filterFrom)
        );
      if (filterFrom && m.sender !== filterFrom) return false;
      if (filterTo && m.recipient !== filterTo) return false;
      return true;
    });
  }
  const chatMsgs = msgs.filter((m) => m.type === "chat");
  if (chatMsgs.length > 0) {
    const newestTs = chatMsgs[chatMsgs.length - 1].timestamp || "";
    if (_lastMsgTimestamp && newestTs > _lastMsgTimestamp && !_msgSendCooldown)
      playMsgSound();
    _lastMsgTimestamp = newestTs;
  }
  const log = document.getElementById("chatLog");
  const wasNearBottom =
    log.scrollHeight - log.scrollTop - log.clientHeight < 60;
  log.innerHTML = msgs
    .map((m) => {
      if (m.type === "event")
        return `<div class="msg-event"><span class="msg-event-line"></span><span class="msg-event-text">${linkifyTaskRefs(esc(m.content))}</span><span class="msg-event-line"></span><span class="msg-event-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div>`;
      const c = avatarColor(m.sender);
      return `<div class="msg"><div class="msg-avatar" style="background:${c}">${avatarInitial(m.sender)}</div><div class="msg-body"><div class="msg-header"><span class="msg-sender" style="cursor:pointer" onclick="openAgentPanel('${m.sender}')">${cap(m.sender)}</span><span class="msg-recipient">\u2192 ${cap(m.recipient)}</span><span class="msg-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div><div class="msg-content md-content">${linkifyTaskRefs(renderMarkdown(m.content))}</div></div></div>`;
    })
    .join("");
  if (wasNearBottom) log.scrollTop = log.scrollHeight;

  // Populate recipient dropdown — only team managers
  if (!_currentTeam) return;
  const agentsRes = await fetch("/teams/" + _currentTeam + "/agents");
  const agents = await agentsRes.json();
  const sel = document.getElementById("recipient");
  const prev = sel.value;
  const managers = agents.filter((a) => a.role === "manager");
  sel.innerHTML = managers
    .map(
      (a) =>
        `<option value="${a.name}">${cap(a.name)} (${_currentTeam})</option>`
    )
    .join("");
  if (!sel.innerHTML)
    sel.innerHTML = agents
      .map((a) => `<option value="${a.name}">${cap(a.name)}</option>`)
      .join("");
  if (prev) sel.value = prev;
  else if (managers.length) sel.value = managers[0].name;
}

// =====================================================================
// Agents
// =====================================================================
async function loadAgents() {
  if (!_currentTeam) return;
  let res;
  try {
    res = await fetch("/teams/" + _currentTeam + "/agents");
  } catch (e) {
    return;
  }
  if (!res.ok) return;
  const agents = await res.json();
  const el = document.getElementById("agents");
  el.innerHTML = agents
    .map(
      (a) => `<div class="agent-card" data-name="${a.name}" onclick="openAgentPanel('${a.name}')">
    <span class="dot ${a.pid ? "dot-active" : "dot-idle"}"></span>
    <span class="agent-name">${cap(a.name)}</span>
    <span class="agent-status">${a.pid ? "Running (PID " + a.pid + ")" : "Idle"} \u00b7 ${a.unread_inbox} unread</span>
  </div>`
    )
    .join("");
}

// =====================================================================
// Sidebar
// =====================================================================
async function loadSidebar() {
  try {
    const [tasksRes, agentsRes] = await Promise.all([
      fetch("/tasks"),
      _currentTeam
        ? fetch("/teams/" + _currentTeam + "/agents")
        : Promise.resolve({ json: () => [] }),
    ]);
    const tasks = await tasksRes.json();
    const agents =
      typeof agentsRes.json === "function"
        ? await agentsRes.json()
        : agentsRes;
    const statsMap = {};
    await Promise.all(
      (agents || []).map(async (a) => {
        try {
          const r = await fetch(
            "/teams/" + _currentTeam + "/agents/" + a.name + "/stats"
          );
          if (r.ok) statsMap[a.name] = await r.json();
        } catch (e) {}
      })
    );
    const now = new Date();
    const oneDayAgo = new Date(now - 24 * 60 * 60 * 1000);
    const doneToday = tasks.filter(
      (t) =>
        t.completed_at &&
        new Date(t.completed_at) > oneDayAgo &&
        t.status === "done"
    ).length;
    const openCount = tasks.filter(
      (t) =>
        t.status === "open" ||
        t.status === "in_progress" ||
        t.status === "review"
    ).length;
    let totalCost = 0;
    for (const name in statsMap)
      totalCost += statsMap[name].total_cost_usd || 0;
    // Update status dot: green+glow when active tasks, gray otherwise
    const statusDot = document.getElementById("sidebarStatusDot");
    if (statusDot) {
      if (openCount > 0) statusDot.classList.add("active");
      else statusDot.classList.remove("active");
    }
    // Render 3-column stat card grid
    document.getElementById("sidebarStatusContent").innerHTML =
      '<div class="sidebar-stat-grid">' +
      '<div class="sidebar-stat-card"><div class="sidebar-stat-number">' + doneToday + '</div><div class="sidebar-stat-label">Done today</div></div>' +
      '<div class="sidebar-stat-card"><div class="sidebar-stat-number">' + openCount + '</div><div class="sidebar-stat-label">Active tasks</div></div>' +
      '<div class="sidebar-stat-card"><div class="sidebar-stat-number">$' + totalCost.toFixed(2) + '</div><div class="sidebar-stat-label">Spent lifetime</div></div>' +
      '</div>';
    const inProgressTasks = tasks.filter((t) => t.status === "in_progress");
    let agentHtml = "";
    for (const a of agents || []) {
      let dotClass = "dot-offline";
      let activity = "Idle";
      if (a.pid) {
        dotClass = "dot-working";
        const agentTask = inProgressTasks.find(
          (t) => t.assignee === a.name
        );
        activity = agentTask
          ? "T" + String(agentTask.id).padStart(4, "0") + " " + agentTask.title
          : "Working...";
      } else if (a.unread_inbox > 0) dotClass = "dot-queued";
      const cost = statsMap[a.name]
        ? "$" + Number(statsMap[a.name].total_cost_usd || 0).toFixed(2)
        : "";
      agentHtml +=
        '<div class="sidebar-agent-row" style="cursor:pointer" onclick="openAgentPanel(\'' +
        a.name +
        "')\">" +
        '<span class="sidebar-agent-dot ' +
        dotClass +
        '"></span>' +
        '<span class="sidebar-agent-name">' +
        cap(a.name) +
        "</span>" +
        '<span class="sidebar-agent-activity">' +
        esc(activity) +
        "</span>" +
        '<span class="sidebar-agent-cost">' +
        cost +
        "</span></div>";
    }
    document.getElementById("sidebarAgentList").innerHTML = agentHtml;
    // Task heuristic: Tier 0 needs_merge, Tier 1 in_progress+review, Tier 2 open, Tier 3 merged+done (max 3). Never show rejected.
    function taskTier(t) {
      if (t.status === "needs_merge") return 0;
      if (t.status === "in_progress" || t.status === "review") return 1;
      if (t.status === "open") return 2;
      if (t.status === "merged" || t.status === "done") return 3;
      return 4; // conflict, etc — treat as low priority
    }
    const eligible = tasks.filter((t) => t.status !== "rejected");
    const tier0 = eligible.filter((t) => taskTier(t) === 0).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const tier1 = eligible.filter((t) => taskTier(t) === 1).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));
    const tier2 = eligible.filter((t) => taskTier(t) === 2).sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    const tier3 = eligible.filter((t) => taskTier(t) === 3).sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || "")).slice(0, 3);
    const sorted = [...tier0, ...tier1, ...tier2, ...tier3].slice(0, 7);
    let taskHtml = "";
    for (const t of sorted) {
      const tid = "T" + String(t.id).padStart(4, "0");
      taskHtml +=
        '<div class="sidebar-task-row" style="cursor:pointer" onclick="openTaskPanel(' +
        t.id +
        ')"><span class="sidebar-task-dot dot-' + t.status + '" title="' + fmtStatus(t.status) + '"></span>' +
        '<span class="sidebar-task-id">' +
        tid +
        '</span><span class="sidebar-task-title">' +
        esc(t.title) +
        '</span><span class="sidebar-task-assignee">' +
        (t.assignee ? cap(t.assignee) : "") +
        "</span></div>";
    }
    document.getElementById("sidebarTaskList").innerHTML = taskHtml;
  } catch (e) {
    console.error("Sidebar load error:", e);
  }
}

// =====================================================================
// Diff panel  (powered by diff2html)
// =====================================================================
function renderDiffFiles() {
  const files = diff2HtmlParse(_diffRawText);
  if (!files.length)
    return '<div class="diff-empty">No files changed</div>';
  let html = '<div class="diff-file-list">';
  for (const f of files) {
    const name = (f.newName === '/dev/null' ? f.oldName : f.newName) || f.oldName || "unknown";
    html +=
      '<div class="diff-file-list-item" onclick="switchDiffTab(\'diff\')"><span class="diff-file-list-name">' +
      esc(name) +
      '</span><span class="diff-file-stats"><span class="diff-file-add">+' +
      f.addedLines +
      '</span><span class="diff-file-del">-' +
      f.deletedLines +
      "</span></span></div>";
  }
  return html + "</div>";
}

function renderDiffFull() {
  if (!_diffRawText)
    return '<div class="diff-empty">No changes</div>';
  return diff2HtmlRender(_diffRawText, {
    outputFormat: "line-by-line",
    drawFileList: false,
    matching: "lines",
  });
}

function switchDiffTab(tab) {
  _diffCurrentTab = tab;
  document
    .querySelectorAll(".diff-tab")
    .forEach((t) => t.classList.toggle("active", t.dataset.dtab === tab));
  const body = document.getElementById("diffPanelBody");
  if (!_diffRawText && !_panelAgent) return;
  body.innerHTML =
    tab === "files" ? renderDiffFiles() : renderDiffFull();
}

async function openDiffPanel(taskId) {
  // Close task panel if open (don't stack panels)
  if (_panelTask !== null) closeTaskPanel();
  _panelMode = "diff";
  _panelAgent = null;
  _agentTabData = {};
  const panel = document.getElementById("diffPanel");
  const backdrop = document.getElementById("diffBackdrop");
  document.getElementById("diffPanelTitle").textContent =
    "T" + String(taskId).padStart(4, "0");
  document.getElementById("diffPanelBranch").textContent = "Loading...";
  document.getElementById("diffPanelCommits").innerHTML = "";
  document.getElementById("diffPanelCommits").style.display = "";
  const tabsEl = panel.querySelector(".diff-panel-tabs");
  tabsEl.innerHTML =
    '<button class="diff-tab active" data-dtab="files" onclick="switchDiffTab(\'files\')">Files Changed</button><button class="diff-tab" data-dtab="diff" onclick="switchDiffTab(\'diff\')">Full Diff</button>';
  document.getElementById("diffPanelBody").innerHTML =
    '<div class="diff-empty">Loading diff...</div>';
  panel.classList.add("open");
  backdrop.classList.add("open");
  try {
    const res = await fetch("/tasks/" + taskId + "/diff");
    const data = await res.json();
    // Show merge_base..merge_tip range if available, otherwise branch name
    if (data.merge_base && data.merge_tip) {
      document.getElementById("diffPanelBranch").textContent =
        data.merge_base.substring(0, 7) + ".." + data.merge_tip.substring(0, 7);
    } else {
      document.getElementById("diffPanelBranch").textContent =
        data.branch || "no branch";
    }
    document.getElementById("diffPanelCommits").innerHTML = (
      data.commits || []
    )
      .map(
        (c) =>
          '<span class="diff-panel-commit">' +
          esc(String(c).substring(0, 7)) +
          "</span>"
      )
      .join("");
    _diffRawText = data.diff || "";
    _diffCurrentTab = "files";
    document
      .querySelectorAll(".diff-tab")
      .forEach((t) =>
        t.classList.toggle("active", t.dataset.dtab === "files")
      );
    document.getElementById("diffPanelBody").innerHTML =
      renderDiffFiles();
  } catch (e) {
    document.getElementById("diffPanelBody").innerHTML =
      '<div class="diff-empty">Failed to load diff</div>';
  }
}

function closePanel() {
  document.getElementById("diffPanel").classList.remove("open");
  document.getElementById("diffBackdrop").classList.remove("open");
  _diffRawText = "";
  _panelMode = null;
  _panelAgent = null;
  _agentTabData = {};
}

// =====================================================================
// Agent panel (re-uses the diff slide-over)
// =====================================================================
function renderAgentInbox(msgs) {
  if (!msgs || !msgs.length)
    return '<div class="diff-empty">No messages</div>';
  return msgs
    .map(
      (m) =>
        '<div class="agent-msg' +
        (m.read ? "" : " unread") +
        '"><div class="agent-msg-header"><span class="agent-msg-sender">' +
        esc(cap(m.sender)) +
        '</span><span class="agent-msg-time">' +
        fmtTimestamp(m.time) +
        '</span></div><div class="agent-msg-body collapsed" onclick="this.classList.toggle(\'collapsed\')">' +
        esc(m.body) +
        "</div></div>"
    )
    .join("");
}

function renderAgentOutbox(msgs) {
  if (!msgs || !msgs.length)
    return '<div class="diff-empty">No messages</div>';
  return msgs
    .map(
      (m) =>
        '<div class="agent-msg' +
        (m.routed ? "" : " pending") +
        '"><div class="agent-msg-header"><span class="agent-msg-sender">\u2192 ' +
        esc(cap(m.recipient)) +
        '</span><span class="agent-msg-time">' +
        fmtTimestamp(m.time) +
        '</span></div><div class="agent-msg-body collapsed" onclick="this.classList.toggle(\'collapsed\')">' +
        esc(m.body) +
        "</div></div>"
    )
    .join("");
}

function renderAgentLogs(data) {
  const sessions = data && data.sessions ? data.sessions : [];
  if (!sessions.length)
    return '<div class="diff-empty">No worklogs</div>';
  return sessions
    .map(
      (s, i) =>
        '<div class="agent-log-session"><div class="agent-log-header" onclick="toggleLogSession(this)"><span class="agent-log-arrow' +
        (i === 0 ? " expanded" : "") +
        '">\u25B6</span>' +
        esc(s.filename) +
        '</div><div class="agent-log-content' +
        (i === 0 ? " expanded" : "") +
        '">' +
        esc(s.content) +
        "</div></div>"
    )
    .join("");
}

function toggleLogSession(header) {
  header.querySelector(".agent-log-arrow").classList.toggle("expanded");
  header.nextElementSibling.classList.toggle("expanded");
}

function renderAgentStatsPanel(s) {
  if (!s)
    return '<div class="diff-empty">Stats unavailable</div>';
  return (
    '<div class="agent-stats-grid">' +
    '<div class="agent-stat"><div class="agent-stat-label">Tasks done</div><div class="agent-stat-value">' +
    s.tasks_done +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">In review</div><div class="agent-stat-value">' +
    s.tasks_in_review +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Total tasks</div><div class="agent-stat-value">' +
    s.tasks_total +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Sessions</div><div class="agent-stat-value">' +
    s.session_count +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Tokens (in/out)</div><div class="agent-stat-value">' +
    fmtTokens(s.total_tokens_in, s.total_tokens_out) +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Total cost</div><div class="agent-stat-value">' +
    fmtCost(s.total_cost_usd) +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Agent time</div><div class="agent-stat-value">' +
    fmtElapsed(s.agent_time_seconds) +
    "</div></div>" +
    '<div class="agent-stat"><div class="agent-stat-label">Avg task time</div><div class="agent-stat-value">' +
    fmtElapsed(s.avg_task_seconds) +
    "</div></div></div>"
  );
}

async function switchAgentTab(tab) {
  _agentCurrentTab = tab;
  document
    .querySelectorAll(".diff-tab")
    .forEach((t) => t.classList.toggle("active", t.dataset.dtab === tab));
  const body = document.getElementById("diffPanelBody");
  const name = _panelAgent;
  if (!name) return;
  if (_agentTabData[tab]) {
    _renderAgentTab(tab, _agentTabData[tab]);
    return;
  }
  body.innerHTML = '<div class="diff-empty">Loading...</div>';
  try {
    const url =
      "/teams/" + _currentTeam + "/agents/" + name + "/" + tab;
    const res = await fetch(url);
    const data = await res.json();
    _agentTabData[tab] = data;
    _renderAgentTab(tab, data);
  } catch (e) {
    body.innerHTML =
      '<div class="diff-empty">Failed to load ' + tab + "</div>";
  }
}

function _renderAgentTab(tab, data) {
  const body = document.getElementById("diffPanelBody");
  if (tab === "inbox") body.innerHTML = renderAgentInbox(data);
  else if (tab === "outbox") body.innerHTML = renderAgentOutbox(data);
  else if (tab === "logs") body.innerHTML = renderAgentLogs(data);
  else if (tab === "stats") body.innerHTML = renderAgentStatsPanel(data);
}

async function openAgentPanel(agentName) {
  // Close task panel if open (don't stack panels)
  if (_panelTask !== null) closeTaskPanel();
  _panelMode = "agent";
  _panelAgent = agentName;
  _agentTabData = {};
  _agentCurrentTab = "inbox";
  _diffRawText = "";
  const panel = document.getElementById("diffPanel");
  const backdrop = document.getElementById("diffBackdrop");
  document.getElementById("diffPanelTitle").textContent = cap(agentName);
  document.getElementById("diffPanelBranch").textContent = "";
  document.getElementById("diffPanelCommits").innerHTML = "";
  document.getElementById("diffPanelCommits").style.display = "none";
  try {
    const r = await fetch("/teams/" + _currentTeam + "/agents");
    const agents = await r.json();
    const agent = agents.find((a) => a.name === agentName);
    if (agent)
      document.getElementById("diffPanelBranch").textContent = cap(
        agent.role
      );
  } catch (e) {}
  const tabsEl = panel.querySelector(".diff-panel-tabs");
  tabsEl.innerHTML =
    '<button class="diff-tab active" data-dtab="inbox" onclick="switchAgentTab(\'inbox\')">Inbox</button><button class="diff-tab" data-dtab="outbox" onclick="switchAgentTab(\'outbox\')">Outbox</button><button class="diff-tab" data-dtab="logs" onclick="switchAgentTab(\'logs\')">Logs</button><button class="diff-tab" data-dtab="stats" onclick="switchAgentTab(\'stats\')">Stats</button>';
  document.getElementById("diffPanelBody").innerHTML =
    '<div class="diff-empty">Loading...</div>';
  panel.classList.add("open");
  backdrop.classList.add("open");
  switchAgentTab("inbox");
}

// =====================================================================
// Message sending
// =====================================================================
async function sendMsg() {
  if (_micActive && _recognition) {
    _recognition.stop();
    _micActive = false;
    const mb = document.getElementById("micBtn");
    if (mb) {
      mb.classList.remove("recording");
      mb.title = "Voice input";
    }
  }
  const input = document.getElementById("msgInput");
  const recipient = document.getElementById("recipient").value;
  if (!input.value.trim() || !_currentTeam) return;
  if (!recipient) {
    console.warn("No recipient selected");
    return;
  }
  _msgSendCooldown = true;
  setTimeout(function () {
    _msgSendCooldown = false;
  }, 4000);
  try {
    const res = await fetch("/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        team: _currentTeam,
        recipient,
        content: input.value,
      }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      console.error("Send failed:", err.detail || res.statusText);
      return;
    }
    input.value = "";
    input.style.height = "auto";
    updateSendBtn();
  } catch (e) {
    console.error("Send error:", e);
  }
}

// =====================================================================
// Textarea helpers
// =====================================================================
function autoResizeTextarea(el) {
  el.style.height = "auto";
  el.style.height = el.scrollHeight + "px";
  updateSendBtn();
}
function handleChatKeydown(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMsg();
  }
}
function updateSendBtn() {
  const input = document.getElementById("msgInput");
  const sendBtn = document.getElementById("sendBtn");
  if (!sendBtn) return;
  if (input && input.value.trim()) {
    sendBtn.classList.add("active");
  } else {
    sendBtn.classList.remove("active");
  }
}

// =====================================================================
// Voice-to-text (mic)
// =====================================================================
(function initMic() {
  const SpeechRecognition =
    window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  const micBtn = document.getElementById("micBtn");
  micBtn.style.display = "flex";
  _recognition = new SpeechRecognition();
  _recognition.continuous = true;
  _recognition.interimResults = true;
  _recognition.lang = navigator.language || "en-US";
  _recognition.onresult = function (e) {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) _micFinalText += e.results[i][0].transcript;
      else interim += e.results[i][0].transcript;
    }
    const _el = document.getElementById("msgInput");
    _el.value = _micBaseText + _micFinalText + interim;
    autoResizeTextarea(_el);
  };
  _recognition.onend = function () {
    _micActive = false;
    _micStopping = false;
    micBtn.classList.remove("recording");
    micBtn.title = "Voice input";
  };
  _recognition.onerror = function (e) {
    if (e.error !== "aborted" && e.error !== "no-speech")
      console.warn("Speech recognition error:", e.error);
    _micActive = false;
    _micStopping = false;
    micBtn.classList.remove("recording");
    micBtn.title = "Voice input";
  };
})();

function toggleMic() {
  if (!_recognition || _micStopping) return;
  const micBtn = document.getElementById("micBtn");
  if (_micActive) {
    _micStopping = true;
    _recognition.stop();
    micBtn.classList.remove("recording");
    micBtn.title = "Voice input";
  } else {
    const input = document.getElementById("msgInput");
    _micBaseText = input.value ? input.value + " " : "";
    _micFinalText = "";
    try {
      _recognition.start();
    } catch (e) {
      return;
    }
    _micActive = true;
    micBtn.classList.add("recording");
    micBtn.title = "Stop recording";
  }
}

// =====================================================================
// Keyboard & Polling
// =====================================================================
document.addEventListener("keydown", function (e) {
  if (e.key === "Escape") {
    if (_panelTask !== null) closeTaskPanel();
    else closePanel();
  }
});

function refreshTimestamps() {
  document.querySelectorAll(".ts[data-ts]").forEach((el) => {
    el.textContent = fmtTimestamp(el.dataset.ts);
  });
}
setInterval(refreshTimestamps, 30000);

setInterval(() => {
  if (!_currentTeam || !_teams.length) loadTeams();
  loadSidebar();
  const active = document.querySelector(".panel.active");
  if (active && active.id === "chat") loadChat();
  if (active && active.id === "tasks") loadTasks();
  if (active && active.id === "agents") loadAgents();
}, 2000);

function initFromHash() {
  const hash = window.location.hash.replace("#", "");
  const valid = ["chat", "tasks", "agents"];
  switchTab(valid.includes(hash) ? hash : "chat", false);
}
window.addEventListener("hashchange", () => {
  const hash = window.location.hash.replace("#", "");
  const valid = ["chat", "tasks", "agents"];
  if (valid.includes(hash)) switchTab(hash, false);
});

// =====================================================================
// Init
// =====================================================================
initTheme();
_updateMuteBtn();
loadTeams().then(() => {
  initFromHash();
  loadSidebar();
});

// =====================================================================
// Expose functions to global scope for inline HTML event handlers
// =====================================================================
Object.assign(window, {
  switchTab,
  onTeamChange,
  toggleMute,
  cycleTheme,
  loadChat,
  loadTasks,
  sendMsg,
  handleChatKeydown,
  autoResizeTextarea,
  updateSendBtn,
  toggleMic,
  toggleTask,
  openDiffPanel,
  switchDiffTab,
  closePanel,
  openAgentPanel,
  switchAgentTab,
  toggleLogSession,
  approveTask,
  rejectTask,
  toggleRejectReason,
  openTaskPanel,
  closeTaskPanel,
  switchTaskPanelDiffTab,
});
