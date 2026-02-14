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
  if (!r.ok) return [];
  const data = await r.json();
  // Backend returns [{name, team_id, agent_count, task_count, human_count, created_at}, ...]
  if (data.length > 0 && typeof data[0] === "object") {
    return data;  // Return full objects
  }
  // Fallback for plain string arrays
  return data.map(name => ({ name, agent_count: 0, task_count: 0, human_count: 0 }));
}

export async function fetchTasks(team) {
  const r = await fetch(`/teams/${team}/tasks`);
  return r.ok ? r.json() : [];
}

export async function fetchAllTasks() {
  const r = await fetch(`/api/tasks?team=all`);
  return r.ok ? r.json() : [];
}

export async function fetchAgents(team) {
  const r = await fetch(`/teams/${team}/agents`);
  return r.ok ? r.json() : [];
}

export async function fetchAgentsCrossTeam() {
  const r = await fetch("/api/agents?team=all");
  return r.ok ? r.json() : [];
}

export async function fetchMessages(team, params) {
  // Filter out undefined/null params
  const cleanParams = {};
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== undefined && value !== null) {
        cleanParams[key] = value;
      }
    }
  }
  const qs = Object.keys(cleanParams).length ? "?" + new URLSearchParams(cleanParams).toString() : "";
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

export async function greetTeam(team, lastSeen = null) {
  const url = lastSeen
    ? `/teams/${team}/greet?last_seen=${encodeURIComponent(lastSeen)}`
    : `/teams/${team}/greet`;
  const r = await fetch(url, {
    method: "POST",
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

export async function fetchTaskMergePreview(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/merge-preview`);
  return r.ok ? r.json() : { diff: {}, branch: "" };
}

export async function fetchTaskCommits(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/commits`);
  return r.ok ? r.json() : { commit_diffs: {} };
}

export async function fetchTaskStats(team, taskId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchTaskActivity(team, taskId, limit = 50) {
  const url = `/teams/${team}/tasks/${taskId}/activity${limit ? `?limit=${limit}` : ''}`;
  const r = await fetch(url);
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

export async function updateReviewComment(team, taskId, commentId, body) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reviews/comments/${commentId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function deleteReviewComment(team, taskId, commentId) {
  const r = await fetch(`/teams/${team}/tasks/${taskId}/reviews/comments/${commentId}`, {
    method: "DELETE",
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

export async function fetchFileContent(team, path, opts = {}) {
  const r = await fetch(`/teams/${team}/files/content?path=${encodeURIComponent(path)}`, { signal: opts.signal });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || "Failed to load file");
  }
  return r.json();
}

// --- Magic Commands ---

export async function execShell(team, command, cwd) {
  const r = await fetch(`/teams/${team}/exec/shell`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, cwd: cwd || undefined }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function saveCommand(team, command, result) {
  const r = await fetch(`/teams/${team}/commands`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ command, result }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// --- Global task endpoints (no team context needed) ---

export async function fetchTaskStatsGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchTaskDiffGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/diff`);
  return r.ok ? r.json() : { diff: {}, branch: "", commits: {} };
}

export async function fetchTaskActivityGlobal(taskId, limit = 50) {
  const url = `/api/tasks/${taskId}/activity${limit ? `?limit=${limit}` : ''}`;
  const r = await fetch(url);
  return r.ok ? r.json() : [];
}

export async function fetchTaskCommentsGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/comments`);
  return r.ok ? r.json() : [];
}

export async function postTaskCommentGlobal(taskId, author, body) {
  const r = await fetch(`/api/tasks/${taskId}/comments`, {
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

export async function fetchTaskMergePreviewGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/merge-preview`);
  return r.ok ? r.json() : { diff: {}, branch: "" };
}

export async function fetchTaskCommitsGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/commits`);
  return r.ok ? r.json() : { commit_diffs: {} };
}

export async function retryMergeGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/retry-merge`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function cancelTaskGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/cancel`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function fetchReviewsGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews`);
  return r.ok ? r.json() : [];
}

export async function fetchCurrentReviewGlobal(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/current`);
  return r.ok ? r.json() : { attempt: 0, verdict: null, summary: "", comments: [] };
}

export async function postReviewCommentGlobal(taskId, { file, line, body }) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/comments`, {
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

export async function updateReviewCommentGlobal(taskId, commentId, body) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/comments/${commentId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function deleteReviewCommentGlobal(taskId, commentId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/comments/${commentId}`, {
    method: "DELETE",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function approveTaskGlobal(taskId, summary = "") {
  const r = await fetch(`/api/tasks/${taskId}/approve`, {
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

export async function rejectTaskGlobal(taskId, reason, summary = "") {
  const r = await fetch(`/api/tasks/${taskId}/reject`, {
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
