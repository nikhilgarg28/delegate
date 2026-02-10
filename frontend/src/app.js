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
let _bossName = "boss"; // fetched from /config
let _isMuted = localStorage.getItem("delegate-muted") === "true";
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
let _taskPanelActiveTab = "details";
let _taskPanelDiffLoaded = false;
let _taskPanelCommitsData = null;  // cached per-commit diffs

// Voice-to-text state
let _recognition = null;
let _micActive = false;
let _micStopping = false;
let _micBaseText = "";
let _micFinalText = "";

// Debounce timers for search inputs
let _chatSearchTimer = null;
let _taskSearchTimer = null;

// Chat filter arrow direction: "one-way" (→) or "bidi" (↔)
let _chatFilterDirection = "one-way";

// =====================================================================
// Multi-repo diff helpers
// =====================================================================

/**
 * Flatten a diff dict {repo: diffText, ...} into a single string.
 * If the input is already a string, return as-is (backward compat).
 */
function flattenDiffDict(diff) {
  if (!diff) return "";
  if (typeof diff === "string") return diff;
  if (typeof diff !== "object") return "";
  var keys = Object.keys(diff);
  if (keys.length === 0) return "";
  if (keys.length === 1) return diff[keys[0]] || "";
  // Multi-repo: prepend a header per repo
  return keys.map(function (repo) {
    return "# ── " + repo + " ──\n" + (diff[repo] || "(no diff)");
  }).join("\n\n");
}

/**
 * Flatten a commits dict {repo: [sha, ...], ...} into a flat array.
 * If the input is already an array, return as-is (backward compat).
 */
function flattenCommitsDict(commits) {
  if (!commits) return [];
  if (Array.isArray(commits)) return commits;
  if (typeof commits !== "object") return [];
  var all = [];
  Object.keys(commits).forEach(function (repo) {
    (commits[repo] || []).forEach(function (c) { all.push(c); });
  });
  return all;
}

// =====================================================================
// Filter persistence (sessionStorage)
// =====================================================================
function _saveChatFilters() {
  try {
    sessionStorage.setItem("chatFilters", JSON.stringify({
      search: document.getElementById("chatFilterSearch").value,
      from: document.getElementById("chatFilterFrom").value,
      to: document.getElementById("chatFilterTo").value,
      showEvents: document.getElementById("chatShowEvents").checked,
      direction: _chatFilterDirection,
    }));
  } catch (e) { }
}
function _restoreChatFilters() {
  try {
    const raw = sessionStorage.getItem("chatFilters");
    if (!raw) return;
    const f = JSON.parse(raw);
    if (f.search) document.getElementById("chatFilterSearch").value = f.search;
    if (f.from) document.getElementById("chatFilterFrom").value = f.from;
    if (f.to) document.getElementById("chatFilterTo").value = f.to;
    if (f.showEvents === false) document.getElementById("chatShowEvents").checked = false;
    if (f.direction === "bidi") {
      _chatFilterDirection = "bidi";
      const el = document.getElementById("chatFilterArrow");
      if (el) { el.innerHTML = "↔"; el.classList.add("bidi"); }
    }
  } catch (e) { }
}
function _saveTaskFilters() {
  try {
    sessionStorage.setItem("taskFilters", JSON.stringify({
      search: document.getElementById("taskFilterSearch").value,
      status: document.getElementById("taskFilterStatus").value,
      assignee: document.getElementById("taskFilterAssignee").value,
      priority: document.getElementById("taskFilterPriority").value,
      repo: document.getElementById("taskFilterRepo").value,
    }));
  } catch (e) { }
}
function _restoreTaskFilters() {
  try {
    const raw = sessionStorage.getItem("taskFilters");
    if (!raw) return;
    const f = JSON.parse(raw);
    if (f.search) document.getElementById("taskFilterSearch").value = f.search;
    if (f.status) document.getElementById("taskFilterStatus").value = f.status;
    if (f.assignee) document.getElementById("taskFilterAssignee").value = f.assignee;
    if (f.priority) document.getElementById("taskFilterPriority").value = f.priority;
    if (f.repo) document.getElementById("taskFilterRepo").value = f.repo;
  } catch (e) { }
}

// Debounced search input handlers
function onChatSearchInput() {
  clearTimeout(_chatSearchTimer);
  _chatSearchTimer = setTimeout(function () {
    _saveChatFilters();
    loadChat();
  }, 300);
}
function onChatFilterChange() {
  _saveChatFilters();
  loadChat();
}
function toggleFilterArrow() {
  _chatFilterDirection = _chatFilterDirection === "one-way" ? "bidi" : "one-way";
  const el = document.getElementById("chatFilterArrow");
  el.innerHTML = _chatFilterDirection === "bidi" ? "↔" : "→";
  el.classList.toggle("bidi", _chatFilterDirection === "bidi");
  _saveChatFilters();
  loadChat();
}
function onTaskSearchInput() {
  clearTimeout(_taskSearchTimer);
  _taskSearchTimer = setTimeout(function () {
    _saveTaskFilters();
    loadTasks();
  }, 300);
}
function onTaskFilterChange() {
  _saveTaskFilters();
  loadTasks();
}

// =====================================================================
// Team selector (sidebar dropdown — Option A)
// =====================================================================
let _teamDropdownOpen = false;

async function loadTeams() {
  try {
    const res = await fetch("/teams");
    if (!res.ok) return;
    _teams = await res.json();
    const prev = _currentTeam;
    if (prev && _teams.includes(prev)) {
      _currentTeam = prev;
    } else if (_teams.length > 0) {
      _currentTeam = _teams[0];
    }
    _renderSidebarTeamSelector();
  } catch (e) {
    console.warn("loadTeams failed:", e);
  }
}

function _renderSidebarTeamSelector() {
  const container = document.getElementById("sidebarTeamSelector");
  const nameEl = document.getElementById("sidebarTeamName");
  if (!container || !nameEl) return;

  nameEl.textContent = _currentTeam || "No team";

  // Single team: hide chevron, disable click
  if (_teams.length <= 1) {
    container.classList.add("single-team");
  } else {
    container.classList.remove("single-team");
  }

  // Remove any existing dropdown
  _closeTeamDropdown();
}

function _toggleTeamDropdown(e) {
  if (e) e.stopPropagation();
  const container = document.getElementById("sidebarTeamSelector");
  if (!container || container.classList.contains("single-team")) return;
  if (_teamDropdownOpen) {
    _closeTeamDropdown();
  } else {
    _openTeamDropdown();
  }
}

function _openTeamDropdown() {
  const container = document.getElementById("sidebarTeamSelector");
  if (!container) return;
  // Remove old dropdown if any
  _closeTeamDropdown();
  _teamDropdownOpen = true;
  // Rotate chevron to indicate open state
  const chevron = document.getElementById("sidebarTeamChevron");
  if (chevron) chevron.style.transform = "rotate(180deg)";
  const dd = document.createElement("div");
  dd.className = "sidebar-team-dropdown";
  dd.id = "sidebarTeamDropdown";
  for (const t of _teams) {
    const opt = document.createElement("div");
    opt.className = "sidebar-team-option" + (t === _currentTeam ? " active" : "");
    opt.textContent = t;
    opt.addEventListener("click", function (e) {
      e.stopPropagation();
      _selectTeam(t);
    });
    dd.appendChild(opt);
  }
  container.appendChild(dd);
}

function _closeTeamDropdown() {
  _teamDropdownOpen = false;
  // Reset chevron rotation
  const chevron = document.getElementById("sidebarTeamChevron");
  if (chevron) chevron.style.transform = "";
  const dd = document.getElementById("sidebarTeamDropdown");
  if (dd) dd.remove();
}

function _selectTeam(team) {
  _closeTeamDropdown();
  if (team === _currentTeam) return;
  _currentTeam = team;
  _renderSidebarTeamSelector();
  onTeamChange();
}

function onTeamChange() {
  loadChat();
  loadAgents();
  loadSidebar();
}

// Click handler for the team selector row
(function _initTeamSelector() {
  const container = document.getElementById("sidebarTeamSelector");
  if (container) {
    container.addEventListener("click", _toggleTeamDropdown);
  }
})();

// Close dropdown on outside click
document.addEventListener("click", function (e) {
  if (!_teamDropdownOpen) return;
  const container = document.getElementById("sidebarTeamSelector");
  if (container && !container.contains(e.target)) {
    _closeTeamDropdown();
  }
});

// =====================================================================
// Audio / mute
// =====================================================================
function toggleMute() {
  _isMuted = !_isMuted;
  localStorage.setItem("delegate-muted", _isMuted ? "true" : "false");
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
  const pref = localStorage.getItem("delegate-theme"); // "light", "dark", or null (system)
  applyTheme(pref);
}

function cycleTheme() {
  const current = localStorage.getItem("delegate-theme");
  let next;
  if (current === null) next = "light";
  else if (current === "light") next = "dark";
  else next = null; // back to system
  if (next) localStorage.setItem("delegate-theme", next);
  else localStorage.removeItem("delegate-theme");
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
  const t = _relativeTimeParts(iso);
  if (!t) return "\u2014";
  if (t.sec < 60) return "Just now";
  if (t.min < 60) return t.min + " min ago";
  const d = new Date(iso);
  const time = d.toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
  if (t.hr < 24) return time;
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

/**
 * Render a status badge for the Details tab (read-only indicator).
 */
function renderTaskApprovalBadge(task) {
  const status = task.status || "";
  const approvalStatus = task.approval_status || "";
  if (status === "done" || approvalStatus === "approved") {
    return '<div class="task-approval-status"><div class="approval-badge approval-badge-approved">\u2714 Approved & Merged</div></div>';
  }
  if (status === "rejected" || approvalStatus === "rejected") {
    const reason = task.rejection_reason || "";
    return (
      '<div class="task-approval-status"><div class="approval-badge approval-badge-rejected">\u2716 Rejected</div>' +
      (reason ? '<div class="approval-rejection-reason">' + esc(reason) + "</div>" : "") +
      "</div>"
    );
  }
  if (status === "in_approval") {
    return '<div class="task-approval-status"><div class="approval-badge approval-badge-pending">\u23F3 Awaiting Approval</div>' +
      '<span style="font-size:12px;color:var(--text-muted);margin-top:4px">Review changes in the <a href="#" onclick="event.preventDefault();switchTaskTab(\'changes\')" style="color:var(--accent-blue)">Changes</a> tab</span></div>';
  }
  if (status === "conflict") {
    return '<div class="task-approval-status"><div class="approval-badge" style="background:rgba(251,146,60,0.12);color:#fb923c">\u26A0 Merge Conflict</div></div>';
  }
  return "";
}

/**
 * Render approve/reject action buttons for the Changes tab (below diff).
 */
function renderTaskApprovalActions(task) {
  const status = task.status || "";
  const approvalStatus = task.approval_status || "";
  if (status === "done" || approvalStatus === "approved") {
    return '<div class="task-review-box task-review-box-approved"><div class="approval-badge approval-badge-approved">\u2714 Approved & Merged</div></div>';
  }
  if (status === "rejected" || approvalStatus === "rejected") {
    const reason = task.rejection_reason || "";
    return '<div class="task-review-box task-review-box-rejected"><div class="approval-badge approval-badge-rejected">\u2716 Changes Rejected</div>' +
      (reason ? '<div class="approval-rejection-reason">' + esc(reason) + "</div>" : "") + "</div>";
  }
  if (status !== "in_approval") return "";
  let html = '<div class="task-review-box">';
  html += '<div class="task-review-box-header">Review changes</div>';
  html += '<div class="task-review-box-actions" id="approvalActionsRow">';
  html += '<button class="btn-approve" id="btnApprove" onclick="event.stopPropagation();approveTask(' + task.id + ')">\u2714 Approve & Merge</button>';
  html += '<button class="btn-reject-outline" id="btnReject" onclick="event.stopPropagation();toggleRejectReason(' + task.id + ')">\u2716 Request Changes</button>';
  html += "</div>";
  html += '<div class="reject-reason-row" id="rejectReasonRow" style="display:none">';
  html += '<input type="text" class="reject-reason-input" id="rejectReasonInput" placeholder="Describe what needs to change..." onclick="event.stopPropagation()" onkeydown="event.stopPropagation();if(event.keyCode===13)rejectTask(' + task.id + ')">';
  html += '<button class="btn-reject" onclick="event.stopPropagation();rejectTask(' + task.id + ')" style="flex-shrink:0">Submit</button>';
  html += '</div>';
  html += "</div>";
  return html;
}

function toggleRejectReason(taskId) {
  _rejectReasonVisible = !_rejectReasonVisible;
  var row = document.getElementById("rejectReasonRow");
  if (row) {
    row.style.display = _rejectReasonVisible ? "flex" : "none";
    if (_rejectReasonVisible) {
      setTimeout(function () {
        var el = document.getElementById("rejectReasonInput");
        if (el) el.focus();
      }, 50);
    }
  }
}

async function approveTask(taskId) {
  var btn = document.getElementById("btnApprove");
  var rejectBtn = document.getElementById("btnReject");
  if (btn) { btn.disabled = true; btn.textContent = "Merging..."; }
  if (rejectBtn) rejectBtn.disabled = true;
  try {
    const res = await fetch("/teams/" + _currentTeam + "/tasks/" + taskId + "/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    if (res.ok) {
      _rejectReasonVisible = false;
      // Update the review box in-place
      var reviewBox = btn ? btn.closest(".task-review-box") : null;
      if (reviewBox) {
        reviewBox.className = "task-review-box task-review-box-approved";
        reviewBox.innerHTML = '<div class="approval-badge approval-badge-approved">\u2714 Approved & Merged</div>';
      }
      // Also update the Details tab badge
      var statusBadge = document.getElementById("taskPanelStatus");
      if (statusBadge) statusBadge.innerHTML = '<span class="badge badge-done">Done</span>';
      var approvalDiv = document.querySelector(".task-approval-status");
      if (approvalDiv) approvalDiv.innerHTML = '<div class="approval-badge approval-badge-approved">\u2714 Approved & Merged</div>';
      // Refresh sidebar & task list in background
      loadTasks();
      loadSidebar();
    } else {
      const err = await res.json().catch(function () { return {}; });
      if (btn) { btn.disabled = false; btn.textContent = "\u2714 Approve & Merge"; }
      if (rejectBtn) rejectBtn.disabled = false;
      alert("Failed to approve: " + (err.detail || res.statusText));
    }
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "\u2714 Approve & Merge"; }
    if (rejectBtn) rejectBtn.disabled = false;
    alert("Failed to approve task: " + e.message);
  }
}

async function rejectTask(taskId) {
  const reasonEl = document.getElementById("rejectReasonInput");
  const reason = reasonEl ? reasonEl.value.trim() : "";
  var confirmBtn = reasonEl ? reasonEl.parentElement.querySelector(".btn-reject") : null;
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = "Rejecting..."; }
  try {
    const res = await fetch("/teams/" + _currentTeam + "/tasks/" + taskId + "/reject", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason: reason || "(no reason)" }),
    });
    if (res.ok) {
      _rejectReasonVisible = false;
      // Update the review box in-place
      var reviewBox = confirmBtn ? confirmBtn.closest(".task-review-box") : null;
      if (reviewBox) {
        reviewBox.className = "task-review-box task-review-box-rejected";
        reviewBox.innerHTML = '<div class="approval-badge approval-badge-rejected">\u2716 Changes Rejected</div>' +
          (reason ? '<div class="approval-rejection-reason">' + esc(reason) + '</div>' : '');
      }
      // Also update the Details tab badge
      var statusBadge = document.getElementById("taskPanelStatus");
      if (statusBadge) statusBadge.innerHTML = '<span class="badge badge-rejected">Rejected</span>';
      var approvalDiv = document.querySelector(".task-approval-status");
      if (approvalDiv) approvalDiv.innerHTML = '<div class="approval-badge approval-badge-rejected">\u2716 Rejected</div>';
      loadTasks();
      loadSidebar();
    } else {
      const err = await res.json().catch(function () { return {}; });
      if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "Submit"; }
      alert("Failed to reject: " + (err.detail || res.statusText));
    }
  } catch (e) {
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = "Submit"; }
    alert("Failed to reject task: " + e.message);
  }
}

async function loadTasks() {
  if (!_currentTeam) return;
  let res;
  try {
    res = await fetch("/teams/" + _currentTeam + "/tasks");
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
      (t.status === "done" || t.status === "in_review")
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
  const repos = new Set();
  for (const t of allTasks) {
    if (t.assignee) assignees.add(t.assignee);
    if (t.repo) {
      var taskRepos = Array.isArray(t.repo) ? t.repo : [t.repo];
      taskRepos.forEach(function (r) { if (r) repos.add(r); });
    }
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
  // Populate repo filter dynamically
  const repoSel = document.getElementById("taskFilterRepo");
  const prevRepo = repoSel.value;
  repoSel.innerHTML =
    '<option value="">All</option>' +
    [...repos]
      .sort()
      .map((r) => `<option value="${r}">${esc(r)}</option>`)
      .join("");
  repoSel.value = prevRepo;
  const filterStatus = document.getElementById("taskFilterStatus").value;
  const filterPriority = document.getElementById("taskFilterPriority").value;
  const filterAssignee = document.getElementById("taskFilterAssignee").value;
  const filterRepo = document.getElementById("taskFilterRepo").value;
  const searchQuery = (document.getElementById("taskFilterSearch").value || "").toLowerCase().trim();
  let tasks = allTasks;
  if (filterStatus) tasks = tasks.filter((t) => t.status === filterStatus);
  if (filterPriority)
    tasks = tasks.filter((t) => t.priority === filterPriority);
  if (filterAssignee)
    tasks = tasks.filter((t) => t.assignee === filterAssignee);
  if (filterRepo)
    tasks = tasks.filter((t) => {
      var repos = Array.isArray(t.repo) ? t.repo : [t.repo];
      return repos.indexOf(filterRepo) !== -1;
    });
  if (searchQuery)
    tasks = tasks.filter((t) => {
      const title = (t.title || "").toLowerCase();
      const desc = (t.description || "").toLowerCase();
      return title.indexOf(searchQuery) !== -1 || desc.indexOf(searchQuery) !== -1;
    });
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
    return match.replace(/(?<!\/)T(\d{4})\b/g, function (full, digits) {
      const id = parseInt(digits, 10);
      return '<span class="task-link" data-task-id="' + id + '" onclick="event.stopPropagation();openTaskPanel(' + id + ')">' + full + '</span>';
    });
  });
}

/**
 * Make known agent names clickable in event text.
 * Caches the agent name list from the sidebar agent data.
 */
let _knownAgentNames = [];
function updateKnownAgents(agents) {
  _knownAgentNames = (agents || []).map(function (a) { return a.name; });
}
/**
 * Detect shared/ file paths in text and make them clickable.
 * Matches paths like shared/specs/foo.md, shared/decisions/bar.md, etc.
 * Only matches in text nodes (between > and <, or at start/end of string).
 */
function linkifyFilePaths(html) {
  return html.replace(/(^[^<]+|>[^<]*)/g, function (match) {
    return match.replace(/\bshared\/[\w\-\.\/]+\.[\w]+/g, function (path) {
      return '<span class="file-link" data-file-path="' + esc(path) + '" onclick="event.stopPropagation();openFilePanel(\'' + esc(path).replace(/'/g, "\\'") + '\')">' + esc(path) + '</span>';
    });
  });
}

function agentifyRefs(html) {
  if (!_knownAgentNames.length) return html;
  // Build a regex that matches capitalized agent names in text nodes
  var pattern = new RegExp("\\b(" + _knownAgentNames.map(function (n) { return n.charAt(0).toUpperCase() + n.slice(1); }).join("|") + ")(?!/)", "g");
  return html.replace(/(^[^<]+|>[^<]*)/g, function (match) {
    return match.replace(pattern, function (full) {
      var name = full.toLowerCase();
      return '<span class="agent-link" onclick="event.stopPropagation();openAgentPanel(\'' + name + '\')">' + full + '</span>';
    });
  });
}

function switchTaskPanelDiffTab(tab) {
  _taskPanelDiffTab = tab;
  const container = document.getElementById("taskPanelBody");
  if (!container) return;
  container.querySelectorAll(".task-panel-diff-tabs .diff-tab").forEach(
    function (t) { t.classList.toggle("active", t.dataset.dtab === tab); }
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
  } else if (tab === "commits") {
    _renderCommitsTab(diffContent);
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

/**
 * Load and render per-commit diffs in the Commits sub-tab.
 * Fetches from /teams/{team}/tasks/{id}/commits on first access, then caches.
 */
function _renderCommitsTab(diffContent) {
  if (_taskPanelCommitsData !== null) {
    _renderCommitsHtml(diffContent, _taskPanelCommitsData);
    return;
  }
  diffContent.innerHTML = '<div class="diff-empty">Loading commits...</div>';
  fetch("/teams/" + _currentTeam + "/tasks/" + _panelTask + "/commits")
    .then(function (res) { return res.json(); })
    .then(function (data) {
      _taskPanelCommitsData = data;
      _renderCommitsHtml(diffContent, data);
    })
    .catch(function () {
      diffContent.innerHTML = '<div class="diff-empty">Failed to load commits</div>';
    });
}

function _renderCommitsHtml(container, commits) {
  if (!commits || !commits.length) {
    container.innerHTML = '<div class="diff-empty">No commits recorded</div>';
    return;
  }
  var html = '<div class="commit-list">';
  for (var i = 0; i < commits.length; i++) {
    var c = commits[i];
    var shortSha = String(c.sha || "").substring(0, 7);
    var msg = esc(c.message || "(no message)");
    var repoLabel = commits.length > 1 && c.repo ? '<span class="commit-repo-label">' + esc(c.repo) + '</span>' : '';
    html += '<div class="commit-item">';
    html += '<div class="commit-header" onclick="toggleCommitDiff(' + i + ')">';
    html += '<span class="commit-expand-icon" id="commitArrow' + i + '">\u25B6</span>';
    html += '<span class="commit-sha">' + shortSha + '</span>';
    html += '<span class="commit-message">' + msg + '</span>';
    html += repoLabel;
    html += '</div>';
    html += '<div class="commit-diff" id="commitDiff' + i + '" style="display:none">';
    if (c.diff && c.diff !== "(empty diff)") {
      html += diff2HtmlRender(c.diff, { outputFormat: "line-by-line", drawFileList: false, matching: "lines" });
    } else {
      html += '<div class="diff-empty">Empty diff</div>';
    }
    html += '</div>';
    html += '</div>';
  }
  html += '</div>';
  container.innerHTML = html;
}

function toggleCommitDiff(idx) {
  var diffDiv = document.getElementById("commitDiff" + idx);
  var arrow = document.getElementById("commitArrow" + idx);
  if (!diffDiv) return;
  var isOpen = diffDiv.style.display !== "none";
  diffDiv.style.display = isOpen ? "none" : "";
  if (arrow) arrow.textContent = isOpen ? "\u25B6" : "\u25BC";
}

// =====================================================================
// Task panel top-level tabs (Details / Changes)
// =====================================================================
function switchTaskTab(tab) {
  _taskPanelActiveTab = tab;
  // Update tab buttons
  document.querySelectorAll("#taskPanelTabs .task-panel-tab").forEach(function (btn) {
    btn.classList.toggle("active", btn.dataset.tab === tab);
  });
  var detailsDiv = document.getElementById("taskPanelDetails");
  var changesDiv = document.getElementById("taskPanelChanges");
  if (detailsDiv) detailsDiv.style.display = tab === "details" ? "" : "none";
  if (changesDiv) changesDiv.style.display = tab === "changes" ? "" : "none";
  // Lazy-load diff on first visit to Changes tab
  if (tab === "changes" && !_taskPanelDiffLoaded && _panelTask !== null) {
    _taskPanelDiffLoaded = true;
    var diffContent = document.getElementById("taskPanelDiffContent");
    if (diffContent) diffContent.innerHTML = '<div class="diff-empty">Loading diff...</div>';
    fetch("/teams/" + _currentTeam + "/tasks/" + _panelTask + "/diff")
      .then(function (res) { return res.json(); })
      .then(function (diffData) {
        _taskPanelDiffRaw = flattenDiffDict(diffData.diff);
        switchTaskPanelDiffTab("files");
      })
      .catch(function () {
        var dc = document.getElementById("taskPanelDiffContent");
        if (dc) dc.innerHTML = '<div class="diff-empty">Failed to load diff</div>';
      });
  }
}

// =====================================================================
// Task activity log
// =====================================================================
async function loadTaskActivity(taskId, task) {
  const events = [];
  // 1. Task creation
  if (task.created_at) {
    events.push({ type: "created", time: task.created_at, text: "Task created", icon: "\u2795" });
  }
  // 2. Assignment
  if (task.assignee) {
    events.push({ type: "assignment", time: task.created_at, text: "Assigned to " + cap(task.assignee), icon: "\uD83D\uDC64" });
  }
  // 3. Current status (if not todo — we don't have full history in v1)
  if (task.status && task.status !== "todo") {
    events.push({ type: "status", time: task.updated_at, text: "Status: " + fmtStatus(task.status), icon: "\uD83D\uDD04" });
  }
  // 4. Chat messages mentioning this task
  try {
    const msgs = await fetch("/teams/" + _currentTeam + "/messages").then(function (r) { return r.json(); });
    const taskRef = "T" + String(taskId).padStart(4, "0");
    for (const m of msgs) {
      if (m.type === "chat" && m.content && m.content.indexOf(taskRef) !== -1) {
        events.push({
          type: "mention",
          time: m.timestamp,
          text: "Mentioned by " + cap(m.sender),
          icon: "\uD83D\uDCAC"
        });
      }
    }
  } catch (e) { }
  // Sort chronologically (oldest first)
  events.sort(function (a, b) { return (a.time || "").localeCompare(b.time || ""); });
  return events;
}

function renderTaskActivity(events) {
  if (!events || !events.length) return '<div class="diff-empty">No activity yet</div>';
  let html = "";
  for (const e of events) {
    html +=
      '<div class="task-activity-event">' +
      '<span class="task-activity-icon">' + e.icon + '</span>' +
      '<span class="task-activity-text">' + esc(e.text) + '</span>' +
      '<span class="task-activity-time">' + _fmtRelativeTime(e.time) + '</span>' +
      '</div>';
  }
  return html;
}

function toggleTaskActivity() {
  const arrow = document.getElementById("taskActivityArrow");
  const list = document.getElementById("taskActivityList");
  if (!arrow || !list) return;
  const expanded = arrow.classList.toggle("expanded");
  list.classList.toggle("expanded", expanded);
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
    const tasksRes = await fetch("/teams/" + _currentTeam + "/tasks");
    const allTasks = await tasksRes.json();
    const task = allTasks.find((t) => t.id === taskId);
    if (!task) {
      document.getElementById("taskPanelBody").innerHTML = '<div class="diff-empty">Task not found</div>';
      return;
    }
    // Fetch stats
    let stats = null;
    try {
      const sRes = await fetch("/teams/" + _currentTeam + "/tasks/" + taskId + "/stats");
      if (sRes.ok) stats = await sRes.json();
    } catch (e) { }
    // Populate header
    document.getElementById("taskPanelTitle").textContent = task.title;
    document.getElementById("taskPanelStatus").innerHTML =
      '<span class="badge badge-' + task.status + '">' + fmtStatus(task.status) + '</span>';
    document.getElementById("taskPanelAssignee").textContent =
      task.assignee ? cap(task.assignee) : "";
    document.getElementById("taskPanelPriority").textContent =
      task.priority ? cap(task.priority) : "";
    // ---- Build Details tab content ----
    let details = "";
    // Metadata grid
    details += '<div class="task-panel-meta-grid">';
    details += '<div class="task-panel-meta-item"><div class="task-detail-label">DRI</div><div class="task-detail-value">' + (task.dri ? cap(task.dri) : "\u2014") + '</div></div>';
    details += '<div class="task-panel-meta-item"><div class="task-detail-label">Assignee</div><div class="task-detail-value">' + (task.assignee ? cap(task.assignee) : "\u2014") + '</div></div>';
    details += '<div class="task-panel-meta-item"><div class="task-detail-label">Priority</div><div class="task-detail-value">' + cap(task.priority) + '</div></div>';
    details += '<div class="task-panel-meta-item"><div class="task-detail-label">Time</div><div class="task-detail-value">' + (stats ? fmtElapsed(stats.elapsed_seconds) : "\u2014") + '</div></div>';
    details += '</div>';
    // Stats row
    if (stats) {
      details += '<div class="task-panel-meta-grid">';
      details += '<div class="task-panel-meta-item"><div class="task-detail-label">Tokens (in/out)</div><div class="task-detail-value">' + fmtTokens(stats.total_tokens_in, stats.total_tokens_out) + '</div></div>';
      details += '<div class="task-panel-meta-item"><div class="task-detail-label">Cost</div><div class="task-detail-value">' + fmtCost(stats.total_cost_usd) + '</div></div>';
      details += '</div>';
    }
    // Dates
    details += '<div class="task-panel-dates">';
    details += '<span>Created: <span class="ts" data-ts="' + (task.created_at || "") + '">' + fmtTimestamp(task.created_at) + '</span></span>';
    details += '<span>Updated: <span class="ts" data-ts="' + (task.updated_at || "") + '">' + fmtTimestamp(task.updated_at) + '</span></span>';
    if (task.completed_at) {
      details += '<span>Completed: <span class="ts" data-ts="' + task.completed_at + '">' + fmtTimestamp(task.completed_at) + '</span></span>';
    }
    details += '</div>';
    // Dependencies
    if (task.depends_on && task.depends_on.length) {
      details += '<div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">Depends on: ';
      task.depends_on.forEach(function (d) {
        const depStatus = (task._dep_statuses && task._dep_statuses[d]) || "todo";
        details += '<span class="task-link" data-task-id="' + d + '" onclick="event.stopPropagation();openTaskPanel(' + d + ')"><span class="badge badge-' + depStatus + '" style="font-size:11px;margin-right:4px;cursor:pointer">T' + String(d).padStart(4, "0") + '</span></span>';
      });
      details += '</div>';
    }
    // Description
    if (task.description) {
      details += '<div class="task-panel-section"><div class="task-panel-section-label">Description</div>';
      details += '<div class="task-panel-desc md-content">' + linkifyFilePaths(linkifyTaskRefs(renderMarkdown(task.description))) + '</div>';
      details += '</div>';
    }
    // Attachments
    if (task.attachments && task.attachments.length) {
      details += '<div class="task-panel-section"><div class="task-panel-section-label">Attachments</div>';
      details += '<div class="task-attachments">';
      task.attachments.forEach(function (fpath) {
        var fname = fpath.split("/").pop();
        var isImage = /\.(png|jpe?g|gif|svg|webp)$/i.test(fname);
        details += '<div class="task-attachment">';
        if (isImage) {
          details += '<span class="task-attachment-icon">\uD83D\uDDBC\uFE0F</span>';
        } else {
          details += '<span class="task-attachment-icon">\uD83D\uDCCE</span>';
        }
        details += '<span class="task-attachment-name clickable-file" onclick="event.stopPropagation();openFilePanel(\'' + esc(fpath).replace(/'/g, "\\'") + '\')">' + esc(fname) + '</span>';
        details += '</div>';
      });
      details += '</div></div>';
    }
    // Activity section (collapsible, default collapsed)
    details += '<div class="task-activity-section">';
    details += '<div class="task-activity-header" onclick="toggleTaskActivity()">';
    details += '<span class="task-activity-arrow" id="taskActivityArrow">\u25B6</span>';
    details += '<span class="task-panel-section-label" style="margin-bottom:0">Activity</span>';
    details += '</div>';
    details += '<div class="task-activity-list" id="taskActivityList">';
    details += '<div class="diff-empty">Loading activity...</div>';
    details += '</div>';
    details += '</div>';
    // Approval status badge (read-only in Details; action buttons are in Changes tab)
    details += renderTaskApprovalBadge(task);

    // ---- Build Changes tab content ----
    let changes = "";
    // VCS info (branch + commits summary)
    if (stats && stats.branch) {
      changes += '<div class="task-panel-vcs-row">';
      changes += '<span class="task-branch" title="' + esc(stats.branch) + '">' + esc(stats.branch) + '</span>';
      if (stats.commits && typeof stats.commits === 'object') {
        var allCommits = [];
        if (Array.isArray(stats.commits)) {
          allCommits = stats.commits;
        } else {
          Object.keys(stats.commits).forEach(function (repo) {
            (stats.commits[repo] || []).forEach(function (c) { allCommits.push(c); });
          });
        }
        allCommits.forEach(function (c) {
          changes += '<span class="diff-panel-commit">' + esc(String(c).substring(0, 7)) + '</span>';
        });
      }
      changes += '</div>';
    }
    // Base SHA
    if (task.base_sha && typeof task.base_sha === 'object') {
      var baseShas = Object.entries(task.base_sha);
      if (baseShas.length) {
        changes += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">Base SHA: ';
        baseShas.forEach(function (entry) {
          changes += '<code style="font-family:SF Mono,Fira Code,monospace;background:var(--bg-active);padding:2px 6px;border-radius:3px;margin-right:6px">';
          if (baseShas.length > 1) changes += esc(entry[0]) + ': ';
          changes += esc(String(entry[1]).substring(0, 10)) + '</code>';
        });
        changes += '</div>';
      }
    } else if (task.base_sha && typeof task.base_sha === 'string') {
      changes += '<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">Base SHA: <code style="font-family:SF Mono,Fira Code,monospace;background:var(--bg-active);padding:2px 6px;border-radius:3px">' + esc(task.base_sha.substring(0, 10)) + '</code></div>';
    }
    // Diff section (loaded lazily when this tab is first activated)
    changes += '<div class="task-panel-diff-section" id="taskPanelDiffSection">';
    changes += '<div class="task-panel-diff-tabs"><button class="diff-tab active" data-dtab="files" onclick="switchTaskPanelDiffTab(\'files\')">Files Changed</button><button class="diff-tab" data-dtab="diff" onclick="switchTaskPanelDiffTab(\'diff\')">Full Diff</button><button class="diff-tab" data-dtab="commits" onclick="switchTaskPanelDiffTab(\'commits\')">Commits</button></div>';
    changes += '<div id="taskPanelDiffContent"><div class="diff-empty">Click this tab to load diff</div></div>';
    changes += '</div>';
    // Approve/Reject actions below the diff (GitHub-style review box)
    changes += renderTaskApprovalActions(task);

    // ---- Assemble body with both tab panes ----
    let body = '<div id="taskPanelDetails">' + details + '</div>';
    body += '<div id="taskPanelChanges" style="display:none">' + changes + '</div>';
    document.getElementById("taskPanelBody").innerHTML = body;

    // Reset tab state
    _taskPanelActiveTab = "details";
    _taskPanelDiffLoaded = false;
    _taskPanelDiffRaw = "";
    _taskPanelDiffTab = "files";
    _taskPanelCommitsData = null;

    // Ensure tab buttons reflect initial state
    document.querySelectorAll("#taskPanelTabs .task-panel-tab").forEach(function (btn) {
      btn.classList.toggle("active", btn.dataset.tab === "details");
    });

    // Load activity asynchronously
    loadTaskActivity(taskId, task).then(function (events) {
      const actList = document.getElementById("taskActivityList");
      if (actList) actList.innerHTML = renderTaskActivity(events);
    }).catch(function () {
      const actList = document.getElementById("taskActivityList");
      if (actList) actList.innerHTML = '<div class="diff-empty">Failed to load activity</div>';
    });
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
  _taskPanelDiffLoaded = false;
  _taskPanelActiveTab = "details";
  _taskPanelCommitsData = null;
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
  const searchQuery = (document.getElementById("chatFilterSearch").value || "").toLowerCase().trim();
  const params = new URLSearchParams();
  if (!showEvents) params.set("type", "chat");
  if (!_currentTeam) return;
  const res = await fetch(
    "/teams/" + _currentTeam + "/messages" + (params.toString() ? "?" + params : "")
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

  // Pre-seed filter dropdowns with all agents (with role labels)
  if (_currentTeam) {
    try {
      const agRes = await fetch("/teams/" + _currentTeam + "/agents");
      const agList = await agRes.json();
      // Merge agent names with any senders/recipients from messages
      const allNames = new Set([...senders, ...recipients, ...agList.map((a) => a.name)]);
      const roleMap = {};
      for (const a of agList) roleMap[a.name] = a.role || "worker";
      const makeOpts = (names) =>
        '<option value="">Anyone</option>' +
        [...names]
          .sort()
          .map((n) => {
            const role = roleMap[n];
            const label = role ? `${cap(n)} (${role})` : cap(n);
            return `<option value="${n}">${label}</option>`;
          })
          .join("");
      fromSel.innerHTML = makeOpts(allNames);
      toSel.innerHTML = makeOpts(allNames);
    } catch (e) {
      // Fallback to message-based population
      if (fromSel.options.length <= 1 || toSel.options.length <= 1) {
        fromSel.innerHTML =
          '<option value="">Anyone</option>' +
          [...senders].sort().map((n) => `<option value="${n}">${cap(n)}</option>`).join("");
        toSel.innerHTML =
          '<option value="">Anyone</option>' +
          [...recipients].sort().map((n) => `<option value="${n}">${cap(n)}</option>`).join("");
      }
    }
  } else if (fromSel.options.length <= 1 || toSel.options.length <= 1) {
    fromSel.innerHTML =
      '<option value="">Anyone</option>' +
      [...senders].sort().map((n) => `<option value="${n}">${cap(n)}</option>`).join("");
    toSel.innerHTML =
      '<option value="">Anyone</option>' +
      [...recipients].sort().map((n) => `<option value="${n}">${cap(n)}</option>`).join("");
  }
  fromSel.value = prevFrom;
  toSel.value = prevTo;

  // Filter by direction: arrow toggle controls one-way vs bidirectional
  const between = _chatFilterDirection === "bidi" && !!(filterFrom && filterTo);
  if (filterFrom || filterTo) {
    msgs = msgs.filter((m) => {
      if (m.type === "event") return true;
      if (between)
        return (
          (m.sender === filterFrom && m.recipient === filterTo) ||
          (m.sender === filterTo && m.recipient === filterFrom)
        );
      if (filterFrom && m.sender !== filterFrom) return false;
      if (filterTo && m.recipient !== filterTo) return false;
      return true;
    });
  }
  // Client-side search filter on message content
  if (searchQuery) {
    msgs = msgs.filter((m) => {
      const text = (m.content || "").toLowerCase();
      return text.indexOf(searchQuery) !== -1;
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
        return `<div class="msg-event"><div class="msg-event-icon"><svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M1 7a6 6 0 1 0 2-4.5"/><polyline points="1 1 1 3.5 3.5 3.5"/></svg></div><span class="msg-event-text">${agentifyRefs(linkifyFilePaths(linkifyTaskRefs(esc(m.content))))}</span><span class="msg-event-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div>`;
      const c = avatarColor(m.sender);
      return `<div class="msg"><div class="msg-avatar" style="background:${c}">${avatarInitial(m.sender)}</div><div class="msg-body"><div class="msg-header"><span class="msg-sender" style="cursor:pointer" onclick="openAgentPanel('${m.sender}')">${cap(m.sender)}</span><span class="msg-recipient">\u2192 ${cap(m.recipient)}</span><span class="msg-time ts" data-ts="${m.timestamp}">${fmtTimestamp(m.timestamp)}</span></div><div class="msg-content md-content">${linkifyFilePaths(linkifyTaskRefs(renderMarkdown(m.content)))}</div></div></div>`;
    })
    .join("");
  if (wasNearBottom) log.scrollTop = log.scrollHeight;

  // Populate recipient dropdown — all agents, managers first with (manager) label
  if (!_currentTeam) return;
  const agentsRes = await fetch("/teams/" + _currentTeam + "/agents");
  const agents = await agentsRes.json();
  const sel = document.getElementById("recipient");
  const prev = sel.value;
  const managers = agents.filter((a) => a.role === "manager").sort((a, b) => a.name.localeCompare(b.name));
  const others = agents.filter((a) => a.role !== "manager").sort((a, b) => a.name.localeCompare(b.name));
  sel.innerHTML =
    managers.map((a) => `<option value="${a.name}">${cap(a.name)} (manager)</option>`).join("") +
    others.map((a) => `<option value="${a.name}">${cap(a.name)} (${a.role || "worker"})</option>`).join("");
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
const _roleBadgeMap = {
  worker: "Worker",
  manager: "Manager",
  qa: "QA",
  design: "Design",
  backend: "Backend",
  frontend: "Frontend",
};

function _fmtTokensShort(n) {
  if (n == null || n === 0) return "0";
  if (n >= 1000000) return (n / 1000000).toFixed(1) + "M";
  if (n >= 1000) return (n / 1000).toFixed(1) + "K";
  return String(n);
}

function _fmtDuration(sec) {
  if (sec == null || sec === 0) return "\u2014";
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (h > 0) return h + "h " + m + "m";
  return m + "m";
}

/**
 * Shared helper: compute relative time parts from an ISO timestamp.
 * Returns { sec, min, hr, days } or null if iso is falsy.
 */
function _relativeTimeParts(iso) {
  if (!iso) return null;
  const sec = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  const min = Math.floor(sec / 60);
  const hr = Math.floor(min / 60);
  const days = Math.floor(hr / 24);
  return { sec, min, hr, days };
}

function _fmtRelativeTime(iso) {
  const t = _relativeTimeParts(iso);
  if (!t) return "";
  if (t.sec < 60) return "Just now";
  if (t.min < 60) return t.min + "m ago";
  if (t.hr < 24) return t.hr + "h ago";
  return t.days + "d ago";
}

function _fmtRelativeTimeShort(iso) {
  const t = _relativeTimeParts(iso);
  if (!t) return "";
  if (t.sec < 60) return "<1m";
  if (t.min < 60) return t.min + "m";
  if (t.hr < 24) return t.hr + "h";
  return t.days + "d";
}

/**
 * Determine agent activity dot class based on last activity time.
 * Returns "dot-active", "dot-stale", "dot-stuck", or "dot-offline".
 */
function getAgentDotClass(agent, tasks, agentStats) {
  if (!agent.pid) return "dot-offline";
  // Determine last activity timestamp
  const assignedTask = tasks.find(function (t) {
    return t.assignee === agent.name && t.status === "in_progress";
  });
  const taskUpdated = assignedTask ? new Date(assignedTask.updated_at) : null;
  const lastActive = (agentStats && agentStats.last_active) ? new Date(agentStats.last_active) : null;
  // Use the most recent timestamp available
  const timestamps = [taskUpdated, lastActive].filter(Boolean);
  if (timestamps.length === 0) return "dot-active"; // just started, no data yet
  const mostRecent = new Date(Math.max.apply(null, timestamps));
  const minutesAgo = (Date.now() - mostRecent.getTime()) / 60000;
  if (minutesAgo <= 5) return "dot-active";
  if (minutesAgo <= 30) return "dot-stale";
  return "dot-stuck";
}

/**
 * Get tooltip text for an agent dot.
 */
function getAgentDotTooltip(dotClass, agent, tasks) {
  if (dotClass === "dot-offline") return "Offline";
  var assignedTask = tasks.find(function (t) {
    return t.assignee === agent.name && t.status === "in_progress";
  });
  var lastTs = assignedTask && assignedTask.updated_at ? assignedTask.updated_at : null;
  var timeStr = lastTs ? _fmtRelativeTime(lastTs) : "";
  if (dotClass === "dot-active") return "Active" + (timeStr ? " \u2014 last activity " + timeStr : "");
  if (dotClass === "dot-stale") return "May be stuck" + (timeStr ? " \u2014 last activity " + timeStr : "");
  if (dotClass === "dot-stuck") return "Likely stuck" + (timeStr ? " \u2014 last activity " + timeStr : "");
  return "";
}

async function loadAgents() {
  if (!_currentTeam) return;
  let agentsRes, tasksRes;
  try {
    [agentsRes, tasksRes] = await Promise.all([
      fetch("/teams/" + _currentTeam + "/agents"),
      fetch("/teams/" + _currentTeam + "/tasks"),
    ]);
  } catch (e) {
    return;
  }
  if (!agentsRes.ok) return;
  const agents = await agentsRes.json();
  const tasks = tasksRes.ok ? await tasksRes.json() : [];

  // Fetch stats for all agents in parallel
  const statsMap = {};
  await Promise.all(
    agents.map(async (a) => {
      try {
        const r = await fetch("/teams/" + _currentTeam + "/agents/" + a.name + "/stats");
        if (r.ok) statsMap[a.name] = await r.json();
      } catch (e) { }
    })
  );

  // Build lookup: agent -> current in_progress task
  const inProgressTasks = tasks.filter((t) => t.status === "in_progress");

  // Count tasks done today per agent
  const now = new Date();
  const oneDayAgo = new Date(now - 24 * 60 * 60 * 1000);
  const doneTodayByAgent = {};
  for (const t of tasks) {
    if (t.completed_at && new Date(t.completed_at) > oneDayAgo && t.status === "done" && t.assignee) {
      doneTodayByAgent[t.assignee] = (doneTodayByAgent[t.assignee] || 0) + 1;
    }
  }

  const el = document.getElementById("agents");
  el.innerHTML = agents
    .map((a) => {
      const stats = statsMap[a.name] || {};
      const currentTask = inProgressTasks.find((t) => t.assignee === a.name);
      const roleBadge = _roleBadgeMap[a.role] || cap(a.role || "worker");
      const doneToday = doneTodayByAgent[a.name] || 0;

      // Activity-based dot classification
      const sidebarDot = getAgentDotClass(a, tasks, statsMap[a.name]);
      // Map sidebar dot class to agent-card dot class
      const dotClass = "agent-card-" + sidebarDot;
      const dotTooltip = getAgentDotTooltip(sidebarDot, a, tasks);
      const statusLabel = sidebarDot === "dot-offline" ? "Offline" : (currentTask ? sidebarDot.replace("dot-", "") : "Idle");

      // Row 1: Identity
      const taskLink = currentTask
        ? '<span class="agent-card-task-link" onclick="event.stopPropagation();openTaskPanel(' + currentTask.id + ')">T' + String(currentTask.id).padStart(4, "0") + '</span>'
        : '<span class="agent-card-idle-label">' + statusLabel + '</span>';

      // Row 2: Activity
      let activityText = "Offline";
      if (a.pid && currentTask) {
        activityText = "Working on " + esc(currentTask.title);
      } else if (a.pid) {
        activityText = "Idle";
      } else if (a.unread_inbox > 0) {
        activityText = a.unread_inbox + " message" + (a.unread_inbox !== 1 ? "s" : "") + " waiting";
      }

      // Last message preview from inbox (we don't have it here, use updated_at from tasks)
      let lastActivity = "";
      const agentTasks = tasks.filter((t) => t.assignee === a.name).sort((x, y) => (y.updated_at || "").localeCompare(x.updated_at || ""));
      if (agentTasks.length > 0 && agentTasks[0].updated_at) {
        lastActivity = "Last active " + _fmtRelativeTime(agentTasks[0].updated_at);
      }

      // Row 3: Stats
      const totalTokens = (stats.total_tokens_in || 0) + (stats.total_tokens_out || 0);
      const cost = stats.total_cost_usd != null ? "$" + Number(stats.total_cost_usd).toFixed(2) : "$0.00";
      const agentTime = _fmtDuration(stats.agent_time_seconds);

      return '<div class="agent-card-rich" onclick="openAgentPanel(\'' + a.name + '\')">' +
        '<div class="agent-card-row1">' +
        '<span class="agent-card-dot ' + dotClass + '" title="' + esc(dotTooltip) + '"></span>' +
        '<span class="agent-card-name">' + cap(a.name) + '</span>' +
        '<span class="agent-card-role badge-role-' + (a.role || "worker") + '">' + roleBadge + '</span>' +
        '<span class="agent-card-task-col">' + taskLink + '</span>' +
        '</div>' +
        '<div class="agent-card-row2">' +
        '<span class="agent-card-activity">' + esc(activityText) + '</span>' +
        (lastActivity ? '<span class="agent-card-last-active">' + esc(lastActivity) + '</span>' : '') +
        '</div>' +
        '<div class="agent-card-row3">' +
        '<span class="agent-card-stat"><span class="agent-card-stat-value">' + doneToday + '</span><span class="agent-card-stat-label">done today</span></span>' +
        '<span class="agent-card-stat"><span class="agent-card-stat-value">' + _fmtTokensShort(totalTokens) + '</span><span class="agent-card-stat-label">tokens</span></span>' +
        '<span class="agent-card-stat"><span class="agent-card-stat-value">' + cost + '</span><span class="agent-card-stat-label">cost</span></span>' +
        '<span class="agent-card-stat"><span class="agent-card-stat-value">' + agentTime + '</span><span class="agent-card-stat-label">uptime</span></span>' +
        '</div>' +
        '</div>';
    })
    .join("");
}

// =====================================================================
// Sidebar
// =====================================================================
async function loadSidebar() {
  try {
    if (!_currentTeam) return;
    const [tasksRes, agentsRes] = await Promise.all([
      fetch("/teams/" + _currentTeam + "/tasks"),
      fetch("/teams/" + _currentTeam + "/agents"),
    ]);
    const tasks = await tasksRes.json();
    const agents = await agentsRes.json();
    updateKnownAgents(agents);
    const statsMap = {};
    await Promise.all(
      (agents || []).map(async (a) => {
        try {
          const r = await fetch(
            "/teams/" + _currentTeam + "/agents/" + a.name + "/stats"
          );
          if (r.ok) statsMap[a.name] = await r.json();
        } catch (e) { }
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
        t.status === "todo" ||
        t.status === "in_progress" ||
        t.status === "in_review"
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
    // Render simple stat rows
    document.getElementById("sidebarStatusContent").innerHTML =
      '<div class="sidebar-stat-row"><span class="sidebar-stat-label">Done today</span><span class="stat-value">' + doneToday + '</span></div>' +
      '<div class="sidebar-stat-row"><span class="sidebar-stat-label">Active tasks</span><span class="stat-value">' + openCount + '</span></div>' +
      '<div class="sidebar-stat-row"><span class="sidebar-stat-label">Spent lifetime</span><span class="stat-value">$' + totalCost.toFixed(2) + '</span></div>';
    // ---- Action Required widget ----
    // Tasks where the boss is the current assignee (i.e. waiting on human action)
    const actionItems = tasks.filter(function (t) {
      return t.assignee && t.assignee.toLowerCase() === _bossName.toLowerCase() &&
        t.status !== "done";
    }).sort(function (a, b) {
      return (a.updated_at || "").localeCompare(b.updated_at || "");
    });
    const actionCountEl = document.getElementById("sidebarActionCount");
    const actionListEl = document.getElementById("sidebarActionList");
    const actionSeeAll = document.querySelector("#sidebarActions .sidebar-see-all");
    if (actionItems.length === 0) {
      if (actionCountEl) actionCountEl.style.display = "none";
      if (actionListEl) actionListEl.innerHTML =
        '<div class="sidebar-action-empty">' +
        '<span style="color:var(--accent-green)">\u2713</span> ' +
        '<span>No items need your attention</span></div>';
      if (actionSeeAll) actionSeeAll.style.display = "none";
    } else {
      if (actionCountEl) {
        actionCountEl.style.display = "";
        actionCountEl.textContent = actionItems.length;
      }
      if (actionSeeAll) actionSeeAll.style.display = "";
      let actionHtml = "";
      for (const t of actionItems) {
        const tid = "T" + String(t.id).padStart(4, "0");
        const icon = t.status === "in_approval" ? "\uD83D\uDD00" : (t.status === "in_review" ? "\uD83D\uDC41" : "\u26A1");
        const timeWaiting = _fmtRelativeTime(t.updated_at);
        actionHtml +=
          '<div class="sidebar-action-row" onclick="openTaskPanel(' + t.id + ')">' +
          '<span class="sidebar-action-type-icon">' + icon + '</span>' +
          '<span class="sidebar-task-id">' + tid + '</span>' +
          '<span class="sidebar-action-desc">' + esc(t.title) + '</span>' +
          '<span class="sidebar-action-time">' + timeWaiting + '</span>' +
          '</div>';
      }
      if (actionListEl) actionListEl.innerHTML = actionHtml;
    }
    // Sort agents: online (any non-offline dot) above offline, alphabetical within each group
    var sortedAgents = (agents || []).slice().sort(function (a, b) {
      var aOnline = a.pid ? 0 : 1;
      var bOnline = b.pid ? 0 : 1;
      if (aOnline !== bOnline) return aOnline - bOnline;
      return (a.name || "").localeCompare(b.name || "");
    });
    let agentHtml = "";
    for (const a of sortedAgents) {
      // Activity-based dot classification
      var dotClass = getAgentDotClass(a, tasks, statsMap[a.name]);
      var dotTooltip = getAgentDotTooltip(dotClass, a, tasks);
      // Determine current task display
      var currentTask = tasks.find(function (t) {
        return t.assignee === a.name && t.status === "in_progress";
      });
      var taskDisplay;
      if (currentTask) {
        var tid = "T" + String(currentTask.id).padStart(4, "0");
        taskDisplay =
          '<span class="sidebar-task-id">' + tid + '</span>' +
          '<span class="sidebar-agent-task-sep">\u2014</span> ' +
          esc(currentTask.title);
      } else if (a.pid) {
        taskDisplay = '<span class="sidebar-agent-idle">Idle</span>';
      } else {
        taskDisplay = '<span class="sidebar-agent-offline">Offline</span>';
      }
      // Last active time (only if agent has PID)
      var lastActiveDisplay = "";
      if (a.pid) {
        var assignedTask = tasks.find(function (t) { return t.assignee === a.name; });
        if (assignedTask && assignedTask.updated_at) {
          lastActiveDisplay = _fmtRelativeTimeShort(assignedTask.updated_at);
        }
      }
      agentHtml +=
        '<div class="sidebar-agent-row" onclick="openAgentPanel(\'' + a.name + '\')">' +
        '<span class="sidebar-agent-dot ' + dotClass + '" title="' + esc(dotTooltip) + '"></span>' +
        '<span class="sidebar-agent-name">' + cap(a.name) + '</span>' +
        '<span class="sidebar-agent-activity">' + taskDisplay + '</span>' +
        (lastActiveDisplay ? '<span class="sidebar-agent-time">' + lastActiveDisplay + '</span>' : '') +
        '</div>';
    }
    document.getElementById("sidebarAgentList").innerHTML = agentHtml;
    // Task heuristic: Tier 0 in_approval, Tier 1 in_progress+in_review, Tier 2 todo, Tier 3 done (max 3). Never show rejected.
    function taskTier(t) {
      if (t.status === "in_approval") return 0;
      if (t.status === "in_progress" || t.status === "in_review") return 1;
      if (t.status === "todo") return 2;
      if (t.status === "done") return 3;
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
        '<div class="sidebar-task-row" onclick="openTaskPanel(' +
        t.id +
        ')"><span class="sidebar-task-dot dot-' + t.status + '" title="' + fmtStatus(t.status) + '"></span>' +
        '<span class="sidebar-task-id">' +
        tid +
        '</span><span class="sidebar-task-name">' +
        (t.assignee ? cap(t.assignee) : "") +
        '</span><span class="sidebar-task-title">' +
        esc(t.title) +
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
    const res = await fetch("/teams/" + _currentTeam + "/tasks/" + taskId + "/diff");
    const data = await res.json();
    // Show branch name (merge_base/merge_tip are now dicts)
    document.getElementById("diffPanelBranch").textContent =
      data.branch || "no branch";
    document.getElementById("diffPanelCommits").innerHTML = flattenCommitsDict(
      data.commits
    )
      .map(
        (c) =>
          '<span class="diff-panel-commit">' +
          esc(String(c).substring(0, 7)) +
          "</span>"
      )
      .join("");
    _diffRawText = flattenDiffDict(data.diff);
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
/**
 * Return a WhatsApp-style delivery indicator:
 *   \u2713        single grey check  = delivered
 *   \u2713\u2713       double grey checks = seen by agent
 *   \u2713\u2713 blue  double blue checks = processed (agent finished turn)
 */
function msgStatusIcon(m) {
  if (m.processed_at) return '<span class="msg-status msg-processed" title="Processed">\u2713\u2713</span>';
  if (m.seen_at) return '<span class="msg-status msg-seen" title="Seen">\u2713\u2713</span>';
  if (m.delivered_at) return '<span class="msg-status msg-delivered" title="Delivered">\u2713</span>';
  return '<span class="msg-status msg-pending" title="Sending\u2026">\u23F3</span>';
}

function renderAgentInbox(msgs) {
  if (!msgs || !msgs.length)
    return '<div class="diff-empty">No messages</div>';
  return msgs
    .map(
      (m) =>
        '<div class="agent-msg' +
        (m.processed_at ? "" : " unread") +
        '"><div class="agent-msg-header"><span class="agent-msg-sender">' +
        esc(cap(m.sender)) +
        '</span><span class="agent-msg-time">' +
        fmtTimestamp(m.time) +
        " " + msgStatusIcon(m) +
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
        '<div class="agent-msg"><div class="agent-msg-header"><span class="agent-msg-sender">\u2192 ' +
        esc(cap(m.recipient)) +
        '</span><span class="agent-msg-time">' +
        fmtTimestamp(m.time) +
        " " + msgStatusIcon(m) +
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
  } catch (e) { }
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
// File viewer panel (re-uses the diff slide-over)
// =====================================================================
let _filePanelPath = "";

function _fileExtension(path) {
  const dot = path.lastIndexOf(".");
  return dot !== -1 ? path.substring(dot + 1).toLowerCase() : "";
}

function _fileBreadcrumb(path) {
  var parts = path.split("/");
  return parts.map(function (p, i) {
    if (i === parts.length - 1) return '<span class="file-breadcrumb-current">' + esc(p) + '</span>';
    return '<span class="file-breadcrumb-dir">' + esc(p) + '</span>';
  }).join('<span class="file-breadcrumb-sep">/</span>');
}

async function openFilePanel(filePath) {
  // Close task panel if open
  if (_panelTask !== null) closeTaskPanel();
  _panelMode = "file";
  _panelAgent = null;
  _filePanelPath = filePath;
  _agentTabData = {};
  _diffRawText = "";
  const panel = document.getElementById("diffPanel");
  const backdrop = document.getElementById("diffBackdrop");
  // Set up header
  document.getElementById("diffPanelTitle").innerHTML = _fileBreadcrumb(filePath);
  document.getElementById("diffPanelBranch").textContent = "";
  document.getElementById("diffPanelCommits").innerHTML = "";
  document.getElementById("diffPanelCommits").style.display = "none";
  // Hide the tabs for file viewer
  var tabsEl = panel.querySelector(".diff-panel-tabs");
  tabsEl.innerHTML = "";
  // Loading state
  document.getElementById("diffPanelBody").innerHTML = '<div class="diff-empty">Loading file...</div>';
  panel.classList.add("open");
  backdrop.classList.add("open");
  try {
    // The API expects path relative to shared/, but we receive paths like "shared/specs/foo.md"
    // Strip the leading "shared/" prefix since the API base is already the shared dir
    var apiPath = filePath;
    if (apiPath.startsWith("shared/")) apiPath = apiPath.substring(7);
    var res = await fetch("/teams/" + _currentTeam + "/files/content?path=" + encodeURIComponent(apiPath));
    if (!res.ok) {
      var err = await res.json().catch(function () { return {}; });
      document.getElementById("diffPanelBody").innerHTML = '<div class="diff-empty">' + esc(err.detail || "Failed to load file") + '</div>';
      return;
    }
    var data = await res.json();
    // Show modified time in subtitle
    document.getElementById("diffPanelBranch").textContent = data.modified ? "Modified " + fmtTimestamp(data.modified) : "";
    // Render content based on file extension
    var ext = _fileExtension(filePath);
    var body = document.getElementById("diffPanelBody");
    if (ext === "md" || ext === "markdown") {
      body.innerHTML = '<div class="file-viewer-content md-content">' + renderMarkdown(data.content) + '</div>';
    } else {
      body.innerHTML = '<div class="file-viewer-content"><pre class="file-viewer-code"><code>' + esc(data.content) + '</code></pre></div>';
    }
  } catch (e) {
    document.getElementById("diffPanelBody").innerHTML = '<div class="diff-empty">Failed to load file</div>';
  }
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
    const res = await fetch("/teams/" + _currentTeam + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
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
    if (_teamDropdownOpen) { _closeTeamDropdown(); return; }
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
_restoreChatFilters();
_restoreTaskFilters();

// Fetch app config (boss name) then bootstrap
fetch("/config").then(r => r.ok ? r.json() : {}).then(cfg => {
  if (cfg.boss_name) _bossName = cfg.boss_name;
}).catch(() => { }).finally(() => {
  loadTeams().then(() => {
    initFromHash();
    loadSidebar();
  });
});

// =====================================================================
// Expose functions to global scope for inline HTML event handlers
// =====================================================================
Object.assign(window, {
  switchTab,
  onTeamChange,
  _toggleTeamDropdown,
  _closeTeamDropdown,
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
  openFilePanel,
  toggleTaskActivity,
  onChatSearchInput,
  onChatFilterChange,
  toggleFilterArrow,
  onTaskSearchInput,
  onTaskFilterChange,
});
