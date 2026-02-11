import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, tasks, taskPanelId, knownAgentNames,
  diffPanelMode, diffPanelTarget,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtStatus, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  fmtRelativeTime, taskIdStr, renderMarkdown, linkifyTaskRefs, linkifyFilePaths,
  flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse,
} from "../utils.js";

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
function ApprovalBadge({ task, review }) {
  const { status } = task;
  const verdict = review ? review.verdict : null;
  const summary = review ? review.summary : "";

  if (status === "done" || verdict === "approved") {
    return <div class="task-approval-status"><div class="approval-badge approval-badge-approved">&#10004; Approved &amp; Merged</div></div>;
  }
  if (status === "rejected" || verdict === "rejected") {
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-rejected">&#10006; Rejected</div>
        {summary && <div class="approval-rejection-reason">{summary}</div>}
      </div>
    );
  }
  if (status === "in_approval") {
    return (
      <div class="task-approval-status">
        <div class="approval-badge approval-badge-pending">&#9203; Awaiting Approval</div>
        <span style={{ fontSize: "12px", color: "var(--text-muted)", marginTop: "4px" }}>
          Review changes in the <a href="#" onClick={(e) => { e.preventDefault(); setTabFromBadge && setTabFromBadge("changes"); }} style={{ color: "var(--accent-blue)" }}>Changes</a> tab
        </span>
      </div>
    );
  }
  if (status === "conflict") {
    return <div class="task-approval-status"><div class="approval-badge" style={{ background: "rgba(251,146,60,0.12)", color: "#fb923c" }}>&#9888; Merge Conflict</div></div>;
  }
  return null;
}

// Module-level variable for badge→tab communication
let setTabFromBadge = null;

// ── Approval Actions (Changes tab) ──
function ApprovalActions({ task, review, onApproved, onRejected }) {
  const [loading, setLoading] = useState(false);
  const [showReject, setShowReject] = useState(false);
  const [rejectReason, setRejectReason] = useState("");
  const [result, setResult] = useState(null); // "approved" | "rejected" | null
  const inputRef = useRef();

  const { status } = task;
  const verdict = review ? review.verdict : null;
  const summary = review ? review.summary : "";

  if (status === "done" || verdict === "approved" || result === "approved") {
    return <div class="task-review-box task-review-box-approved"><div class="approval-badge approval-badge-approved">&#10004; Approved &amp; Merged</div></div>;
  }
  if (status === "rejected" || verdict === "rejected" || result === "rejected") {
    const reason = result === "rejected" ? rejectReason : summary;
    return (
      <div class="task-review-box task-review-box-rejected">
        <div class="approval-badge approval-badge-rejected">&#10006; Changes Rejected</div>
        {reason && <div class="approval-rejection-reason">{reason}</div>}
      </div>
    );
  }
  if (status !== "in_approval") return null;

  const handleApprove = async () => {
    setLoading(true);
    try {
      await api.approveTask(currentTeam.value, task.id);
      setResult("approved");
      if (onApproved) onApproved();
    } catch (e) {
      alert("Failed to approve: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  const handleReject = async () => {
    setLoading(true);
    try {
      await api.rejectTask(currentTeam.value, task.id, rejectReason || "(no reason)");
      setResult("rejected");
      if (onRejected) onRejected();
    } catch (e) {
      alert("Failed to reject: " + e.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div class="task-review-box">
      <div class="task-review-box-header">Review changes</div>
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

// ── Activity section ──
function ActivitySection({ taskId, task }) {
  const [events, setEvents] = useState(null);
  const [expanded, setExpanded] = useState(false);
  const team = currentTeam.value;

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const evts = [];
      if (task.created_at) evts.push({ type: "created", time: task.created_at, text: "Task created", icon: "\u2795" });
      if (task.assignee) evts.push({ type: "assignment", time: task.created_at, text: "Assigned to " + cap(task.assignee), icon: "\uD83D\uDC64" });
      if (task.status && task.status !== "todo") evts.push({ type: "status", time: task.updated_at, text: "Status: " + fmtStatus(task.status), icon: "\uD83D\uDD04" });
      try {
        const msgs = await api.fetchMessages(team, {});
        const taskRef = taskIdStr(taskId);
        for (const m of msgs) {
          if (m.type === "chat" && m.content && m.content.includes(taskRef)) {
            evts.push({ type: "mention", time: m.timestamp, text: "Mentioned by " + cap(m.sender), icon: "\uD83D\uDCAC" });
          }
        }
      } catch (e) { }
      evts.sort((a, b) => (a.time || "").localeCompare(b.time || ""));
      if (!cancelled) setEvents(evts);
    })();
    return () => { cancelled = true; };
  }, [taskId]);

  return (
    <div class="task-activity-section">
      <div class="task-activity-header" onClick={() => setExpanded(!expanded)}>
        <span class={"task-activity-arrow" + (expanded ? " expanded" : "")}>&#9654;</span>
        <span class="task-panel-section-label" style={{ marginBottom: 0 }}>Activity</span>
      </div>
      <div class={"task-activity-list" + (expanded ? " expanded" : "")}>
        {events === null ? (
          <div class="diff-empty">Loading activity...</div>
        ) : events.length === 0 ? (
          <div class="diff-empty">No activity yet</div>
        ) : (
          events.map((e, i) => (
            <div key={i} class="task-activity-event">
              <span class="task-activity-icon">{e.icon}</span>
              <span class="task-activity-text">{e.text}</span>
              <span class="task-activity-time">{fmtRelativeTime(e.time)}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

// ── Diff tabs content ──
function DiffContent({ diffRaw, taskId, diffTab, setDiffTab }) {
  const [commitsData, setCommitsData] = useState(null);
  const team = currentTeam.value;

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

  const renderFullDiff = () => {
    if (!diffRaw) return <div class="diff-empty">No changes</div>;
    const html = diff2HtmlRender(diffRaw, { outputFormat: "line-by-line", drawFileList: false, matching: "lines" });
    return <div dangerouslySetInnerHTML={{ __html: html }} />;
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
            {tab === "files" ? "Files Changed" : tab === "diff" ? "Full Diff" : "Commits"}
          </button>
        ))}
      </div>
      <div>
        {diffTab === "files" && renderFiles()}
        {diffTab === "diff" && renderFullDiff()}
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
  const [review, setReview] = useState(null);
  const [activeTabLocal, setActiveTabLocal] = useState("details");
  const [diffRaw, setDiffRaw] = useState("");
  const [diffLoaded, setDiffLoaded] = useState(false);
  const [diffTab, setDiffTab] = useState("files");

  // Set the tab switcher for badge → changes tab link
  useEffect(() => {
    setTabFromBadge = setActiveTabLocal;
    return () => { setTabFromBadge = null; };
  }, []);

  // Load task data when panel opens
  useEffect(() => {
    if (id === null || !team) { setTask(null); setReview(null); return; }
    setActiveTabLocal("details");
    setDiffRaw("");
    setDiffLoaded(false);
    setDiffTab("files");
    setReview(null);

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
      // Fetch current review from the reviews table (source of truth)
      try {
        const r = await api.fetchCurrentReview(team, id);
        setReview(r);
      } catch (e) { }
    })();
  }, [id, team]);

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
                <DetailsTab task={t} stats={stats} review={review} />
              </div>
              {/* Changes tab */}
              <div style={{ display: activeTabLocal === "changes" ? "" : "none" }}>
                <ChangesTab task={t} stats={stats} review={review} diffRaw={diffRaw} diffTab={diffTab} setDiffTab={setDiffTab} onApproved={handleApproved} />
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
function DetailsTab({ task, stats, review }) {
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
      {/* Approval badge */}
      <ApprovalBadge task={t} review={review} />
    </div>
  );
}

// ── Changes tab content ──
function ChangesTab({ task, stats, review, diffRaw, diffTab, setDiffTab, onApproved }) {
  const t = task;

  return (
    <div>
      {/* VCS info */}
      {stats && stats.branch && (
        <div class="task-panel-vcs-row">
          <span class="task-branch" title={stats.branch}>{stats.branch}</span>
          {stats.commits && typeof stats.commits === "object" && (() => {
            let allCommits = [];
            if (Array.isArray(stats.commits)) {
              allCommits = stats.commits;
            } else {
              Object.keys(stats.commits).forEach(repo => {
                (stats.commits[repo] || []).forEach(c => allCommits.push(c));
              });
            }
            return allCommits.map((c, i) => (
              <span key={i} class="diff-panel-commit">{String(c).substring(0, 7)}</span>
            ));
          })()}
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
      <DiffContent diffRaw={diffRaw} taskId={t.id} diffTab={diffTab} setDiffTab={setDiffTab} />
      {/* Approval actions */}
      <ApprovalActions task={t} review={review} onApproved={onApproved} onRejected={onApproved} />
    </div>
  );
}
