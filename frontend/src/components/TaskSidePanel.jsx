import { forwardRef } from "preact/compat";
import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import { effect } from "@preact/signals";
import {
  tasks, taskPanelId, knownAgentNames, humanName,
  panelStack, pushPanel, closeAllPanels, popPanel, taskTeamFilter, currentTeam,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, prettyName, esc, fmtStatus, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  fmtRelativeTime, taskIdStr, registerTaskDisplayIds, renderMarkdown, linkifyTaskRefs, linkifyFilePaths,
  agentifyRefs, flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse,
  stripEmojis, handleCopyClick, toApiPath, fmtCompactDuration, displayName,
} from "../utils.js";
import { ReviewableDiff } from "./ReviewableDiff.jsx";
import { ReviewerEditModal } from "./ReviewerEditModal.jsx";
import { showToast } from "../toast.js";
import { CopyBtn } from "./CopyBtn.jsx";

// ── Live timer hook ──
// Returns a compact elapsed-time string (e.g. "42s", "5m") updated every second.
// Pass null/undefined to get null (used to hide the timer).
function useLiveTimer(startIso) {
  const [elapsed, setElapsed] = useState(() =>
    startIso ? fmtCompactDuration(Date.now() - new Date(startIso).getTime()) : null
  );

  useEffect(() => {
    if (!startIso) { setElapsed(null); return; }
    const tick = () => setElapsed(fmtCompactDuration(Date.now() - new Date(startIso).getTime()));
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [startIso]);

  return elapsed;
}

// ── Per-task stale-while-revalidate cache ──
// Keyed by taskId → { stats, diffRaw, mergePreviewRaw, currentReview, oldComments, activityRaw }
// Data is served from cache instantly on panel open, then revalidated in the background.
// Note: Task IDs are globally unique, so no team prefix needed.
const _cache = new Map();
function _cacheKey(id) { return `${id}`; }
function _getCache(id) { return _cache.get(_cacheKey(id)) || {}; }
function _setCache(id, patch) {
  const key = _cacheKey(id);
  _cache.set(key, { ...(_cache.get(key) || {}), ...patch });
}

// ── Cache invalidation (called from SSE handlers) ──
export function invalidateTaskCache(taskId) {
  _cache.delete(_cacheKey(taskId));
}

// ── Seed cache before opening a panel ──
// Call this with the task object you already have (e.g. from the task list)
// BEFORE calling openPanel().  Guarantees the first render never shows
// "Loading..." — immune to @preact/signals v2 hook-state loss.
export function seedTaskCache(taskId, taskObj) {
  if (taskId != null && taskObj) {
    _setCache(taskId, { task: taskObj });
  }
}

// ── Background prefetch for recent tasks ──
// Proactively fetches and caches stats+reviews for task IDs to warm the cache.
// Processes tasks sequentially to avoid hammering the server.
// Skips tasks that already have cached stats.
export async function prefetchTaskPanelData(taskIds) {
  if (!taskIds || taskIds.length === 0) return;

  for (const id of taskIds) {
    const cached = _getCache(id);
    // Skip if already cached
    if (cached.stats) continue;

    try {
      // Fetch stats and current review (but not full reviews list, diff, or activity)
      const [stats, currentReview] = await Promise.all([
        api.fetchTaskStats(id).catch(() => null),
        api.fetchCurrentReview(id).catch(() => null),
      ]);

      const cacheUpdate = {};
      if (stats) cacheUpdate.stats = stats;
      if (currentReview) cacheUpdate.currentReview = currentReview;

      if (Object.keys(cacheUpdate).length > 0) {
        _setCache(id, cacheUpdate);
      }
    } catch (e) {
      // Silently skip failures — prefetch is best-effort
    }
  }
}

// ── Prefetch all tab data for a single task (called from SSE) ──
// Eagerly refreshes cache for all tabs when a task updates via SSE.
// Fire-and-forget parallel fetches to ensure panel opens instantly.
export function prefetchTaskData(taskId) {
  if (!taskId) return;

  Promise.allSettled([
    api.fetchTaskStats(taskId).then(s => s && _setCache(taskId, { stats: s })),
    api.fetchTaskDiff(taskId).then(d => {
      if (!d) return;
      _setCache(taskId, { diffRaw: flattenDiffDict(d.diff) });
    }),
    api.fetchTaskActivity(taskId, 50).then(a => a && _setCache(taskId, { activityRaw: a })),
    api.fetchCurrentReview(taskId).then(r => r && _setCache(taskId, { currentReview: r })),
  ]).catch(() => {});  // allSettled won't reject, but safety net
}

// ── Panel title helper (for back-bar) ──
function panelTitle(entry, allTasks) {
  if (!entry) return "";
  if (entry.type === "task") {
    const t = (allTasks || []).find(t => t.id === entry.target);
    return taskIdStr(entry.target, t && t.prefix, t && t.seq) + (t ? " " + t.title : "");
  }
  if (entry.type === "agent") return cap(entry.target || "");
  if (entry.type === "file") return (entry.target || "").split("/").pop() || "File";
  return "";
}

// ── Module-level tab state (survives signal-driven remounts) ──
const _tabState = new Map();  // taskId -> { activeTab, visitedTabs }

// ── Event delegation for linked content ──
// Uses onClick prop (not useEffect+addEventListener) to avoid broken
// commit-phase hook scheduling with @preact/signals v2.
const LinkedDiv = forwardRef(function LinkedDiv({ html, class: cls, style }, ref) {
  const handler = useCallback((e) => {
    const copyBtn = e.target.closest(".copy-btn");
    if (copyBtn) { e.stopPropagation(); e.preventDefault(); handleCopyClick(copyBtn); return; }
    const taskLink = e.target.closest("[data-task-id]");
    if (taskLink) { e.stopPropagation(); pushPanel("task", parseInt(taskLink.dataset.taskId, 10)); return; }
    const taskSeqLink = e.target.closest("[data-task-seq]");
    if (taskSeqLink) {
      e.stopPropagation();
      const displayId = taskSeqLink.dataset.taskSeq;
      const match = (tasks.peek() || []).find(t => t.display_id === displayId);
      if (match) pushPanel("task", match.id);
      return;
    }
    const agentLink = e.target.closest("[data-agent-name]");
    if (agentLink) { e.stopPropagation(); pushPanel("agent", agentLink.dataset.agentName); return; }
    const fileLink = e.target.closest("[data-file-path]");
    if (fileLink) { e.stopPropagation(); pushPanel("file", fileLink.dataset.filePath); return; }
  }, []);

  return <div ref={ref} class={cls} style={style} onClick={handler} dangerouslySetInnerHTML={{ __html: html }} />;
});

// ── Retry merge button (compact, inline) ──
function RetryMergeButton({ task }) {
  const [loading, setLoading] = useState(false);

  const handleRetry = async () => {
    if (loading) return;
    setLoading(true);
    try {
      await api.retryMerge(task.id);
      // Refresh task list - task.team is available if needed
      if (task.team) {
        const refreshed = await api.fetchTasks(task.team);
        registerTaskDisplayIds(refreshed);
        tasks.value = refreshed;
      }
    } catch (err) {
      alert("Retry failed: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
      <button
      class="btn-approve"
        onClick={handleRetry}
        disabled={loading}
      style={{ marginLeft: "8px" }}
      >
      {loading ? "Retrying..." : "\u21BB Retry Merge"}
      </button>
  );
}

// ── Approval bar (fixed between header and tabs) ──
function ApprovalBar({ task, currentReview, onAction, onEdit }) {
  const [loading, setLoading] = useState(false);
  const [summary, setSummary] = useState("");
  const [result, setResult] = useState(null);

  const { status, approval_status, rejection_reason } = task;
  const reviewSummary = currentReview && currentReview.summary;
  const commentCount = currentReview && currentReview.comments ? currentReview.comments.length : 0;
  const isAutoReview = currentReview && currentReview.reviewer === "auto-approver";

  // Already approved
  if (status === "done" || approval_status === "approved" || result === "approved") {
    return (
      <div class="task-approval-bar task-approval-bar-resolved">
        <span class="approval-badge approval-badge-approved">&#10004; Approved</span>
        {isAutoReview && <span class="approval-badge" style={{ background: "rgba(99,102,241,0.10)", color: "var(--accent)" }}>AI Reviewed</span>}
        {(summary || reviewSummary) && (
          <span class="task-approval-bar-summary">{summary || reviewSummary}</span>
        )}
      </div>
    );
  }
  // Rejected
  if (status === "rejected" || approval_status === "rejected" || result === "rejected") {
    const reason = result === "rejected" ? summary : (reviewSummary || rejection_reason);
    return (
      <div class="task-approval-bar task-approval-bar-resolved">
        <span class="approval-badge approval-badge-rejected">&#10006; Rejected</span>
        {isAutoReview && <span class="approval-badge" style={{ background: "rgba(99,102,241,0.10)", color: "var(--accent)" }}>AI Reviewed</span>}
        {reason && <span class="task-approval-bar-summary">{reason}</span>}
      </div>
    );
  }
  // Merging
  if (status === "merging") {
    return (
      <div class="task-approval-bar task-approval-bar-resolved">
        <span class="approval-badge approval-badge-merging">&#8635; Merging...</span>
      </div>
    );
  }
  // Merge failed
  if (status === "merge_failed") {
    return (
      <div class="task-approval-bar task-approval-bar-resolved">
        <span class="approval-badge" style={{ background: "rgba(204,167,0,0.08)", color: "var(--semantic-orange)" }}>&#9888; Merge Failed</span>
        <RetryMergeButton task={task} />
      </div>
    );
  }
  // Not reviewable — only show bar for in_review and in_approval
  if (status !== "in_approval" && status !== "in_review") return null;

  const handleApprove = async () => {
    setLoading(true);
    try {
      await api.approveTask(task.id, summary);
      setResult("approved");
      if (onAction) onAction();
    } catch (e) {
      showToast("Failed to approve: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleReject = async () => {
    setLoading(true);
    try {
      await api.rejectTask(task.id, summary || "(no reason)", summary);
      setResult("rejected");
      if (onAction) onAction();
    } catch (e) {
      showToast("Failed to reject: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  // For in_review tasks, only the Edit button is shown (no Approve/Reject yet —
  // those are only available once the task moves to in_approval).
  if (status === "in_review") {
    return (
      <div class="task-approval-bar">
        <div class="task-approval-bar-actions">
          <button
            class="btn btn-secondary"
            disabled={loading}
            onClick={(e) => { e.stopPropagation(); if (onEdit) onEdit(); }}
          >
            Edit
          </button>
        </div>
      </div>
    );
  }

  return (
    <div class="task-approval-bar">
      <textarea
        class="task-approval-bar-input"
        placeholder="Review comment (optional)..."
        value={summary}
        onInput={(e) => {
          setSummary(e.target.value);
          e.target.style.height = 'auto';
          e.target.style.height = e.target.scrollHeight + 'px';
        }}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        rows="3"
      />
      <div class="task-approval-bar-actions">
        <button
          class="btn-approve"
          disabled={loading}
          onClick={(e) => { e.stopPropagation(); handleApprove(); }}
        >
          {loading ? "Approving..." : "\u2714 Approve"}
        </button>
        <button
          class="btn btn-secondary"
          disabled={loading}
          onClick={(e) => { e.stopPropagation(); if (onEdit) onEdit(); }}
        >
          Edit
        </button>
        <button
          class="btn-reject"
          disabled={loading}
          onClick={(e) => { e.stopPropagation(); handleReject(); }}
        >
          {loading ? "Rejecting..." : "\u2716 Request Changes"}
        </button>
        {commentCount > 0 && (
          <span class="task-approval-bar-comment-count">
            {commentCount} comment{commentCount !== 1 ? "s" : ""}
          </span>
        )}
      </div>
    </div>
  );
}

// ── Commit list ──
function CommitList({ commits, multiRepo }) {
  const [expandedIdx, setExpandedIdx] = useState({});
  const toggle = (idx) => { setExpandedIdx(prev => ({ ...prev, [idx]: !prev[idx] })); };

  return (
    <div class="commit-list">
      {commits.map((c, i) => {
        const shortSha = String(c.sha || "").substring(0, 7);
        const msg = c.message || "(no message)";
        const isOpen = expandedIdx[i];
        return (
          <div key={i} class="commit-item">
            <div class="commit-header" onClick={() => toggle(i)}>
              <span class="commit-expand-icon">{isOpen ? "\u25BC" : "\u25B6"}</span>
              <span class="commit-sha">{shortSha}</span>
              <span class="commit-message">{msg}</span>
              {multiRepo && c.repo && <span class="commit-repo-label">{c.repo}</span>}
            </div>
            {isOpen && (
              <div class="commit-diff">
                {c.diff && c.diff !== "(empty diff)" ? (
                  <ReviewableDiff
                    diffRaw={c.diff}
                    taskId={null}
                    currentComments={[]}
                    oldComments={[]}
                    isReviewable={false}
                    defaultCollapsed={true}
                  />
                ) : (
                  <div class="diff-empty">Empty diff</div>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Overview tab ──
function OverviewTab({ task, stats }) {
  const t = task;
  const descHtml = useMemo(() =>
    t.description ? linkifyFilePaths(linkifyTaskRefs(renderMarkdown(t.description))) : "",
    [t.description]
  );

  // Build depends-on pills
  const depPills = t.depends_on && t.depends_on.length > 0 ? (
    <span class="task-overview-dep-pills">
      {t.depends_on.map(d => {
        const depStatus = (t._dep_statuses && t._dep_statuses[d]) || "todo";
        return (
          <span
            key={d}
            class={"badge badge-" + depStatus + " task-overview-dep-pill"}
            onClick={(e) => { e.stopPropagation(); pushPanel("task", d); }}
          >
            {taskIdStr(d)}
          </span>
        );
      })}
    </span>
  ) : null;

  return (
    <div>
      {/* 2-column property grid */}
      <div class="task-overview-grid">
        <span class="task-overview-key">Assignee</span>
        <span class="task-overview-val">{t.assignee ? displayName(t.assignee) : "\u2014"}</span>

        <span class="task-overview-key">DRI</span>
        <span class="task-overview-val">{t.dri ? displayName(t.dri) : "\u2014"}</span>

        <span class="task-overview-key">Priority</span>
        <span class="task-overview-val">{t.priority ? cap(t.priority) : "\u2014"}</span>

        <span class="task-overview-key">Created</span>
        <span class="task-overview-val">{fmtTimestamp(t.created_at)}</span>

        <span class="task-overview-key">Updated</span>
        <span class="task-overview-val">{fmtTimestamp(t.updated_at)}</span>

        {depPills && (
          <>
            <span class="task-overview-key">Depends on</span>
            <span class="task-overview-val">{depPills}</span>
          </>
        )}
      </div>
      {/* Description */}
      {t.description && (
        <div class="task-panel-section">
          <div class="task-panel-section-label">Description</div>
          <LinkedDiv class="task-panel-desc md-content" html={descHtml} />
        </div>
      )}
      {/* Attachments */}
      {t.attachments && t.attachments.length > 0 && (
        <div class="task-panel-section">
          <div class="task-panel-section-label">Attachments</div>
          <div class="task-attachments">
            {t.attachments.map((fpath, i) => {
              const fname = fpath.split("/").pop();
              const isImage = /\.(png|jpe?g|gif|svg|webp)$/i.test(fname);
              return (
                <div key={i} class="task-attachment">
                  <span class="task-attachment-icon">{isImage ? "\uD83D\uDDBC\uFE0F" : "\uD83D\uDCCE"}</span>
                  <span
                    class="task-attachment-name clickable-file"
                    onClick={(e) => {
                      e.stopPropagation();
                      pushPanel("file", fpath);
                    }}
                  >
                    {fname}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {/* Status detail (merge failure reason etc.) */}
      {t.status_detail && (
        <div style={{ fontSize: "12px", color: "var(--text-muted)", padding: "8px 12px", background: "rgba(204,167,0,0.06)", borderRadius: "6px", marginBottom: "8px", border: "1px solid rgba(204,167,0,0.12)" }}>
          {t.status_detail}
        </div>
      )}
    </div>
  );
}

// ── Changes tab ──
function ChangesTab({ task, diffRaw, currentReview, oldComments, stats }) {
  const [showFileList, setShowFileList] = useState(false);
  const [commitsData, setCommitsData] = useState(null);
  const [commitsExpanded, setCommitsExpanded] = useState(false);
  const [commitsLoading, setCommitsLoading] = useState(false);
  const t = task;
  const isReviewable = t && t.status === "in_approval";

  if (diffRaw === null) return <div class="diff-empty">Loading changes...</div>;

  const files = useMemo(() => diffRaw ? diff2HtmlParse(diffRaw) : [], [diffRaw]);
  let totalAdd = 0, totalDel = 0;
  for (const f of files) { totalAdd += f.addedLines; totalDel += f.deletedLines; }

  // Lazy-load commits when expanded
  useEffect(() => {
    if (!commitsExpanded || commitsData !== null) return;
    setCommitsLoading(true);
    api.fetchTaskCommits(t.id).then(data => {
      if (!data) {
        setCommitsData({ commit_diffs: {} });
        return;
      }
      setCommitsData(data);
    }).catch((err) => {
      console.warn('Failed to fetch commits for task', t.id, err);
      setCommitsData({ commit_diffs: {} });
    }).finally(() => {
      setCommitsLoading(false);
    });
  }, [commitsExpanded, commitsData, t.id]);

  const allCommits = useMemo(() => {
    if (!commitsData) return [];
    const cd = commitsData.commit_diffs || {};
    const commits = [];
    Object.keys(cd).forEach(repo => {
      (cd[repo] || []).forEach(c => commits.push({ ...c, repo }));
    });
    return commits;
  }, [commitsData]);

  const multiRepo = commitsData ? Object.keys(commitsData.commit_diffs || {}).length > 1 : false;

  return (
    <div>
      {/* Branch info */}
      {stats && stats.branch && (
        <div class="task-panel-vcs-row">
          <span class="task-branch copyable" title={stats.branch}>{stats.branch}<CopyBtn text={stats.branch} /></span>
        </div>
      )}
      {/* Base SHA */}
      {t.base_sha && typeof t.base_sha === "object" && Object.keys(t.base_sha).length > 0 && (
        <div style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "12px" }}>
          Base:{" "}
          {Object.entries(t.base_sha).map(([repo, sha], i) => (
            <span key={i} class="task-base-sha-entry">
              <code style={{ fontFamily: "SF Mono,Fira Code,monospace", background: "var(--bg-active)", padding: "2px 6px", borderRadius: "3px" }}>
                {Object.keys(t.base_sha).length > 1 ? repo + ": " : ""}{String(sha).substring(0, 10)}
              </code>
              <CopyBtn text={String(sha)} />
            </span>
          ))}
        </div>
      )}
      {/* Engineering stats (Est / Tokens / Cost) */}
      {stats && (
        <div class="task-panel-meta-inline" style={{ marginBottom: "12px" }}>
          <span class="task-panel-meta-pair">
            <span class="task-panel-meta-label">Est:</span> {fmtElapsed(stats.elapsed_seconds)}
          </span>
          <span class="task-panel-meta-separator"> · </span>
          <span class="task-panel-meta-pair">
            <span class="task-panel-meta-label">Tokens:</span> {fmtTokens(stats.total_tokens_in, stats.total_tokens_out)}
          </span>
          <span class="task-panel-meta-separator"> · </span>
          <span class="task-panel-meta-pair">
            <span class="task-panel-meta-label">Cost:</span> {fmtCost(stats.total_cost_usd)}
          </span>
        </div>
      )}
      {/* File summary bar */}
      {files.length > 0 ? (
        <div class="changes-file-summary" onClick={() => setShowFileList(!showFileList)}>
          <span class="changes-file-toggle">{showFileList ? "\u25BC" : "\u25B6"}</span>
          <span>{files.length} file{files.length !== 1 ? "s" : ""} changed</span>
          <span class="changes-file-stats">
            <span style={{ color: "var(--diff-add-text)" }}>+{totalAdd}</span>
            {" "}
            <span style={{ color: "var(--diff-del-text)" }}>&minus;{totalDel}</span>
          </span>
        </div>
      ) : !diffRaw ? (
        <div class="diff-empty">No changes yet</div>
      ) : null}
      {/* Expandable file list */}
      {showFileList && files.length > 0 && (
        <div class="diff-file-list" style={{ marginBottom: "12px" }}>
          {files.map((f, i) => {
            const name = (f.newName === "/dev/null" ? f.oldName : f.newName) || f.oldName || "unknown";
            return (
              <div key={i} class="diff-file-list-item">
                <span class="diff-file-list-name">{name}</span>
                <span class="diff-file-stats">
                  <span class="diff-file-add">+{f.addedLines}</span>
                  <span class="diff-file-del">-{f.deletedLines}</span>
                </span>
              </div>
            );
          })}
        </div>
      )}
      {/* Reviewable diff */}
      {diffRaw ? (
        <ReviewableDiff
          diffRaw={diffRaw}
          taskId={t.id}
          currentComments={currentReview ? (currentReview.comments || []) : []}
          oldComments={oldComments || []}
          isReviewable={isReviewable}
          defaultCollapsed={true}
        />
      ) : null}
      {/* Commits (collapsible) */}
      <div class="changes-commits-section">
        <div class="changes-commits-header" onClick={() => setCommitsExpanded(!commitsExpanded)}>
          <span class="changes-commits-toggle">{commitsExpanded ? "\u25BC" : "\u25B6"}</span>
          <span>Commits</span>
        </div>
        {commitsExpanded && (
          (commitsLoading || commitsData === null)
            ? <div class="diff-empty">Loading commits...</div>
            : !allCommits.length
              ? <div class="diff-empty">No commits recorded</div>
              : <CommitList commits={allCommits} multiRepo={multiRepo} />
        )}
      </div>
    </div>
  );
}

// ── Merge Preview tab ──
function MergePreviewTab({ task, mergePreviewRaw, stats }) {
  const files = useMemo(() => mergePreviewRaw ? diff2HtmlParse(mergePreviewRaw) : [], [mergePreviewRaw]);
  let totalAdd = 0, totalDel = 0;
  for (const f of files) { totalAdd += f.addedLines; totalDel += f.deletedLines; }

  if (mergePreviewRaw === null) return <div class="diff-empty">Loading merge preview...</div>;

  return (
    <div>
      {/* Branch info */}
      {stats && stats.branch && (
        <div class="task-panel-vcs-row">
          <span class="task-branch copyable" title={stats.branch}>{stats.branch}<CopyBtn text={stats.branch} /></span>
          <span style={{ fontSize: "11px", color: "var(--text-muted)" }}>→ main</span>
        </div>
      )}
      {/* File summary */}
      {files.length > 0 ? (
        <div style={{ fontSize: "12px", color: "var(--text-muted)", marginBottom: "12px" }}>
          {files.length} file{files.length !== 1 ? "s" : ""} changed{" "}
          <span style={{ color: "var(--diff-add-text)" }}>+{totalAdd}</span>{" "}
          <span style={{ color: "var(--diff-del-text)" }}>&minus;{totalDel}</span>
        </div>
      ) : (
        <div class="diff-empty">No differences from main</div>
      )}
      {/* Full diff */}
      <ReviewableDiff
        diffRaw={mergePreviewRaw}
        taskId={task.id}
        currentComments={[]}
        oldComments={[]}
        isReviewable={false}
        defaultCollapsed={true}
      />
    </div>
  );
}

// ── Activity tab ──
function ActivityTab({ taskId, task, activityRaw, onLoadActivity }) {
  const [timeline, setTimeline] = useState(null);
  const [commentText, setCommentText] = useState("");
  const [posting, setPosting] = useState(false);
  const [showingAll, setShowingAll] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const human = humanName.peek() || "human";
  const agentNames = knownAgentNames.peek() || [];

  // Transform raw activity data into timeline format
  const transformActivity = useCallback((activity) => {
    return activity
      .filter((m) => m.type === "comment" || m.type === "event" || m.type === "task_comment")
      .map((m) => {
        if (m.type === "comment" || m.type === "task_comment") {
          return {
            type: "comment",
            time: m.timestamp,
            author: m.sender || "unknown",
            body: m.content || "",
            icon: "\u270E",
          };
        }
        const text = m.content || "Event";
        let icon = "\u21BB";
        if (/created/i.test(text)) icon = "+";
        else if (/assign/i.test(text)) icon = "\u2192";
        else if (/approved|merged/i.test(text)) icon = "\u2713";
        else if (/rejected/i.test(text)) icon = "\u2717";
        else if (/review/i.test(text)) icon = "\u2299";
        else if (/commented/i.test(text)) icon = "\u270E";
        return { type: "event", time: m.timestamp, text, icon };
      });
  }, []);

  // Update timeline when activityRaw prop changes (from cache or fresh fetch)
  // Note: We limit to 50 items by default (see loadActivity in parent component)
  // to avoid rendering hundreds of DOM nodes for tasks with extensive activity.
  // Users can click "Load earlier activity" to see the full timeline.
  useEffect(() => {
    if (activityRaw) {
      setTimeline(transformActivity(activityRaw));
      // If we got exactly 50 items (the default limit), there might be more
      setShowingAll(activityRaw.length < 50);
    }
  }, [activityRaw, transformActivity]);

  // Pre-compute comment HTML to avoid re-running markdown+linkification on every render
  // Step 1: Parse markdown (only depends on timeline)
  const timelineWithBaseHtml = useMemo(() => {
    if (!timeline) return null;
    return timeline.map((e) => {
      if (e.type === "comment") {
        const baseHtml = linkifyFilePaths(linkifyTaskRefs(renderMarkdown(e.body)));
        return { ...e, baseHtml };
      }
      return e;
    });
  }, [timeline]);

  // Step 2: Apply agent links (depends on baseHtml + agentNames)
  const timelineWithHtml = useMemo(() => {
    if (!timelineWithBaseHtml) return null;
    return timelineWithBaseHtml.map((e) => {
      if (e.baseHtml) {
        return { ...e, commentHtml: agentifyRefs(e.baseHtml, agentNames) };
      }
      return e;
    });
  }, [timelineWithBaseHtml, agentNames]);

  const handleLoadMore = async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      // Fetch without limit to get all activity
      const allActivity = await api.fetchTaskActivity(taskId, null);
      setTimeline(transformActivity(allActivity));
      setShowingAll(true);
    } catch (e) {
      showToast("Failed to load more activity: " + e.message, "error");
    } finally {
      setLoadingMore(false);
    }
  };

  const handlePostComment = async () => {
    const body = commentText.trim();
    if (!body || posting) return;
    setPosting(true);
    try {
      await api.postTaskComment(taskId, human, body);
      setCommentText("");
      // Trigger refresh from parent
      if (onLoadActivity) onLoadActivity();
    } catch (e) {
      showToast("Failed to post comment: " + e.message, "error");
    } finally {
      setPosting(false);
    }
  };

  return (
    <div class="task-activity-tab">
        {timelineWithHtml === null ? (
          <div class="diff-empty">Loading activity...</div>
        ) : timelineWithHtml.length === 0 ? (
          <div class="diff-empty">No activity yet</div>
        ) : (
        <>
          <div class="task-activity-timeline">
            {timelineWithHtml.map((e, i) => {
              if (e.type === "comment") {
                return (
                  <div key={i} class="task-activity-event task-comment-entry">
                    <span class="task-activity-icon">{e.icon}</span>
                    <div class="task-comment-body">
                      <div class="task-comment-meta">
                        <span class="task-comment-author">{cap(e.author)}</span>
                        <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
                      </div>
                      <LinkedDiv class="task-comment-text md-content" html={e.commentHtml} />
                    </div>
                  </div>
                );
              } else {
                return (
                  <div key={i} class="task-activity-event">
                    <span class="task-activity-icon">{e.icon}</span>
                    <span class="task-activity-text">{stripEmojis(e.text)}</span>
                    <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
                  </div>
                );
              }
            })}
          </div>
          {!showingAll && (
            <div style={{ textAlign: "center", padding: "12px" }}>
              <button
                class="btn-approve"
                onClick={handleLoadMore}
                disabled={loadingMore}
                style={{ fontSize: "12px", padding: "6px 12px" }}
              >
                {loadingMore ? "Loading..." : "Load earlier activity"}
              </button>
            </div>
          )}
        </>
      )}
      {/* Comment input */}
        <div class="task-comment-input-row">
          <input
            type="text"
            class="task-comment-input"
            placeholder="Add a comment..."
            value={commentText}
            onInput={(e) => setCommentText(e.target.value)}
            onKeyDown={(e) => { e.stopPropagation(); if (e.key === "Enter") handlePostComment(); }}
            onClick={(e) => e.stopPropagation()}
            disabled={posting}
          />
          <button
            class="task-comment-submit"
            onClick={(e) => { e.stopPropagation(); handlePostComment(); }}
            disabled={posting || !commentText.trim()}
          >
            {posting ? "..." : "\u2192"}
          </button>
      </div>
    </div>
  );
}

// ── Approve dialog (keyboard shortcut flow) ──
// Opened via Enter when task is in_approval. Cmd+Enter submits, Esc closes.
function ApproveDialog({ task, onClose, onAction }) {
  const [summary, setSummary] = useState("");
  const [loading, setLoading] = useState(false);
  const textareaRef = useRef(null);

  // Focus textarea on mount
  useEffect(() => {
    if (textareaRef.current) textareaRef.current.focus();
  }, []);

  const handleApprove = async () => {
    if (loading) return;
    setLoading(true);
    try {
      await api.approveTask(task.id, summary);
      if (onAction) onAction();
      onClose();
    } catch (e) {
      showToast("Failed to approve: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleReject = async () => {
    if (loading) return;
    setLoading(true);
    try {
      await api.rejectTask(task.id, summary || "(no reason)", summary);
      if (onAction) onAction();
      onClose();
    } catch (e) {
      showToast("Failed to reject: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e) => {
    e.stopPropagation();
    if (e.key === "Escape") { onClose(); return; }
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); handleApprove(); return; }
  };

  return (
    <div class="approve-dialog-backdrop" onClick={onClose}>
      <div class="approve-dialog" onClick={(e) => e.stopPropagation()} onKeyDown={handleKeyDown}>
        <div class="approve-dialog-header">
          <span class="approve-dialog-title">Approve task</span>
          <button class="approve-dialog-close" onClick={onClose}>&times;</button>
        </div>
        <textarea
          ref={textareaRef}
          class="approve-dialog-textarea"
          placeholder="Review comment (optional)..."
          value={summary}
          onInput={(e) => setSummary(e.target.value)}
          rows="4"
        />
        <div class="approve-dialog-hint">Cmd+Enter to approve · Esc to cancel</div>
        <div class="approve-dialog-actions">
          <button class="btn-approve" disabled={loading} onClick={handleApprove}>
            {loading ? "Approving..." : "\u2714 Approve"}
          </button>
          <button class="btn-reject" disabled={loading} onClick={handleReject}>
            {loading ? "Rejecting..." : "\u2716 Request Changes"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ── Main TaskSidePanel ──
export function TaskSidePanel() {
  const [id, setId] = useState(() => taskPanelId.peek());
  useEffect(() => {
    const dispose = effect(() => {
      const newId = taskPanelId.value;
      // Eagerly seed cache from tasks signal BEFORE the re-render.
      // tasks.peek() is subscription-free — immune to the v2 bug.
      if (newId !== null && !_getCache(newId).task) {
        const found = tasks.peek().find(x => x.id === newId);
        if (found) _setCache(newId, { task: found });
      }
      setId(newId);
    });
    return dispose;
  }, []);
  // NOTE: Do NOT read tasks.value here in the render body.
  // Doing so subscribes this component to the `tasks` signal,
  // which the polling loop updates every 2 s with a new array ref.
  // @preact/signals v2 can lose hook state on signal-driven re-renders
  // (the known commit-phase scheduling bug), resetting activeTab to
  // "overview" each time.  Instead, read tasks.value only inside
  // effects / callbacks (which don't create signal subscriptions)
  // and use tasks.peek() for the one render-time read (panelTitle).

  const [task, setTask] = useState(() => {
    const c = _getCache(id);
    return c.task || null;
  });
  const [stats, setStats] = useState(null);
  const [activeTab, setActiveTab] = useState(() => {
    const saved = _tabState.get(id);
    return saved ? saved.activeTab : "overview";
  });
  // Track which tabs have been visited — only mount a tab's component
  // after the user first navigates to it (lazy rendering).
  const [visitedTabs, setVisitedTabs] = useState(() => {
    const saved = _tabState.get(id);
    return saved ? saved.visitedTabs : { overview: true };
  });
  const [diffRaw, setDiffRaw] = useState(null);
  const [diffLoaded, setDiffLoaded] = useState(false);
  const [mergePreviewRaw, setMergePreviewRaw] = useState(null);
  const [mergePreviewLoaded, setMergePreviewLoaded] = useState(false);
  const [currentReview, setCurrentReview] = useState(null);
  const [oldComments, setOldComments] = useState([]);
  const [activityRaw, setActivityRaw] = useState(null);
  const [activityLoaded, setActivityLoaded] = useState(false);
  const [approveDialogOpen, setApproveDialogOpen] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);

  // Mark tab as visited when selected.
  // IMPORTANT: persist to _tabState synchronously FIRST, before queuing
  // any state setters.  @preact/signals v2 can trigger a commit-phase
  // re-render between the click and the state commit, and if the
  // component re-initialises hooks during that window it must find the
  // latest tab in _tabState.
  const switchTab = useCallback((tab) => {
    const prev = _tabState.get(id) || { visitedTabs: { overview: true } };
    _tabState.set(id, {
      activeTab: tab,
      visitedTabs: { ...prev.visitedTabs, [tab]: true },
    });
    setActiveTab(tab);
    setVisitedTabs(v => v[tab] ? v : { ...v, [tab]: true });
  }, [id]);

  // Load task data when panel opens — stale-while-revalidate.
  // If we have cached data for this task, show it immediately;
  // then always re-fetch in the background to ensure freshness.
  useEffect(() => {
    if (id === null) { setTask(null); return; }

    // ── Restore from cache (instant) ──
    const c = _getCache(id);
    setStats(c.stats ?? null);
    setDiffRaw(c.diffRaw ?? null);
    setDiffLoaded(!!c.diffRaw);
    setMergePreviewRaw(c.mergePreviewRaw ?? null);
    setMergePreviewLoaded(!!c.mergePreviewRaw);
    setCurrentReview(c.currentReview ?? null);
    setOldComments(c.oldComments ?? []);
    setActivityRaw(c.activityRaw ?? null);
    setActivityLoaded(!!c.activityRaw);

    // Restore tab state if we have it (survives signal-driven remounts)
    const saved = _tabState.get(id);
    if (saved) {
      setActiveTab(saved.activeTab);
      setVisitedTabs(saved.visitedTabs);
    } else {
      setActiveTab("overview");
      setVisitedTabs({ overview: true });
    }

    // ── Synchronous fallback from signal (instant when tasks already loaded) ──
    // Use peek() to avoid subscribing the component to the tasks signal.
    // A .value read inside useEffect can still create a subscription in
    // @preact/signals v2, leading to the commit-phase hook-state-loss bug.
    const cached = tasks.peek().find(t => t.id === id);
    if (cached) {
      setTask(cached);
      _setCache(id, { task: cached });
    }

    // ── Primary source: direct single-task fetch (immune to signal remounts) ──
    api.fetchTask(id).then(taskData => {
      if (taskData) {
        setTask(taskData);
        _setCache(id, { task: taskData });
      }
    }).catch(() => {});

    // ── Watchdog: retry if task is still null after 3 seconds ──
    // Guards against race conditions where the initial fetch is lost
    // due to signal-driven remounts or transient network errors.
    const watchdog = setTimeout(() => {
      setTask(prev => {
        if (prev !== null) return prev; // already loaded, no-op
        api.fetchTask(id).then(data => {
          if (data) {
            setTask(data);
            _setCache(id, { task: data });
          }
        }).catch(() => {});
        return prev;
      });
    }, 3000);

    // Parallelize stats + reviews loading
    (async () => {
      try {
        const [s, review, reviews] = await Promise.all([
          api.fetchTaskStats(id).catch(() => null),
          api.fetchCurrentReview(id).catch(() => null),
          api.fetchReviews(id).catch(() => []),
        ]);

        if (s) {
          setStats(s);
          _setCache(id, { stats: s });
        }

        if (review) {
          setCurrentReview(review);
          _setCache(id, { currentReview: review });
        }

        // Process old comments from reviews
        if (reviews.length > 1) {
          const latest = reviews[reviews.length - 1];
          const old = [];
          for (const r of reviews) {
            if (r.attempt !== latest.attempt && r.comments) {
              for (const c of r.comments) {
                old.push({ ...c, attempt: r.attempt });
              }
            }
          }
          setOldComments(old);
          _setCache(id, { oldComments: old });
        }
      } catch (e) { }

      // Eagerly start loading diff and activity in background (non-blocking)
      api.fetchTaskDiff(id).then(data => {
        const raw = flattenDiffDict(data.diff);
        setDiffRaw(raw);
        setDiffLoaded(true);
        _setCache(id, { diffRaw: raw });
      }).catch(() => {});

      api.fetchTaskActivity(id, 50).then(raw => {
        setActivityRaw(raw);
        setActivityLoaded(true);
        _setCache(id, { activityRaw: raw });
      }).catch(() => {
        setActivityRaw([]);
      });
    })();

    return () => clearTimeout(watchdog);
  }, [id]);

  // Sync task from signal when SSE pushes updates.
  // We subscribe to `tasks` via effect() (from @preact/signals) rather
  // than reading tasks.value in the render body, which would re-subscribe
  // the whole component and trigger signal-driven re-renders that can lose
  // hook state due to the v2 commit-phase scheduling bug.
  useEffect(() => {
    if (id === null) return;
    const dispose = effect(() => {
      const allTasks = tasks.value;  // auto-tracked by effect()
      const updated = allTasks.find(t => t.id === id);
      if (updated) {
        _setCache(id, { task: updated });
        setTask(prev => prev ? { ...prev, ...updated } : updated);
      }
    });
    return dispose;
  }, [id]);

  // Lazy load diff when Changes tab first visited — stale-while-revalidate
  useEffect(() => {
    if (!visitedTabs.changes || id === null) return;
    // If we already have cached data we showed it immediately above.
    // Always re-fetch to ensure freshness (unless this is the initial
    // load from a cold cache, which the diffLoaded flag already guards).
    if (diffLoaded && _getCache(id).diffRaw) {
      // Already showing stale data — revalidate in background
      api.fetchTaskDiff(id).then(data => {
        const raw = flattenDiffDict(data.diff);
        setDiffRaw(raw);
        _setCache(id, { diffRaw: raw });
      }).catch(() => {});
      return;
    }
    setDiffLoaded(true);
    api.fetchTaskDiff(id).then(data => {
      const raw = flattenDiffDict(data.diff);
      setDiffRaw(raw);
      _setCache(id, { diffRaw: raw });
    }).catch(() => {});
  }, [visitedTabs.changes, diffLoaded, id]);

  // Lazy load merge preview when Merge Preview tab first visited — stale-while-revalidate
  useEffect(() => {
    if (!visitedTabs.merge || id === null) return;
    if (mergePreviewLoaded && _getCache(id).mergePreviewRaw) {
      api.fetchTaskMergePreview(id).then(data => {
        const raw = flattenDiffDict(data.diff);
        setMergePreviewRaw(raw);
        _setCache(id, { mergePreviewRaw: raw });
      }).catch(() => {});
      return;
    }
    setMergePreviewLoaded(true);
    api.fetchTaskMergePreview(id).then(data => {
      const raw = flattenDiffDict(data.diff);
      setMergePreviewRaw(raw);
      _setCache(id, { mergePreviewRaw: raw });
    }).catch(() => {});
  }, [visitedTabs.merge, mergePreviewLoaded, id]);

  // Lazy load activity when Activity tab first visited — stale-while-revalidate
  const loadActivity = useCallback(() => {
    if (id === null) return;
    api.fetchTaskActivity(id, 50).then(raw => {
      setActivityRaw(raw);
      _setCache(id, { activityRaw: raw });
    }).catch(() => {
      setActivityRaw([]);
    });
  }, [id]);

  useEffect(() => {
    if (!visitedTabs.activity || id === null) return;
    if (activityLoaded && _getCache(id).activityRaw) {
      // Already showing stale data — revalidate in background
      loadActivity();
      return;
    }
    setActivityLoaded(true);
    loadActivity();
  }, [visitedTabs.activity, activityLoaded, id, loadActivity]);

  const close = useCallback(() => { closeAllPanels(); }, []);

  // ── Keyboard shortcuts (scoped to task panel when open) ──
  // Runs in capture phase to intercept before app.jsx bubble-phase handler.
  useEffect(() => {
    if (id === null) return;

    const PANEL_TABS = ["overview", "activity", "changes", "merge"];

    const handler = (e) => {
      const target = e.target;
      const inInput = target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT" || target.isContentEditable;

      // Esc: if approve dialog is open, close dialog and stop propagation so
      // app.jsx doesn't also close the panel.
      if (e.key === "Escape") {
        // Check current dialog state via ref to avoid stale closure
        const dialogEl = document.querySelector(".approve-dialog-backdrop");
        if (dialogEl) {
          e.stopPropagation();
          setApproveDialogOpen(false);
        }
        // Otherwise let app.jsx handle Esc (closes the panel)
        return;
      }

      // Tab: cycle through tabs (not when inside an input)
      if (e.key === "Tab" && !e.metaKey && !e.ctrlKey && !e.altKey && !inInput) {
        e.preventDefault();
        setActiveTab(prev => {
          const idx = PANEL_TABS.indexOf(prev);
          const next = PANEL_TABS[(idx + 1) % PANEL_TABS.length];
          // Update module-level tabState too (synchronously, before possible remount)
          const saved = _tabState.get(id) || { visitedTabs: { overview: true } };
          _tabState.set(id, {
            activeTab: next,
            visitedTabs: { ...saved.visitedTabs, [next]: true },
          });
          setVisitedTabs(v => v[next] ? v : { ...v, [next]: true });
          return next;
        });
        return;
      }

      // Enter: open approve dialog (only when task is in_approval, not in input)
      if (e.key === "Enter" && !e.metaKey && !e.ctrlKey && !e.altKey && !inInput) {
        // Read task status from module-level cache (avoids stale closure)
        const currentTask = _getCache(id)?.task;
        if (currentTask && currentTask.status === "in_approval") {
          e.preventDefault();
          setApproveDialogOpen(true);
        }
        return;
      }
    };

    document.addEventListener("keydown", handler, true); // capture phase: runs before app.jsx bubble handler
    return () => document.removeEventListener("keydown", handler, true);
  }, [id]);

  const handleAction = useCallback(() => {
    const filter = taskTeamFilter.value;
    if (filter === "all") {
      api.fetchAllTasks().then(list => { registerTaskDisplayIds(list); tasks.value = list; });
    } else if (filter === "current") {
      api.fetchTasks(currentTeam.value).then(list => { registerTaskDisplayIds(list); tasks.value = list; });
    } else if (task && task.team) {
      api.fetchTasks(task.team).then(list => { registerTaskDisplayIds(list); tasks.value = list; });
    }
  }, [task]);

  // Derive changed file paths from the loaded diff for the edit modal.
  // Recomputed only when diffRaw changes.
  const changedFiles = useMemo(() => {
    if (!diffRaw) return [];
    return diff2HtmlParse(diffRaw).map(f =>
      f.newName && f.newName !== "/dev/null" ? f.newName : f.oldName
    ).filter(Boolean);
  }, [diffRaw]);

  if (id === null) return null;

  const isOpen = id !== null;
  // Derive task from multiple sources — resilient to signal-driven
  // hook state loss (@preact/signals v2 commit-phase bug):
  //   1. React state (set by useEffect)
  //   2. Module-level cache (survives remounts)
  //   3. tasks signal via peek() (no subscription — immune to v2 bug)
  const t = task
    ?? _getCache(id)?.task
    ?? (id !== null ? tasks.peek().find(x => x.id === id) : null)
    ?? null;
  const TABS = ["overview", "activity", "changes", "merge"];
  const TAB_LABELS = { overview: "Overview", changes: "Changes", merge: "Merge Preview", activity: "Activity" };

  const stack = panelStack.peek();
  const hasPrev = stack.length > 1;
  const prev = hasPrev ? stack[stack.length - 2] : null;

  return (
    <>
      {approveDialogOpen && t && t.status === "in_approval" && (
        <ApproveDialog
          task={t}
          onClose={() => setApproveDialogOpen(false)}
          onAction={handleAction}
        />
      )}
      {showEditModal && t && (
        <ReviewerEditModal
          taskId={t.id}
          changedFiles={changedFiles}
          onDone={async (newSha) => {
            setShowEditModal(false);
            try {
              await api.approveTask(t.id, "");
              handleAction();
            } catch (e) {
              showToast("Failed to approve after edit: " + e.message, "error");
            }
          }}
          onDiscard={() => setShowEditModal(false)}
        />
      )}
      <div class={"task-panel" + (isOpen ? " open" : "")}>
        {/* Back bar */}
        {hasPrev && (
          <div class="panel-back-bar" onClick={popPanel}>
            <span class="panel-back-arrow">&larr;</span> Back to {panelTitle(prev, tasks.peek())}
          </div>
        )}
        {/* Header */}
        <div class="task-panel-header">
          <div class="task-panel-header-line-1">
            <span class="task-panel-id copyable">
              {taskIdStr(id, t && t.prefix, t && t.seq)}
              <CopyBtn text={taskIdStr(id, t && t.prefix, t && t.seq)} />
            </span>
            {taskTeamFilter.peek() === "all" && t && t.team && (
              <span class="task-team-name">{prettyName(t.team)}</span>
            )}
            <span class="task-panel-header-status">
              {t && <span class={"badge badge-" + t.status}>{fmtStatus(t.status)}</span>}
            </span>
          </div>
          <div class="task-panel-header-line-2">
            <span class="task-panel-title">{t ? t.title : "Loading..."}</span>
          </div>
          <button class="task-panel-close" onClick={close}>&times;</button>
        </div>
        {/* Approval bar (sticky, between header and tabs) */}
        {t && <ApprovalBar task={t} currentReview={currentReview} onAction={handleAction} onEdit={() => setShowEditModal(true)} />}
        {/* Tabs */}
        <div class="task-panel-tabs">
          {TABS.map(tab => (
            <button
              key={tab}
              class={"task-panel-tab" + (activeTab === tab ? " active" : "")}
              onClick={() => switchTab(tab)}
            >
              {TAB_LABELS[tab]}
            </button>
          ))}
        </div>
        {/* Body — tabs are only mounted after first visit, then kept alive */}
        <div class="task-panel-body">
          {!t ? (
            <div class="diff-empty">Loading...</div>
          ) : (
            <>
              {visitedTabs.overview && (
                <div style={{ display: activeTab === "overview" ? "" : "none" }}>
                  <OverviewTab task={t} stats={stats} />
              </div>
              )}
              {visitedTabs.changes && (
                <div style={{ display: activeTab === "changes" ? "" : "none" }}>
                  <ChangesTab task={t} diffRaw={diffRaw} currentReview={currentReview} oldComments={oldComments} stats={stats} />
              </div>
          )}
              {visitedTabs.merge && (
                <div style={{ display: activeTab === "merge" ? "" : "none" }}>
                  <MergePreviewTab task={t} mergePreviewRaw={mergePreviewRaw} stats={stats} />
        </div>
              )}
              {visitedTabs.activity && (
                <div style={{ display: activeTab === "activity" ? "" : "none" }}>
                  <ActivityTab taskId={t.id} task={t} activityRaw={activityRaw} onLoadActivity={loadActivity} />
      </div>
      )}
    </>
          )}
      </div>
        </div>
      <div class={"task-backdrop" + (isOpen ? " open" : "")} onClick={close}></div>
    </>
  );
}
