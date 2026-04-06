/**
 * Centralized API fetch functions.
 * Each function returns parsed JSON (or throws).
 */

export async function fetchConfig() {
  const r = await fetch("/config");
  return r.ok ? r.json() : {};
}

export async function fetchBootstrap(team = null) {
  const qs = team ? `?team=${encodeURIComponent(team)}` : "";
  const r = await fetch(`/bootstrap${qs}`);
  return r.ok ? r.json() : null;
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

export async function fetchMergeOrder(team) {
  const r = await fetch(`/teams/${team}/tasks/merge-order`);
  return r.ok ? r.json() : { order: [] };
}

export async function fetchAllTasks() {
  const r = await fetch(`/api/tasks?team=all`);
  return r.ok ? r.json() : [];
}

export async function fetchTask(taskId) {
  const r = await fetch(`/api/tasks/${taskId}`);
  return r.ok ? r.json() : null;
}

export async function fetchAgents(team) {
  const r = await fetch(`/teams/${team}/agents`);
  return r.ok ? r.json() : [];
}

export async function fetchAgentsCrossTeam() {
  const r = await fetch("/api/agents?team=all");
  return r.ok ? r.json() : [];
}

export async function fetchActiveTurns() {
  const r = await fetch("/turns/active");
  return r.ok ? r.json() : [];
}

export async function fetchAgentActivity(team, agentName, n = 100) {
  const r = await fetch(`/teams/${team}/agents/${agentName}/activity?n=${n}`);
  return r.ok ? r.json() : [];
}

export async function addAgent(team, name, options = {}) {
  const r = await fetch(`/teams/${team}/agents/add`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, ...options }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
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
  const ctrl = new AbortController();
  const timeout = setTimeout(() => ctrl.abort(), 10000); // 10s timeout
  try {
    const r = await fetch(`/teams/${team}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ recipient, content }),
      signal: ctrl.signal,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      throw new Error(err.detail || r.statusText);
    }
    return r.json();
  } catch (e) {
    if (e.name === "AbortError") throw new Error("Message send timed out — please try again");
    throw e;
  } finally {
    clearTimeout(timeout);
  }
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

// --- Task endpoints: Use global /api/tasks/{id}/... versions below ---

export async function fetchAgentTab(team, agentName, tab) {
  const r = await fetch(`/teams/${team}/agents/${agentName}/${tab}`);
  return r.ok ? r.json() : null;
}

export async function fetchAgentStats(team, agentName) {
  const r = await fetch(`/teams/${team}/agents/${agentName}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchAllAgentStats(team) {
  const r = await fetch(`/teams/${team}/agents/stats`);
  return r.ok ? r.json() : {};
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

export async function fetchDefaultCwd(team) {
  const r = await fetch(`/teams/${team}/default-cwd`);
  if (!r.ok) return null;
  const data = await r.json();
  return data.cwd || null;
}

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

export async function fetchCostSummary(team) {
  const r = await fetch(`/teams/${team}/cost-summary`);
  return r.ok ? r.json() : null;
}

// --- Global task endpoints (no team context needed) ---

export async function fetchTaskDiff(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/diff`);
  return r.ok ? r.json() : { diff: {}, branch: "" };
}

export async function fetchTaskStats(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/stats`);
  return r.ok ? r.json() : null;
}

export async function fetchTaskActivity(taskId, limit = 50) {
  const url = `/api/tasks/${taskId}/activity${limit ? `?limit=${limit}` : ''}`;
  const r = await fetch(url);
  return r.ok ? r.json() : [];
}

export async function fetchTaskComments(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/comments`);
  return r.ok ? r.json() : [];
}

export async function postTaskComment(taskId, author, body) {
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

export async function fetchTaskMergePreview(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/merge-preview`);
  return r.ok ? r.json() : { diff: {}, branch: "" };
}

export async function fetchTaskCommits(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/commits`);
  return r.ok ? r.json() : { commit_diffs: {} };
}

export async function retryMerge(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/retry-merge`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function cancelTask(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/cancel`, {
    method: "POST",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function fetchReviews(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews`);
  return r.ok ? r.json() : [];
}

export async function fetchCurrentReview(taskId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/current`);
  return r.ok ? r.json() : { attempt: 0, verdict: null, summary: "", comments: [] };
}

export async function postReviewComment(taskId, { file, line, body }) {
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

export async function updateReviewComment(taskId, commentId, body) {
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

export async function deleteReviewComment(taskId, commentId) {
  const r = await fetch(`/api/tasks/${taskId}/reviews/comments/${commentId}`, {
    method: "DELETE",
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function approveTask(taskId, summary = "") {
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

export async function rejectTask(taskId, reason, summary = "") {
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

// --- Reviewer edit endpoints ---

export async function getTaskFile(taskId, path) {
  const res = await fetch(`/api/tasks/${taskId}/file?path=${encodeURIComponent(path)}`);
  if (res.status === 404) return null;
  if (!res.ok) throw new Error(await res.text());
  return res.json(); // { content, head_sha }
}

export async function postReviewerEdits(taskId, edits) {
  const res = await fetch(`/api/tasks/${taskId}/reviewer-edits`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ edits }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw Object.assign(new Error(body.error || 'Failed'), { status: res.status, body });
  }
  return res.json(); // { new_sha }
}

// --- Project creation ---

export async function createProject({ name, repoPath, agentCount = 2, model = "sonnet" }) {
  const r = await fetch("/projects", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, repo_path: repoPath, agent_count: agentCount, model }),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

export async function deleteProject(name) {
  const r = await fetch(`/projects/${encodeURIComponent(name)}`, { method: "DELETE" });
  if (!r.ok) {
    const err = await r.json().catch(() => ({}));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// --- Reviewer ---

export async function fetchReviewer(team) {
  const r = await fetch(`/teams/${team}/reviewer`);
  return r.ok ? r.json() : { mode: "human", threshold: 3.5 };
}

export async function setReviewer(team, config) {
  const r = await fetch(`/teams/${team}/reviewer`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  return r.ok ? r.json() : null;
}

// Deprecated — kept for backwards compat
export async function fetchAutoApprover(team) {
  const r = await fetch(`/teams/${team}/auto-approver`);
  return r.ok ? r.json() : { enabled: false, threshold: 3.5 };
}

export async function setAutoApprover(team, config) {
  const r = await fetch(`/teams/${team}/auto-approver`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  return r.ok ? r.json() : null;
}

// --- Task freeze ---

export async function fetchTaskFreeze(team) {
  const r = await fetch(`/teams/${team}/task-freeze`);
  return r.ok ? r.json() : { enabled: false };
}

export async function setTaskFreeze(team, config) {
  const r = await fetch(`/teams/${team}/task-freeze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  return r.ok ? r.json() : null;
}

// --- Max tasks limit ---

export async function fetchMaxTasks(team) {
  const r = await fetch(`/teams/${team}/max-tasks`);
  return r.ok ? r.json() : { enabled: false, limit_in_progress: 5, limit_queued: 10 };
}

export async function setMaxTasks(team, config) {
  const r = await fetch(`/teams/${team}/max-tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(config),
  });
  return r.ok ? r.json() : null;
}

// --- Version ---

export async function fetchVersion() {
  const res = await fetch('/api/version');
  if (!res.ok) return null;
  return res.json(); // { current, latest, update_available }
}

// --- File completion endpoints ---

// Complete filesystem paths (global, for shell -d argument).
// Returns entries: [{ path, is_dir, has_git }]
export async function completeFiles(path) {
  const res = await fetch(`/api/files/complete?path=${encodeURIComponent(path)}&limit=20`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.entries || [];
}

// Complete paths within a task's worktree (for reviewer file editor).
// Returns entries: [{ path, is_dir, has_git }]
export async function completeTaskFiles(taskId, q) {
  const res = await fetch(`/api/tasks/${taskId}/files/complete?q=${encodeURIComponent(q)}&limit=20`);
  if (!res.ok) return [];
  const data = await res.json();
  return data.entries || [];
}
// --- Agent actions ---

export async function nudgeAgent(agent, team) {
  const r = await fetch(`/api/agents/${agent}/nudge?team=${encodeURIComponent(team)}`, { method: "POST" });
  if (!r.ok) { const err = await r.json().catch(() => ({})); throw new Error(err.detail || r.statusText); }
  return r.json();
}

export async function interruptAgent(agent, team) {
  const r = await fetch(`/api/agents/${agent}/interrupt?team=${encodeURIComponent(team)}`, { method: "POST" });
  if (!r.ok) { const err = await r.json().catch(() => ({})); throw new Error(err.detail || r.statusText); }
  return r.json();
}

// --- File Upload ---

export async function uploadFiles(team, files, onProgress) {
  const formData = new FormData();
  for (const file of files) {
    formData.append("files", file);
  }

  const xhr = new XMLHttpRequest();
  return new Promise((resolve, reject) => {
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable && onProgress) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(JSON.parse(xhr.responseText));
      } else {
        reject(new Error(xhr.responseText || `Upload failed: ${xhr.status}`));
      }
    };
    xhr.onerror = () => reject(new Error("Upload failed: network error"));
    xhr.open("POST", `/teams/${team}/uploads`);
    xhr.send(formData);
  });
}
