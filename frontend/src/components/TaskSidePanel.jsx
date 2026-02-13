import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, tasks, taskPanelId, knownAgentNames, bossName,
  panelStack, pushPanel, closeAllPanels, popPanel,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtStatus, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  fmtRelativeTime, taskIdStr, renderMarkdown, linkifyTaskRefs, linkifyFilePaths,
  flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse, stripEmojis,
  handleCopyClick, toApiPath,
} from "../utils.js";
import { ReviewableDiff } from "./ReviewableDiff.jsx";
import { showToast } from "../toast.js";
import { CopyBtn } from "./CopyBtn.jsx";

// ── Per-task stale-while-revalidate cache ──
// Keyed by "team:taskId" → { stats, diffRaw, mergePreviewRaw, currentReview, oldComments }
// Data is served from cache instantly on panel open, then revalidated in the background.
const _cache = new Map();
function _cacheKey(team, id) { return `${team}:${id}`; }
function _getCache(team, id) { return _cache.get(_cacheKey(team, id)) || {}; }
function _setCache(team, id, patch) {
  const key = _cacheKey(team, id);
  _cache.set(key, { ...(_cache.get(key) || {}), ...patch });
}

// ── Panel title helper (for back-bar) ──
function panelTitle(entry, allTasks) {
  if (!entry) return "";
  if (entry.type === "task") {
    const t = (allTasks || []).find(t => t.id === entry.target);
    return taskIdStr(entry.target) + (t ? " " + t.title : "");
  }
  if (entry.type === "agent") return cap(entry.target || "");
  if (entry.type === "file") return (entry.target || "").split("/").pop() || "File";
  return "";
}

// ── Event delegation for linked content ──
function LinkedDiv({ html, class: cls, style, ref: externalRef }) {
  const internalRef = useRef();

  const setRefs = useCallback((el) => {
    internalRef.current = el;
    if (externalRef) {
      if (typeof externalRef === 'function') externalRef(el);
      else externalRef.current = el;
    }
  }, [externalRef]);

  useEffect(() => {
    if (!internalRef.current) return;
    const el = internalRef.current;
    const handler = (e) => {
      const copyBtn = e.target.closest(".copy-btn");
      if (copyBtn) { e.stopPropagation(); e.preventDefault(); handleCopyClick(copyBtn); return; }
      const taskLink = e.target.closest("[data-task-id]");
      if (taskLink) { e.stopPropagation(); pushPanel("task", parseInt(taskLink.dataset.taskId, 10)); return; }
      const agentLink = e.target.closest("[data-agent-name]");
      if (agentLink) { e.stopPropagation(); pushPanel("agent", agentLink.dataset.agentName); return; }
      const fileLink = e.target.closest("[data-file-path]");
      if (fileLink) { e.stopPropagation(); pushPanel("file", fileLink.dataset.filePath); return; }
    };
    el.addEventListener("click", handler);
    return () => el.removeEventListener("click", handler);
  }, [html]);

  return <div ref={setRefs} class={cls} style={style} dangerouslySetInnerHTML={{ __html: html }} />;
}

// ── Retry merge button (compact, inline) ──
function RetryMergeButton({ task }) {
  const [loading, setLoading] = useState(false);
  const team = currentTeam.value;

  const handleRetry = async () => {
    if (loading) return;
    setLoading(true);
    try {
      await api.retryMerge(team, task.id);
      const refreshed = await api.fetchTasks(team);
      tasks.value = refreshed;
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
function ApprovalBar({ task, currentReview, onAction }) {
  const [loading, setLoading] = useState(false);
  const [summary, setSummary] = useState("");
  const [result, setResult] = useState(null);

  const { status, approval_status, rejection_reason } = task;
  const reviewSummary = currentReview && currentReview.summary;
  const commentCount = currentReview && currentReview.comments ? currentReview.comments.length : 0;

  // Already approved
  if (status === "done" || approval_status === "approved" || result === "approved") {
    return (
      <div class="task-approval-bar task-approval-bar-resolved">
        <span class="approval-badge approval-badge-approved">&#10004; Approved</span>
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
  // Not reviewable
  if (status !== "in_approval") return null;

  const handleApprove = async () => {
    setLoading(true);
    try {
      await api.approveTask(currentTeam.value, task.id, summary);
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
      await api.rejectTask(currentTeam.value, task.id, summary || "(no reason)", summary);
      setResult("rejected");
      if (onAction) onAction();
    } catch (e) {
      showToast("Failed to reject: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

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

// ── Collapsible description ──
const TASK_DESC_COLLAPSE_THRESHOLD = 110;

function CollapsibleDescription({ html }) {
  const contentRef = useRef();
  const [isLong, setIsLong] = useState(false);
  const [isExpanded, setIsExpanded] = useState(false);

  useEffect(() => {
    if (!contentRef.current) return;
    const checkOverflow = () => {
      const el = contentRef.current;
      if (el && el.scrollHeight > TASK_DESC_COLLAPSE_THRESHOLD) {
        setIsLong(true);
      } else {
        setIsLong(false);
      }
    };
    checkOverflow();
    const observer = new ResizeObserver(checkOverflow);
    if (contentRef.current) observer.observe(contentRef.current);
    return () => observer.disconnect();
  }, [html]);

  const toggle = useCallback(() => { setIsExpanded(prev => !prev); }, []);
  const wrapperClass = "task-desc-wrapper" + (isLong && !isExpanded ? " collapsed" : "");

  return (
    <>
      <div class={wrapperClass}>
        <LinkedDiv class="task-panel-desc md-content" html={html} ref={contentRef} />
        {isLong && !isExpanded && <div class="task-desc-fade-overlay" />}
      </div>
      {isLong && (
        <button class="task-desc-expand-btn" onClick={toggle}>
          {isExpanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </>
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
                  <div dangerouslySetInnerHTML={{ __html: diff2HtmlRender(c.diff, { outputFormat: "line-by-line", drawFileList: false, matching: "lines" }) }} />
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
  const descHtml = t.description ? linkifyFilePaths(linkifyTaskRefs(renderMarkdown(t.description))) : "";

  return (
    <div>
      {/* Meta grid */}
      <div class="task-panel-meta-grid">
        <div class="task-panel-meta-item"><div class="task-detail-label">DRI</div><div class="task-detail-value">{t.dri ? cap(t.dri) : "\u2014"}</div></div>
        <div class="task-panel-meta-item"><div class="task-detail-label">Assignee</div><div class="task-detail-value">{t.assignee ? cap(t.assignee) : "\u2014"}</div></div>
        <div class="task-panel-meta-item"><div class="task-detail-label">Priority</div><div class="task-detail-value">{cap(t.priority)}</div></div>
        <div class="task-panel-meta-item"><div class="task-detail-label">Time</div><div class="task-detail-value">{stats ? fmtElapsed(stats.elapsed_seconds) : "\u2014"}</div></div>
      </div>
      {stats && (
        <div class="task-panel-meta-grid">
          <div class="task-panel-meta-item"><div class="task-detail-label">Tokens (in/out)</div><div class="task-detail-value">{fmtTokens(stats.total_tokens_in, stats.total_tokens_out)}</div></div>
          <div class="task-panel-meta-item"><div class="task-detail-label">Cost</div><div class="task-detail-value">{fmtCost(stats.total_cost_usd)}</div></div>
        </div>
      )}
      {/* Dates */}
      <div class="task-panel-dates">
        <span>Created: <span>{fmtTimestamp(t.created_at)}</span></span>
        <span>Updated: <span>{fmtTimestamp(t.updated_at)}</span></span>
        {t.completed_at && <span>Completed: <span>{fmtTimestamp(t.completed_at)}</span></span>}
      </div>
      {/* Dependencies */}
      {t.depends_on && t.depends_on.length > 0 && (
        <div style={{ fontSize: "12px", color: "var(--text-muted)", marginBottom: "12px" }}>
          Depends on:{" "}
          {t.depends_on.map(d => {
            const depStatus = (t._dep_statuses && t._dep_statuses[d]) || "todo";
            return (
              <span
                key={d}
                class="task-link"
                onClick={(e) => { e.stopPropagation(); pushPanel("task", d); }}
              >
                <span class={"badge badge-" + depStatus} style={{ fontSize: "11px", marginRight: "4px", cursor: "pointer" }}>
                  {taskIdStr(d)}
                </span>
              </span>
            );
          })}
        </div>
      )}
      {/* Description */}
      {t.description && (
        <div class="task-panel-section">
          <div class="task-panel-section-label">Description</div>
          <CollapsibleDescription html={descHtml} />
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
                      const isHtmlFile = /\.html?$/i.test(fname);
                      if (isHtmlFile) {
                        window.open(`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(fpath, currentTeam.value))}`, "_blank");
                      } else {
                        pushPanel("file", fpath);
                      }
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
  const team = currentTeam.value;
  const t = task;
  const isReviewable = t && t.status === "in_approval";

  const files = useMemo(() => diffRaw ? diff2HtmlParse(diffRaw) : [], [diffRaw]);
  let totalAdd = 0, totalDel = 0;
  for (const f of files) { totalAdd += f.addedLines; totalDel += f.deletedLines; }

  // Lazy-load commits when expanded
  useEffect(() => {
    if (!commitsExpanded || commitsData !== null) return;
    api.fetchTaskCommits(team, t.id).then(data => {
      setCommitsData(data);
    }).catch(() => {
      setCommitsData({ commit_diffs: {} });
    });
  }, [commitsExpanded, commitsData, team, t.id]);

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
            <code key={i} style={{ fontFamily: "SF Mono,Fira Code,monospace", background: "var(--bg-active)", padding: "2px 6px", borderRadius: "3px", marginRight: "6px" }}>
              {Object.keys(t.base_sha).length > 1 ? repo + ": " : ""}{String(sha).substring(0, 10)}
            </code>
          ))}
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
        />
      ) : null}
      {/* Commits (collapsible) */}
      <div class="changes-commits-section">
        <div class="changes-commits-header" onClick={() => setCommitsExpanded(!commitsExpanded)}>
          <span class="changes-commits-toggle">{commitsExpanded ? "\u25BC" : "\u25B6"}</span>
          <span>Commits</span>
        </div>
        {commitsExpanded && (
          commitsData === null
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

  if (!mergePreviewRaw) return <div class="diff-empty">Loading merge preview...</div>;

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
      />
    </div>
  );
}

// ── Activity tab ──
function ActivityTab({ taskId, task }) {
  const [timeline, setTimeline] = useState(null);
  const [commentText, setCommentText] = useState("");
  const [posting, setPosting] = useState(false);
  const team = currentTeam.value;
  const boss = bossName.value || "boss";

  const loadTimeline = useCallback(async () => {
    try {
      const activity = await api.fetchTaskActivity(team, taskId);
      const items = activity
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
      setTimeline(items);
    } catch (e) {
      setTimeline([]);
    }
  }, [team, taskId]);

  useEffect(() => {
    let cancelled = false;
    loadTimeline().then(() => { if (cancelled) return; });
    return () => { cancelled = true; };
  }, [taskId, team, loadTimeline]);

  const handlePostComment = async () => {
    const body = commentText.trim();
    if (!body || posting) return;
    setPosting(true);
    try {
      await api.postTaskComment(team, taskId, boss, body);
      setCommentText("");
      await loadTimeline();
    } catch (e) {
      showToast("Failed to post comment: " + e.message, "error");
    } finally {
      setPosting(false);
    }
  };

  return (
    <div class="task-activity-tab">
        {timeline === null ? (
          <div class="diff-empty">Loading activity...</div>
        ) : timeline.length === 0 ? (
          <div class="diff-empty">No activity yet</div>
        ) : (
        <div class="task-activity-timeline">
          {timeline.map((e, i) =>
            e.type === "comment" ? (
              <div key={i} class="task-activity-event task-comment-entry">
                <span class="task-activity-icon">{e.icon}</span>
                <div class="task-comment-body">
                  <div class="task-comment-meta">
                    <span class="task-comment-author">{cap(e.author)}</span>
                    <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
                  </div>
                  <div class="task-comment-text">{stripEmojis(e.body)}</div>
                </div>
              </div>
            ) : (
              <div key={i} class="task-activity-event">
                <span class="task-activity-icon">{e.icon}</span>
                <span class="task-activity-text">{stripEmojis(e.text)}</span>
                <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
              </div>
          )
        )}
        </div>
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

// ── Main TaskSidePanel ──
export function TaskSidePanel() {
  const id = taskPanelId.value;
  const team = currentTeam.value;
  const allTasks = tasks.value;

  const [task, setTask] = useState(null);
  const [stats, setStats] = useState(null);
  const [activeTab, setActiveTab] = useState("overview");
  // Track which tabs have been visited — only mount a tab's component
  // after the user first navigates to it (lazy rendering).
  const [visitedTabs, setVisitedTabs] = useState({ overview: true });
  const [diffRaw, setDiffRaw] = useState("");
  const [diffLoaded, setDiffLoaded] = useState(false);
  const [mergePreviewRaw, setMergePreviewRaw] = useState("");
  const [mergePreviewLoaded, setMergePreviewLoaded] = useState(false);
  const [currentReview, setCurrentReview] = useState(null);
  const [oldComments, setOldComments] = useState([]);

  // Mark tab as visited when selected
  const switchTab = useCallback((tab) => {
    setActiveTab(tab);
    setVisitedTabs(prev => prev[tab] ? prev : { ...prev, [tab]: true });
  }, []);

  // Load task data when panel opens — stale-while-revalidate.
  // If we have cached data for this task, show it immediately;
  // then always re-fetch in the background to ensure freshness.
  useEffect(() => {
    if (id === null || !team) { setTask(null); return; }
    setActiveTab("overview");

    // ── Restore from cache (instant) ──
    const c = _getCache(team, id);
    setStats(c.stats ?? null);
    setDiffRaw(c.diffRaw ?? "");
    setDiffLoaded(!!c.diffRaw);
    setMergePreviewRaw(c.mergePreviewRaw ?? "");
    setMergePreviewLoaded(!!c.mergePreviewRaw);
    setCurrentReview(c.currentReview ?? null);
    setOldComments(c.oldComments ?? []);

    // If we have cached diff/merge data, mark those tabs as "already visited"
    // so the lazy-mount keeps them rendered; otherwise start fresh.
    const restored = { overview: true };
    if (c.diffRaw) restored.changes = true;
    if (c.mergePreviewRaw) restored.merge = true;
    setVisitedTabs(restored);

    const cached = allTasks.find(t => t.id === id);
    if (cached) setTask(cached);

    // ── Revalidate in background ──
    (async () => {
      try {
        const s = await api.fetchTaskStats(team, id);
        setStats(s);
        _setCache(team, id, { stats: s });
      } catch (e) { }
    })();
  }, [id, team]);

  // Sync task from signal when SSE pushes updates
  useEffect(() => {
    if (id === null) return;
    const updated = allTasks.find(t => t.id === id);
    if (updated) setTask(prev => prev ? { ...prev, ...updated } : updated);
  }, [allTasks, id]);

  // Load review data eagerly (needed by approval bar in header) — also cached
  useEffect(() => {
    if (id === null || !team) return;
    (async () => {
      try {
        const review = await api.fetchCurrentReview(team, id);
        setCurrentReview(review);
        _setCache(team, id, { currentReview: review });
      } catch (e) { }
      try {
        const reviews = await api.fetchReviews(team, id);
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
          _setCache(team, id, { oldComments: old });
        }
      } catch (e) { }
    })();
  }, [id, team, task && task.review_attempt]);

  // Lazy load diff when Changes tab first visited — stale-while-revalidate
  useEffect(() => {
    if (!visitedTabs.changes || id === null || !team) return;
    // If we already have cached data we showed it immediately above.
    // Always re-fetch to ensure freshness (unless this is the initial
    // load from a cold cache, which the diffLoaded flag already guards).
    if (diffLoaded && _getCache(team, id).diffRaw) {
      // Already showing stale data — revalidate in background
      api.fetchTaskDiff(team, id).then(data => {
        const raw = flattenDiffDict(data.diff);
        setDiffRaw(raw);
        _setCache(team, id, { diffRaw: raw });
      }).catch(() => {});
      return;
    }
    setDiffLoaded(true);
    api.fetchTaskDiff(team, id).then(data => {
      const raw = flattenDiffDict(data.diff);
      setDiffRaw(raw);
      _setCache(team, id, { diffRaw: raw });
    }).catch(() => {});
  }, [visitedTabs.changes, diffLoaded, id, team]);

  // Lazy load merge preview when Merge Preview tab first visited — stale-while-revalidate
  useEffect(() => {
    if (!visitedTabs.merge || id === null || !team) return;
    if (mergePreviewLoaded && _getCache(team, id).mergePreviewRaw) {
      api.fetchTaskMergePreview(team, id).then(data => {
        const raw = flattenDiffDict(data.diff);
        setMergePreviewRaw(raw);
        _setCache(team, id, { mergePreviewRaw: raw });
      }).catch(() => {});
      return;
    }
    setMergePreviewLoaded(true);
    api.fetchTaskMergePreview(team, id).then(data => {
      const raw = flattenDiffDict(data.diff);
      setMergePreviewRaw(raw);
      _setCache(team, id, { mergePreviewRaw: raw });
    }).catch(() => {});
  }, [visitedTabs.merge, mergePreviewLoaded, id, team]);

  const close = useCallback(() => { closeAllPanels(); }, []);

  const handleAction = useCallback(() => {
    if (team) api.fetchTasks(team).then(list => { tasks.value = list; });
  }, [team]);

  if (id === null) return null;

  const isOpen = id !== null;
  const t = task;
  const TABS = ["overview", "changes", "merge", "activity"];
  const TAB_LABELS = { overview: "Overview", changes: "Changes", merge: "Merge Preview", activity: "Activity" };

  const stack = panelStack.value;
  const hasPrev = stack.length > 1;
  const prev = hasPrev ? stack[stack.length - 2] : null;

  return (
    <>
      <div class={"task-panel" + (isOpen ? " open" : "")}>
        {/* Back bar */}
        {hasPrev && (
          <div class="panel-back-bar" onClick={popPanel}>
            <span class="panel-back-arrow">&larr;</span> Back to {panelTitle(prev, allTasks)}
          </div>
        )}
        {/* Header */}
        <div class="task-panel-header">
          <div class="task-panel-title-row">
            <span class="task-panel-id copyable">{taskIdStr(id)}<CopyBtn text={taskIdStr(id)} /></span>
            <span class="task-panel-title">{t ? t.title : "Loading..."}</span>
          </div>
          <div class="task-panel-meta-row">
            <span class="task-panel-status">
              {t && <span class={"badge badge-" + t.status}>{fmtStatus(t.status)}</span>}
            </span>
            <span class="task-panel-assignee copyable">{t && t.assignee ? cap(t.assignee) : ""}{t && t.assignee && <CopyBtn text={t.assignee} />}</span>
            <span class="task-panel-priority">{t && t.priority ? cap(t.priority) : ""}</span>
          </div>
          <button class="task-panel-close" onClick={close}>&times;</button>
        </div>
        {/* Approval bar (sticky, between header and tabs) */}
        {t && <ApprovalBar task={t} currentReview={currentReview} onAction={handleAction} />}
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
                  <ActivityTab taskId={t.id} task={t} />
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
