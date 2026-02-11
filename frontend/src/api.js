/**
 * Centralized API fetch functions.
 * Each function returns parsed JSON (or throws).
 */

export async function fetchConfig() {
  const r = await fetch("/config");
  return r.ok ? r.json() : {};
}

export async function fetchTeams() {
  const r = await fetch("/teams");
  return r.ok ? r.json() : [];
}

export async function fetchTasks(team) {
  const r = await fetch(`/teams/${team}/tasks`);
  return r.ok ? r.json() : [];
}

export async function fetchAgents(team) {
  const r = await fetch(`/teams/${team}/agents`);
  return r.ok ? r.json() : [];
}

export async function fetchMessages(team, params) {
  const qs = params ? "?" + new URLSearchParams(params).toString() : "";
  const r = await fetch(`/teams/${team}/messages${qs}`);
  return r.ok ? r.json() : [];
}

export async function sendMessage(team, recipient, content) {
  const r = await fetch(`/teams/${team}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ recipient, content }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function fetchTaskDiff(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/diff`);
  return r.ok ? r.json() : { diff: {}, branch: "", commits: {} };
}

export async function fetchTaskCommits(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/commits`);
  return r.ok ? r.json() : { commit_diffs: {} };
}

export async function fetchTaskStats(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchTaskActivity(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/activity`);
  return r.ok ? r.json() : [];
}

// --- Task Comments ---

export async function fetchTaskComments(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/comments`);
  return r.ok ? r.json() : [];
}

export async function postTaskComment(team, taskId, author, body) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ author, body }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// --- Reviews ---

export async function fetchReviews(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reviews`);
  return r.ok ? r.json() : [];
}

export async function fetchCurrentReview(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reviews/current`);
  return r.ok ? r.json() : { attempt: 0, verdict: null, summary: "", comments: [] };
}

export async function postReviewComment(team, taskId, { file, line, body }) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reviews/comments`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file, line: line || null, body }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// --- Retry Merge ---

export async function retryMerge(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/retry-merge`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// --- Approve / Reject ---

export async function approveTask(team, taskId, summary = "") {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ summary }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function rejectTask(team, taskId, reason, summary = "") {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reject`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ reason: reason || "(no reason)", summary: summary || reason || "(no reason)" }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function fetchAgentTab(team, agentName, tab) {
  const r = await fetch(`/teams/${team}/agents/${agentName}/${tab}`);
  return r.ok ? r.json() : null;
}

export async function fetchAgentStats(team, agentName) {
  const r = await fetch(`/teams/${team}/agents/${agentName}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchFileContent(team, path) {
  const r = await fetch(`/teams/${team}/files/content?path=${encodeURIComponent(path)}`);
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to load file");
  }
  return r.json();
}
