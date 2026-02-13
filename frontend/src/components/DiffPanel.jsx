import { useState, useEffect, useCallback, useRef } from "preact/hooks";
import {
  currentTeam, diffPanelMode, diffPanelTarget, tasks,
  panelStack, pushPanel, closeAllPanels, popPanel,
  agentActivityLog,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse,
  renderMarkdown, msgStatusIcon, taskIdStr, toApiPath, displayFilePath,
} from "../utils.js";

// ── Diff viewer (task diff) ──
function DiffView({ taskId }) {
  const team = currentTeam.value;
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("files");
  const [rawDiff, setRawDiff] = useState("");

  useEffect(() => {
    if (!taskId || !team) return;
    setData(null); setTab("files"); setRawDiff("");
    api.fetchTaskDiff(team, taskId).then(d => {
      setData(d);
      setRawDiff(flattenDiffDict(d.diff));
    }).catch(() => {});
  }, [taskId, team]);

  if (!data) return <div class="diff-empty">Loading diff...</div>;

  const renderFiles = () => {
    const files = diff2HtmlParse(rawDiff);
    if (!files.length) return <div class="diff-empty">No files changed</div>;
    return (
      <div class="diff-file-list">
        {files.map((f, i) => {
          const name = (f.newName === "/dev/null" ? f.oldName : f.newName) || f.oldName || "unknown";
          return (
            <div key={i} class="diff-file-list-item" onClick={() => setTab("diff")}>
              <span class="diff-file-list-name">{name}</span>
              <span class="diff-file-stats">
                <span class="diff-file-add">+{f.addedLines}</span>
                <span class="diff-file-del">-{f.deletedLines}</span>
              </span>
            </div>
          );
        })}
      </div>
    );
  };

  const renderFull = () => {
    if (!rawDiff) return <div class="diff-empty">No changes</div>;
    return <div dangerouslySetInnerHTML={{ __html: diff2HtmlRender(rawDiff, { outputFormat: "line-by-line", drawFileList: false, matching: "words" }) }} />;
  };

  return (
    <>
      <div class="diff-panel-tabs">
        <button class={"diff-tab" + (tab === "files" ? " active" : "")} onClick={() => setTab("files")}>Files Changed</button>
        <button class={"diff-tab" + (tab === "diff" ? " active" : "")} onClick={() => setTab("diff")}>Full Diff</button>
      </div>
      <div class="diff-panel-body">
        {tab === "files" ? renderFiles() : renderFull()}
      </div>
    </>
  );
}

// ── Agent panel ──
function AgentView({ agentName }) {
  const team = currentTeam.value;
  const [tab, setTab] = useState("activity");
  const [tabData, setTabData] = useState({});
  const [role, setRole] = useState("");
  const activityEndRef = useRef(null);

  useEffect(() => {
    if (!agentName || !team) return;
    setTab("activity"); setTabData({});
    api.fetchAgents(team).then(agents => {
      const a = agents.find(x => x.name === agentName);
      if (a) setRole(cap(a.role));
    }).catch(() => {});
  }, [agentName, team]);

  const switchTab = useCallback((t) => {
    setTab(t);
    if (t !== "activity" && !tabData[t]) {
      api.fetchAgentTab(team, agentName, t).then(d => {
        setTabData(prev => ({ ...prev, [t]: d }));
      }).catch(() => {});
    }
  }, [team, agentName, tabData]);

  const renderInbox = (msgs) => {
    if (!msgs || !msgs.length) return <div class="diff-empty">No messages</div>;
    return msgs.map((m, i) => (
      <div key={i} class={"agent-msg" + (m.processed_at ? "" : " unread")}>
        <div class="agent-msg-header">
          <span class="agent-msg-sender">{cap(m.sender)}</span>
          <span class="agent-msg-time" dangerouslySetInnerHTML={{ __html: fmtTimestamp(m.time) + " " + msgStatusIcon(m) }} />
        </div>
        <div class="agent-msg-body collapsed" onClick={(e) => e.target.classList.toggle("collapsed")}>
          {m.body}
        </div>
      </div>
    ));
  };

  const renderOutbox = (msgs) => {
    if (!msgs || !msgs.length) return <div class="diff-empty">No messages</div>;
    return msgs.map((m, i) => (
      <div key={i} class="agent-msg">
        <div class="agent-msg-header">
          <span class="agent-msg-sender">&rarr; {cap(m.recipient)}</span>
          <span class="agent-msg-time" dangerouslySetInnerHTML={{ __html: fmtTimestamp(m.time) + " " + msgStatusIcon(m) }} />
        </div>
        <div class="agent-msg-body collapsed" onClick={(e) => e.target.classList.toggle("collapsed")}>
          {m.body}
        </div>
      </div>
    ));
  };

  const renderLogs = (data) => {
    const sessions = data && data.sessions ? data.sessions : [];
    if (!sessions.length) return <div class="diff-empty">No worklogs</div>;
    return sessions.map((s, i) => (
      <div key={i} class="agent-log-session">
        <div class="agent-log-header" onClick={(e) => {
          e.target.closest(".agent-log-session").querySelector(".agent-log-arrow").classList.toggle("expanded");
          e.target.closest(".agent-log-session").querySelector(".agent-log-content").classList.toggle("expanded");
        }}>
          <span class={"agent-log-arrow" + (i === 0 ? " expanded" : "")}>&#9654;</span>
          {s.filename}
        </div>
        <div class={"agent-log-content" + (i === 0 ? " expanded" : "")}>
          {s.content}
        </div>
      </div>
    ));
  };

  const renderStats = (s) => {
    if (!s) return <div class="diff-empty">Stats unavailable</div>;
    return (
      <div class="agent-stats-grid">
        <div class="agent-stat"><div class="agent-stat-label">Tasks done</div><div class="agent-stat-value">{s.tasks_done}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">In review</div><div class="agent-stat-value">{s.tasks_in_review}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Total tasks</div><div class="agent-stat-value">{s.tasks_total}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Sessions</div><div class="agent-stat-value">{s.session_count}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Tokens (in/out)</div><div class="agent-stat-value">{fmtTokens(s.total_tokens_in, s.total_tokens_out)}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Total cost</div><div class="agent-stat-value">{fmtCost(s.total_cost_usd)}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Agent time</div><div class="agent-stat-value">{fmtElapsed(s.agent_time_seconds)}</div></div>
        <div class="agent-stat"><div class="agent-stat-label">Avg task time</div><div class="agent-stat-value">{fmtElapsed(s.avg_task_seconds)}</div></div>
      </div>
    );
  };

  const renderReflections = (data) => {
    const content = data && data.content;
    if (!content) return <div class="diff-empty">No reflections yet</div>;
    return <div class="agent-markdown-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(content) }} />;
  };

  const renderJournal = (data) => {
    const entries = data && data.entries ? data.entries : [];
    if (!entries.length) return <div class="diff-empty">No journal entries</div>;
    return entries.map((e, i) => (
      <div key={i} class="agent-log-session">
        <div class="agent-log-header" onClick={(e) => {
          e.target.closest(".agent-log-session").querySelector(".agent-log-arrow").classList.toggle("expanded");
          e.target.closest(".agent-log-session").querySelector(".agent-log-content").classList.toggle("expanded");
        }}>
          <span class={"agent-log-arrow" + (i === 0 ? " expanded" : "")}>&#9654;</span>
          {e.filename}
        </div>
        <div class={"agent-log-content" + (i === 0 ? " expanded" : "")}>
          <div class="agent-markdown-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(e.content) }} />
        </div>
      </div>
    ));
  };

  // --- Activity tab (live SSE stream, tail -f style) ---
  const renderActivity = () => {
    const allEntries = agentActivityLog.value;
    const entries = allEntries.filter(e => e.agent === agentName);

    // Auto-scroll to bottom on new entries
    useEffect(() => {
      if (tab === "activity" && activityEndRef.current) {
        activityEndRef.current.scrollIntoView({ behavior: "smooth" });
      }
    }, [entries.length, tab]);

    if (!entries.length) {
      return <div class="diff-empty">No activity yet — waiting for agent actions...</div>;
    }

    return (
      <div class="agent-activity-log">
        {entries.map((e, i) => {
          const toolLower = (e.tool || "").toLowerCase();
          const ts = e.timestamp ? new Date(e.timestamp).toLocaleTimeString() : "";
          return (
            <div key={i} class="agent-activity-entry">
              <span class="agent-activity-ts">{ts}</span>
              {e.task_id != null && (
                <span
                  class="agent-activity-task"
                  onClick={(ev) => { ev.stopPropagation(); pushPanel("task", e.task_id); }}
                  title={`Task ${taskIdStr(e.task_id)}`}
                >
                  {taskIdStr(e.task_id)}
                </span>
              )}
              <span class={"agent-activity-tool agent-activity-tool-" + toolLower}>{toolLower}</span>
              <span class="agent-activity-detail" title={e.detail || ""}>{e.detail || ""}</span>
            </div>
          );
        })}
        <div ref={activityEndRef} />
      </div>
    );
  };

  const TABS = ["activity", "inbox", "outbox", "logs", "reflections", "journal", "stats"];
  const data = tabData[tab];

  return (
    <>
      {role && <div class="diff-panel-role">{role}</div>}
      <div class="diff-panel-tabs">
        {TABS.map(t => (
          <button key={t} class={"diff-tab" + (tab === t ? " active" : "")} onClick={() => switchTab(t)}>
            {cap(t)}
          </button>
        ))}
      </div>
      <div class="diff-panel-body">
        {tab === "activity" ? renderActivity()
          : data === undefined ? <div class="diff-empty">Loading...</div>
          : tab === "inbox" ? renderInbox(data)
          : tab === "outbox" ? renderOutbox(data)
          : tab === "logs" ? renderLogs(data)
          : tab === "reflections" ? renderReflections(data)
          : tab === "journal" ? renderJournal(data)
          : renderStats(data)
        }
      </div>
    </>
  );
}

// ── File viewer ──
function FileView({ filePath }) {
  const team = currentTeam.value;
  const [fileData, setFileData] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!filePath || !team) return;
    setFileData(null); setError(null);
    const abortCtrl = new AbortController();
    let settled = false;
    // Normalise the file path for the backend.
    // Absolute paths inside delegate home are stripped to delegate-relative.
    // The backend resolves delegate-relative paths from hc_home.
    const apiPath = toApiPath(filePath, team);
    api.fetchFileContent(team, apiPath, { signal: abortCtrl.signal }).then(data => {
      settled = true;
      if (!abortCtrl.signal.aborted) setFileData(data);
    }).catch(e => {
      settled = true;
      if (!abortCtrl.signal.aborted) {
        console.error('FileView fetch failed:', e);
        setError((e && e.message) || String(e) || 'Failed to load file');
      }
    });
    // Safety: if the fetch hasn't settled after 8s, show an error
    const timeout = setTimeout(() => {
      if (!settled && !abortCtrl.signal.aborted) {
        setError("Request timed out — the file may not exist or the server is unreachable.");
      }
    }, 8000);
    return () => { abortCtrl.abort(); clearTimeout(timeout); };
  }, [filePath, team]);

  const ext = filePath ? (filePath.lastIndexOf(".") !== -1 ? filePath.substring(filePath.lastIndexOf(".") + 1).toLowerCase() : "") : "";

  const truncatePath = displayFilePath;

  const displayPath = truncatePath(filePath);
  const breadcrumb = displayPath ? displayPath.split("/").map((p, i, arr) => (
    <span key={i}>
      {i < arr.length - 1
        ? <><span class="file-breadcrumb-dir">{p}</span><span class="file-breadcrumb-sep">/</span></>
        : <span class="file-breadcrumb-current">{p}</span>
      }
    </span>
  )) : null;

  const modified = fileData?.modified || "";
  const imageExts = ["png", "jpg", "jpeg", "gif", "svg", "webp"];
  const isImage = imageExts.includes(ext);
  const htmlExts = ["html", "htm"];
  const isHtml = htmlExts.includes(ext);

  return (
    <>
      <div class="file-viewer-header">
        <div class="diff-panel-title">{breadcrumb}</div>
        <div class="diff-panel-branch">{modified ? "Modified " + fmtTimestamp(modified) : ""}</div>
      </div>
      <div class="diff-panel-body">
        {error ? <div class="diff-empty">{error}</div>
          : fileData === null ? <div class="diff-empty">Loading file...</div>
          : fileData.is_binary && isImage && fileData.content
            ? <div class="file-viewer-content file-viewer-image">
                <img src={`data:${fileData.content_type};base64,${fileData.content}`} alt={filePath} />
              </div>
          : fileData.is_binary
            ? <div class="diff-empty">Binary file ({fileData.size} bytes)</div>
          : (ext === "md" || ext === "markdown")
            ? <div class="file-viewer-content md-content" dangerouslySetInnerHTML={{ __html: renderMarkdown(fileData.content) }} />
          : isHtml && fileData.content
            ? <div class="file-viewer-content" style={{ padding: "40px 20px", textAlign: "center" }}>
                <div style={{ fontSize: "14px", color: "var(--text-secondary)", marginBottom: "16px" }}>
                  HTML files are opened in a new browser tab
                </div>
                <button
                  class="diff-panel-close-btn"
                  style={{ padding: "8px 16px" }}
                  onClick={() => {
                    window.open(`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(filePath, currentTeam.value))}`, "_blank");
                  }}
                >
                  Open in New Tab
                </button>
              </div>
            : <div class="file-viewer-content"><pre class="file-viewer-code"><code>{fileData.content}</code></pre></div>
        }
      </div>
    </>
  );
}

// ── Panel title helper (for back-bar) ──
function panelTitle(entry, allTasks) {
  if (!entry) return "";
  if (entry.type === "task") {
    const t = (allTasks || []).find(t => t.id === entry.target);
    return "T" + String(entry.target).padStart(4, "0") + (t ? " " + t.title : "");
  }
  if (entry.type === "agent") return cap(entry.target || "");
  if (entry.type === "file") return (entry.target || "").split("/").pop() || "File";
  return "";
}

// ── Main DiffPanel ──
export function DiffPanel() {
  const mode = diffPanelMode.value;
  const target = diffPanelTarget.value;
  const isOpen = mode !== null;
  const allTasks = tasks.value;

  const close = useCallback(() => { closeAllPanels(); }, []);

  const stack = panelStack.value;
  const hasPrev = stack.length > 1;
  const prev = hasPrev ? stack[stack.length - 2] : null;

  return (
    <>
      <div class={"diff-panel" + (isOpen ? " open" : "")}>
        {/* Back bar */}
        {hasPrev && (
          <div class="panel-back-bar" onClick={popPanel}>
            <span class="panel-back-arrow">&larr;</span> Back to {panelTitle(prev, allTasks)}
          </div>
        )}
        <div class="diff-panel-header">
          {mode === "diff" && <div class="diff-panel-title">{"T" + String(target).padStart(4, "0")}</div>}
          {mode === "agent" && <div class="diff-panel-title">{cap(target || "")}</div>}
          {mode === "file" && null /* FileView renders its own title */}
          <button class="diff-panel-close" onClick={close}>&times;</button>
        </div>
        {mode === "diff" && <DiffView taskId={target} />}
        {mode === "agent" && <AgentView agentName={target} />}
        {mode === "file" && <FileView filePath={target} />}
      </div>
      <div class={"diff-backdrop" + (isOpen ? " open" : "")} onClick={close}></div>
    </>
  );
}
