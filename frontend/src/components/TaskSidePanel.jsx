import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, tasks, taskPanelId, knownAgentNames, bossName,
  diffPanelMode, diffPanelTarget,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtStatus, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  fmtRelativeTime, taskIdStr, renderMarkdown, linkifyTaskRefs, linkifyFilePaths,
  flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse,
} from "../utils.js";
import { ReviewableDiff } from "./ReviewableDiff.jsx";
import { showToast } from "../toast.js";

// ── Event delegation for linked content ──
function LinkedDiv({ html, class: cls, style }) {
  const ref = useRef();
  useEffect(() => {
    if (!ref.current) return;
    const handler = (e) => {
      const taskLink = e.target.closest("[data-task-id]");
      if (taskLink) { e.stopPropagation(); taskPanelId.value = parseInt(taskLink.dataset.taskId, 10); return; }
      const agentLink = e.target.closest("[data-agent-name]");
      if (agentLink) { e.stopPropagation(); diffPanelMode.value = "agent"; diffPanelTarget.value = agentLink.dataset.agentName; return; }
      const fileLink = e.target.closest("[data-file-path]");
      if (fileLink) { e.stopPropagation(); diffPanelMode.value = "file"; diffPanelTarget.value = fileLink.dataset.filePath; return; }
    };
    ref.current.addEventListener("click", handler);
    return () => ref.current && ref.current.removeEventListener("click", handler);
  }, [html]);
  return <div ref={ref} class={cls} style={style} dangerouslySetInnerHTML={{ __html: html }} />;
}

// ── Approval Badge (Details tab) ──
function ApprovalBadge({ task, currentReview }) {
  const { status, approval_status, rejection_reason } = task;
  const attempt = task.review_attempt || 0;
  const attemptLabel = attempt > 1 ? ` (attempt ${attempt})` : "";
  const reviewSummary = currentReview && currentReview.summary;

  if (status === "done" || approval_status === "approved") {
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-approved">&#10004; Approved &amp; Merged{attemptLabel}</div>
        {reviewSummary && <div class="approval-rejection-reason" style={{ color: "var(--text-secondary)" }}>{reviewSummary}</div>}
      </div>
    );
  }
  if (status === "rejected" || approval_status === "rejected") {
    const reason = reviewSummary || rejection_reason;
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-rejected">&#10006; Rejected{attemptLabel}</div>
        {reason && <div class="approval-rejection-reason">{reason}</div>}
      </div>
    );
  }
  if (status === "in_approval") {
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-pending">&#9203; Awaiting Approval{attemptLabel}</div>
        <span style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "4px" }}>
          Review changes in the <a href="#" onClick={(e) => { e.preventDefault(); setTabFromBadge && setTabFromBadge("changes"); }} style={{ color: "var(--accent-blue)" }}>Changes</a> tab
        </span>
      </div>
    );
  }
  if (status === "merging") {
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-merging">&#8635; Merging...</div>
      </div>
    );
  }
  if (status === "merge_failed") {
    return <div class="task-approval-status"><div class="approval-badge" style={{ background: "rgba(251,146,60,0.12)", color: "#fb923c" }}>&#9888; Merge Failed</div></div>;
  }
  return null;
}

// ── Retry Merge Button ──
function RetryMergeButton({ task }) {
  const [loading, setLoading] = useState(false);
  const team = currentTeam.value;

  const handleRetry = async () => {
    if (loading) return;
    setLoading(true);
    try {
      await api.retryMerge(team, task.id);
      // Refresh task list
      const refreshed = await api.fetchTasks(team);
      tasks.value = refreshed;
    } catch (err) {
      alert("Retry failed: " + err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ marginTop: "8px" }}>
      <button
        class="approve-btn"
        onClick={handleRetry}
        disabled={loading}
        style={{ width: "100%" }}
      >
        {loading ? "Retrying..." : "Retry Merge"}
      </button>
    </div>
  );
}

// Module-level variable for badge→tab communication
let setTabFromBadge = null;

// ── Approval Actions (Changes tab) ──
function ApprovalActions({ task, currentReview, onApproved, onRejected }) {
  const [loading, setLoading] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [summary, setSummary] = useState("");
  const [result, setResult] = useState(null); // "approved" | "rejected" | null
  const inputRef = useRef();

  const { status, approval_status, rejection_reason } = task;
  const attempt = task.review_attempt || 0;
  const attemptLabel = attempt > 1 ? ` (attempt ${attempt})` : "";
  const reviewSummary = currentReview && currentReview.summary;
  const commentCount = currentReview && currentReview.comments ? currentReview.comments.length : 0;

  if (status === "done" || approval_status === "approved" || result === "approved") {
    return (
      <div class="task-review-box task-review-box-approved">
        <div class="approval-badge approval-badge-approved">&#10004; Approved &amp; Merged{attemptLabel}</div>
        {(summary || reviewSummary) && <div class="approval-rejection-reason" style={{ color: "var(--text-secondary)" }}>{summary || reviewSummary}</div>}
      </div>
    );
  }
  if (status === "rejected" || approval_status === "rejected" || result === "rejected") {
    const reason = result === "rejected" ? (summary || rejectReason) : (reviewSummary || rejection_reason);
    return (
      <div class="task-review-box task-review-box-rejected">
        <div class="approval-badge approval-badge-rejected">&#10006; Changes Rejected{attemptLabel}</div>
        {reason && <div class="approval-rejection-reason">{reason}</div>}
        {commentCount > 0 && <div style={{ fontSize: "11px", color: "var(--text-muted)", marginTop: "4px" }}>{commentCount} inline comment{commentCount !== 1 ? "s" : ""}</div>}
      </div>
    );
  }
  if (status !== "in_approval") return null;

  const handleApprove = async () => {
    setLoading(true);
    try {
      await api.approveTask(currentTeam.value, task.id, summary);
      setResult("approved");
      if (onApproved) onApproved();
    } catch (e) {
      showToast("Failed to approve: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  const handleReject = async () => {
    setLoading(true);
    try {
      await api.rejectTask(currentTeam.value, task.id, rejectReason || summary || "(no reason)", summary);
      setResult("rejected");
      if (onRejected) onRejected();
    } catch (e) {
      showToast("Failed to reject: " + e.message, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div class="task-review-box">
      <div class="task-review-box-header">
        Review changes{attemptLabel}
        {commentCount > 0 && (
          <span style={{ marginLeft: "8px", fontSize: "11px", color: "var(--accent)" }}>
            {commentCount} comment{commentCount !== 1 ? "s" : ""}
          </span>
        )}
      </div>
      {/* Overall comment textarea */}
      <textarea
        class="rc-comment-textarea"
        style={{ marginBottom: "8px" }}
        placeholder="Overall comment (optional)..."
        value={summary}
        onInput={(e) => setSummary(e.target.value)}
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => e.stopPropagation()}
        rows="2"
      />
      <div class="task-review-box-actions">
        <button
          class={"btn-approve" + (loading ? " loading" : "")}
          disabled={loading}
          onClick={(e) => { e.stopPropagation(); handleApprove(); }}
        >
          {loading ? "Merging..." : "\u2714 Approve & Merge"}
        </button>
        <button
          class="btn-reject-outline"
          disabled={loading}
          onClick={(e) => {
            e.stopPropagation();
            setShowReject(!showReject);
            if (!showReject) setTimeout(() => inputRef.current && inputRef.current.focus(), 50);
          }}
        >
          &#10006; Request Changes
        </button>
      </div>
      {showReject && (
        <div class="reject-reason-row">
          <input
            ref={inputRef}
            type="text"
            class="reject-reason-input"
            placeholder="Describe what needs to change..."
            value={rejectReason}
            onInput={(e) => setRejectReason(e.target.value)}
            onClick={(e) => e.stopPropagation()}
            onKeyDown={(e) => { e.stopPropagation(); if (e.key === "Enter") handleReject(); }}
          />
          <button
            class="btn-reject"
            disabled={loading}
            onClick={(e) => { e.stopPropagation(); handleReject(); }}
            style={{ flexShrink: 0 }}
          >
            {loading ? "Rejecting..." : "Submit"}
          </button>
        </div>
      )}
    </div>
  );
}

// ── Activity section (interleaved events + comments) ──
function ActivitySection({ taskId, task }) {
  const [timeline, setTimeline] = useState(null);
  const [expanded, setExpanded] = useState(true);
  const [commentText, setCommentText] = useState("");
  const [posting, setPosting] = useState(false);
  const team = currentTeam.value;
  const boss = bossName.value || "boss";

  const loadTimeline = useCallback(async () => {
    try {
      const activity = await api.fetchTaskActivity(team, taskId);
      const items = activity.map((m) => {
        if (m.type === "comment") {
          return {
            type: "comment",
            time: m.timestamp,
            author: m.sender || "unknown",
            body: m.content || "",
            icon: "\u270E",
          };
        }
        if (m.type === "event") {
          const text = m.content || "Event";
          let icon = "\u21BB";
          if (/created/i.test(text)) icon = "+";
          else if (/assign/i.test(text)) icon = "\u2192";
          else if (/approved|merged/i.test(text)) icon = "\u2713";
          else if (/rejected/i.test(text)) icon = "\u2717";
          else if (/review/i.test(text)) icon = "\u2299";
          else if (/commented/i.test(text)) icon = "\u270E";
          return { type: "event", time: m.timestamp, text, icon };
        }
        const sender = cap(m.sender || "unknown");
        const recipient = cap(m.recipient || "unknown");
        return { type: "chat", time: m.timestamp, text: `${sender} \u2192 ${recipient}`, icon: "\u25B7" };
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
    <div class="task-activity-section">
      <div class="task-activity-header" onClick={() => setExpanded(!expanded)}>
        <span class={"task-activity-arrow" + (expanded ? " expanded" : "")}>&#9654;</span>
        <span class="task-panel-section-label" style={{ marginBottom: 0 }}>Activity</span>
      </div>
      <div class={"task-activity-list" + (expanded ? " expanded" : "")}>
        {timeline === null ? (
          <div class="diff-empty">Loading activity...</div>
        ) : timeline.length === 0 ? (
          <div class="diff-empty">No activity yet</div>
        ) : (
          timeline.map((e, i) =>
            e.type === "comment" ? (
              <div key={i} class="task-activity-event task-comment-entry">
                <span class="task-activity-icon">{e.icon}</span>
                <div class="task-comment-body">
                  <div class="task-comment-meta">
                    <span class="task-comment-author">{cap(e.author)}</span>
                    <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
                  </div>
                  <div class="task-comment-text">{e.body}</div>
                </div>
              </div>
            ) : (
              <div key={i} class="task-activity-event">
                <span class="task-activity-icon">{e.icon}</span>
                <span class="task-activity-text">{e.text}</span>
                <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
              </div>
            )
          )
        )}
        {/* Comment input for the boss */}
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
    </div>
  );
}

// ── Diff tabs content ──
function DiffContent({ diffRaw, taskId, diffTab, setDiffTab, task, currentReview, oldComments }) {
  const [commitsData, setCommitsData] = useState(null);
  const team = currentTeam.value;
  const isReviewable = task && task.status === "in_approval";

  const renderFiles = () => {
    const files = diff2HtmlParse(diffRaw);
    if (!files.length) return <div class="diff-empty">No files changed</div>;
    let totalAdd = 0, totalDel = 0;
    for (const f of files) { totalAdd += f.addedLines; totalDel += f.deletedLines; }
    return (
      <div>
        <div style={{ fontSize: "12px", color: "var(--text-muted)", marginBottom: "8px" }}>
          {files.length} file{files.length !== 1 ? "s" : ""} changed,{" "}
          <span style={{ color: "var(--diff-add-text)" }}>+{totalAdd}</span>{" "}
          <span style={{ color: "var(--diff-del-text)" }}>&minus;{totalDel}</span>
        </div>
        <div class="diff-file-list">
          {files.map((f, i) => {
            const name = (f.newName === "/dev/null" ? f.oldName : f.newName) || f.oldName || "unknown";
            return (
              <div key={i} class="diff-file-list-item" onClick={() => setDiffTab("diff")}>
                <span class="diff-file-list-name">{name}</span>
                <span class="diff-file-stats">
                  <span class="diff-file-add">+{f.addedLines}</span>
                  <span class="diff-file-del">-{f.deletedLines}</span>
                </span>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  const renderReviewableDiff = () => {
    if (!diffRaw) return <div class="diff-empty">No changes</div>;
    return (
      <ReviewableDiff
        diffRaw={diffRaw}
        taskId={taskId}
        currentComments={currentReview ? (currentReview.comments || []) : []}
        oldComments={oldComments || []}
        isReviewable={isReviewable}
      />
    );
  };

  const renderCommits = () => {
    if (commitsData === null) {
      // Fetch on first view
      api.fetchTaskCommits(team, taskId).then(data => {
        setCommitsData(data);
      }).catch(() => {
        setCommitsData({ commit_diffs: {} });
      });
      return <div class="diff-empty">Loading commits...</div>;
    }
    const allCommits = [];
    const cd = commitsData.commit_diffs || {};
    Object.keys(cd).forEach(repo => {
      (cd[repo] || []).forEach(c => allCommits.push({ ...c, repo }));
    });
    if (!allCommits.length) return <div class="diff-empty">No commits recorded</div>;
    return <CommitList commits={allCommits} multiRepo={Object.keys(cd).length > 1} />;
  };

  return (
    <div class="task-panel-diff-section">
      <div class="task-panel-diff-tabs">
        {["files", "diff", "commits"].map(tab => (
          <button
            key={tab}
            class={"diff-tab" + (diffTab === tab ? " active" : "")}
            onClick={() => setDiffTab(tab)}
          >
            {tab === "files" ? "Files Changed" : tab === "diff" ? "Review Diff" : "Commits"}
          </button>
        ))}
      </div>
      <div>
        {diffTab === "files" && renderFiles()}
        {diffTab === "diff" && renderReviewableDiff()}
        {diffTab === "commits" && renderCommits()}
      </div>
    </div>
  );
}

// ── Commit list ──
function CommitList({ commits, multiRepo }) {
  const [expandedIdx, setExpandedIdx] = useState({});

  const toggle = (idx) => {
    setExpandedIdx(prev => ({ ...prev, [idx]: !prev[idx] }));
  };

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

// ── Main TaskSidePanel ──
export function TaskSidePanel() {
  const id = taskPanelId.value;
  const team = currentTeam.value;
  const allTasks = tasks.value;

  const [task, setTask] = useState(null);
  const [stats, setStats] = useState(null);
  const [activeTabLocal, setActiveTabLocal] = useState("details");
  const [diffRaw, setDiffRaw] = useState("");
  const [diffLoaded, setDiffLoaded] = useState(false);
  const [diffTab, setDiffTab] = useState("files");
  const [currentReview, setCurrentReview] = useState(null);
  const [oldComments, setOldComments] = useState([]);

  // Set the tab switcher for badge → changes tab link
  useEffect(() => {
    setTabFromBadge = setActiveTabLocal;
    return () => { setTabFromBadge = null; };
  }, []);

  // Load task data when panel opens
  useEffect(() => {
    if (id === null || !team) { setTask(null); return; }
    setActiveTabLocal("details");
    setDiffRaw("");
    setDiffLoaded(false);
    setDiffTab("files");
    setCurrentReview(null);
    setOldComments([]);

    // Find task from cached list first
    const cached = allTasks.find(t => t.id === id);
    if (cached) setTask(cached);

    // Also fetch fresh (in case cached is stale)
    (async () => {
      try {
        const taskList = await api.fetchTasks(team);
        const found = taskList.find(t => t.id === id);
        if (found) setTask(found);
      } catch (e) { }
      try {
        const s = await api.fetchTaskStats(team, id);
        setStats(s);
      } catch (e) { }
    })();
  }, [id, team]);

  // Load review data when panel opens or task changes
  useEffect(() => {
    if (id === null || !team) return;
    (async () => {
      try {
        const review = await api.fetchCurrentReview(team, id);
        setCurrentReview(review);
      } catch (e) { }
      try {
        const reviews = await api.fetchReviews(team, id);
        if (reviews.length > 1) {
          // Collect comments from all previous attempts (not the latest)
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
        }
      } catch (e) { }
    })();
  }, [id, team, task && task.review_attempt]);

  // Lazy load diff when Changes tab activated
  useEffect(() => {
    if (activeTabLocal !== "changes" || diffLoaded || id === null || !team) return;
    setDiffLoaded(true);
    api.fetchTaskDiff(team, id).then(data => {
      setDiffRaw(flattenDiffDict(data.diff));
    }).catch(() => {});
  }, [activeTabLocal, diffLoaded, id, team]);

  const close = useCallback(() => { taskPanelId.value = null; }, []);

  const handleApproved = useCallback(() => {
    // Refresh tasks in background
    if (team) api.fetchTasks(team).then(list => { tasks.value = list; });
  }, [team]);

  if (id === null) return null;

  const isOpen = id !== null;
  const t = task;

  return (
    <>
      <div class={"task-panel" + (isOpen ? " open" : "")}>
        <div class="task-panel-header">
          <div class="task-panel-title-row">
            <span class="task-panel-id">{taskIdStr(id)}</span>
            <span class="task-panel-title">{t ? t.title : "Loading..."}</span>
          </div>
          <div class="task-panel-meta-row">
            <span class="task-panel-status">
              {t && <span class={"badge badge-" + t.status}>{fmtStatus(t.status)}</span>}
            </span>
            <span class="task-panel-assignee">{t && t.assignee ? cap(t.assignee) : ""}</span>
            <span class="task-panel-priority">{t && t.priority ? cap(t.priority) : ""}</span>
          </div>
          <button class="task-panel-close" onClick={close}>&times;</button>
        </div>
        <div class="task-panel-tabs">
          {["details", "changes"].map(tab => (
            <button
              key={tab}
              class={"task-panel-tab" + (activeTabLocal === tab ? " active" : "")}
              onClick={() => setActiveTabLocal(tab)}
            >
              {tab === "details" ? "Details" : "Changes"}
            </button>
          ))}
        </div>
        <div class="task-panel-body">
          {!t ? (
            <div class="diff-empty">Loading...</div>
          ) : (
            <>
              {/* Details tab */}
              <div style={{ display: activeTabLocal === "details" ? "" : "none" }}>
                <DetailsTab task={t} stats={stats} currentReview={currentReview} />
              </div>
              {/* Changes tab */}
              <div style={{ display: activeTabLocal === "changes" ? "" : "none" }}>
                <ChangesTab task={t} stats={stats} diffRaw={diffRaw} diffTab={diffTab} setDiffTab={setDiffTab} onApproved={handleApproved} currentReview={currentReview} oldComments={oldComments} />
              </div>
            </>
          )}
        </div>
      </div>
      <div class={"task-backdrop" + (isOpen ? " open" : "")} onClick={close}></div>
    </>
  );
}

// ── Details tab content ──
function DetailsTab({ task, stats, currentReview }) {
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
      {/* Stats */}
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
                onClick={(e) => { e.stopPropagation(); taskPanelId.value = d; }}
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
                    onClick={(e) => { e.stopPropagation(); diffPanelMode.value = "file"; diffPanelTarget.value = fpath; }}
                  >
                    {fname}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}
      {/* Activity */}
      <ActivitySection taskId={t.id} task={t} />
      {/* Status detail (merge failure reason etc.) */}
      {t.status_detail && (
        <div style={{ fontSize: "12px", color: "var(--text-muted)", padding: "8px 12px", background: "rgba(251,191,36,0.06)", borderRadius: "6px", marginBottom: "8px", border: "1px solid rgba(251,191,36,0.15)" }}>
          {t.status_detail}
        </div>
      )}
      {/* Approval badge */}
      <ApprovalBadge task={t} currentReview={currentReview} />
      {/* Retry merge button */}
      {t.status === "merge_failed" && <RetryMergeButton task={t} />}
    </div>
  );
}

// ── Changes tab content ──
function ChangesTab({ task, stats, diffRaw, diffTab, setDiffTab, onApproved, currentReview, oldComments }) {
  const t = task;

  return (
    <div>
      {/* VCS info */}
      {stats && stats.branch && (
        <div class="task-panel-vcs-row">
          <span class="task-branch" title={stats.branch}>{stats.branch}</span>
        </div>
      )}
      {/* Base SHA */}
      {t.base_sha && typeof t.base_sha === "object" && Object.keys(t.base_sha).length > 0 && (
        <div style={{ fontSize: "11px", color: "var(--text-muted)", marginBottom: "12px" }}>
          Base SHA:{" "}
          {Object.entries(t.base_sha).map(([repo, sha], i) => (
            <code key={i} style={{ fontFamily: "SF Mono,Fira Code,monospace", background: "var(--bg-active)", padding: "2px 6px", borderRadius: "3px", marginRight: "6px" }}>
              {Object.keys(t.base_sha).length > 1 ? repo + ": " : ""}{String(sha).substring(0, 10)}
            </code>
          ))}
        </div>
      )}
      {/* Diff */}
      <DiffContent diffRaw={diffRaw} taskId={t.id} diffTab={diffTab} setDiffTab={setDiffTab} task={t} currentReview={currentReview} oldComments={oldComments} />
      {/* Approval actions */}
      <ApprovalActions task={t} currentReview={currentReview} onApproved={onApproved} onRejected={onApproved} />
    </div>
  );
}
