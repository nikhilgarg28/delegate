import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, diffPanelMode, diffPanelTarget, tasks,
  panelStack, pushPanel, closeAllPanels, popPanel,
  agentActivityLog, agents, allTeamsTurnState,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtTimestamp, fmtElapsed, fmtTokens, fmtCost,
  flattenDiffDict, flattenCommitsDict, diff2HtmlRender, diff2HtmlParse,
  renderMarkdown, msgStatusIcon, taskIdStr, toApiPath, displayFilePath,
  fmtCompactDuration,
} from "../utils.js";
import { showToast } from "../toast.js";

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

import hljs from "highlight.js";

const EXT_TO_LANG = {
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript",
  py: "python", pyw: "python",
  go: "go",
  rs: "rust",
  java: "java",
  html: "xml", htm: "xml", xml: "xml", svg: "xml",
  css: "css", scss: "css", less: "css",
  json: "json",
  yaml: "yaml", yml: "yaml",
  toml: "ini", ini: "ini", cfg: "ini",
  sh: "bash", bash: "bash", zsh: "bash",
  sql: "sql",
  md: "markdown", markdown: "markdown",
  diff: "diff", patch: "diff",
  dockerfile: "dockerfile",
  makefile: "makefile",
};

function hlLang(ext, fileName) {
  // Check exact filename matches first (for files like Makefile, Dockerfile)
  const base = fileName ? fileName.split("/").pop().toLowerCase() : "";
  if (base === "makefile" || base === "gnumakefile") return "makefile";
  if (base === "dockerfile") return "dockerfile";
  return EXT_TO_LANG[ext] || null;
}

// ── Diff viewer (task diff) ──
function DiffView({ taskId }) {
  const team = currentTeam.value;
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("files");
  const [rawDiff, setRawDiff] = useState("");

  useEffect(() => {
    if (!taskId) return;
    setData(null); setTab("files"); setRawDiff("");
    api.fetchTaskDiff(taskId).then(d => {
      setData(d);
      setRawDiff(flattenDiffDict(d.diff));
    }).catch(() => {});
  }, [taskId]);

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
  const activityEndRef = useRef(null);

  // Get role directly from agents signal (no async fetch needed)
  const agent = agents.value.find(x => x.name === agentName);
  const role = agent?.role ? cap(agent.role) : "";

  const switchTab = useCallback((t) => {
    setTab(t);
    if (t !== "activity" && !tabData[t]) {
      api.fetchAgentTab(team, agentName, t).then(d => {
        setTabData(prev => ({ ...prev, [t]: d }));
      }).catch(() => {});
    }
  }, [team, agentName, tabData]);

  // Backfill activity log from REST endpoint when Activity tab opens
  useEffect(() => {
    if (tab === "activity" && team && agentName) {
      const allEntries = agentActivityLog.value;
      const entries = allEntries.filter(e => e.agent === agentName);

      // Only backfill if we have no entries for this agent
      if (entries.length === 0) {
        api.fetchAgentActivity(team, agentName, 100).then(backfillEntries => {
          if (backfillEntries && backfillEntries.length > 0) {
            // Merge backfilled entries into the activity log, deduping by timestamp
            const existingTimestamps = new Set(allEntries.map(e => e.timestamp));
            const newEntries = backfillEntries.filter(e => !existingTimestamps.has(e.timestamp));

            if (newEntries.length > 0) {
              agentActivityLog.value = [...allEntries, ...newEntries];
            }
          }
        }).catch(() => {});
      }
    }
  }, [tab, team, agentName]);

  const renderMessages = (msgs) => {
    if (!msgs || !msgs.length) return <div class="diff-empty">No messages</div>;
    return msgs.map((m, i) => {
      const isIncoming = m.direction === "in";
      const unprocessed = isIncoming && !m.processed_at;
      const arrow = isIncoming ? "&larr;" : "&rarr;";

      return (
        <div key={i} class={"agent-msg" + (unprocessed ? " unread" : "")}>
          <div class="agent-msg-header">
            <span class="agent-msg-direction" dangerouslySetInnerHTML={{ __html: arrow }} />
            <span class="agent-msg-sender">{cap(m.counterparty)}</span>
            {m.task_id != null && (
              <>
                <span class="msg-task-sep">|</span>
                <span
                  class="msg-task-badge"
                  style="cursor:pointer"
                  onClick={(ev) => { ev.stopPropagation(); pushPanel("task", m.task_id); }}
                  title={`Task ${taskIdStr(m.task_id)}`}
                >{taskIdStr(m.task_id)}</span>
              </>
            )}
            <span class="agent-msg-time" dangerouslySetInnerHTML={{ __html: fmtTimestamp(m.time) + " " + msgStatusIcon(m) }} />
          </div>
          <div class="agent-msg-body collapsed" onClick={(e) => e.target.classList.toggle("collapsed")}>
            {m.body}
          </div>
        </div>
      );
    });
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
          // Render turn separator
          if (e.type === "turn_separator") {
            let label = "Turn ended";
            if (e.sender && e.task_id != null) {
              label = `Responding to ${e.sender} about ${taskIdStr(e.task_id)}`;
            } else if (e.task_id != null) {
              label = `Working on ${taskIdStr(e.task_id)}`;
            } else if (e.sender) {
              label = `Responding to ${e.sender}`;
            }
            return (
              <div key={i} class="agent-activity-separator">
                <span class="agent-activity-separator-line"></span>
                <span class="agent-activity-separator-text">{label}</span>
                <span class="agent-activity-separator-line"></span>
              </div>
            );
          }

          // Render regular activity entry
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

  const TABS = ["activity", "messages", "logs", "reflections", "journal", "stats"];
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
          : tab === "messages" ? renderMessages(data)
          : tab === "logs" ? renderLogs(data)
          : tab === "reflections" ? renderReflections(data)
          : tab === "journal" ? renderJournal(data)
          : renderStats(data)
        }
      </div>
    </>
  );
}

// ── Byte formatter for directory listings ──
function formatBytes(bytes) {
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

// ── File viewer ──
// NOTE: useEffect / useSignalEffect do NOT fire reliably inside
// signal-driven parent renders (@preact/signals v2 commit-phase bug).
// We use a render-phase fetch pattern instead: the fetch is kicked off
// during render when the inputs change (guarded by a key stored in
// state to prevent infinite loops).  This is the same pattern React
// documents as "adjusting state during rendering".
function FileView({ filePath }) {
  const team = currentTeam.value;
  const [fileData, setFileData] = useState(null);
  const [error, setError] = useState(null);
  const [fetchKey, setFetchKey] = useState(null);
  const abortRef = useRef(null);

  const key = `${team}|${filePath}`;
  if (filePath && team && key !== fetchKey) {
    // New inputs detected during render — kick off fetch
    setFetchKey(key);
    setFileData(null);
    setError(null);

    if (abortRef.current) abortRef.current.abort();
    const ctrl = new AbortController();
    abortRef.current = ctrl;

    const apiPath = toApiPath(filePath, team);
    api.fetchFileContent(team, apiPath, { signal: ctrl.signal }).then(data => {
      if (!ctrl.signal.aborted) setFileData(data);
    }).catch(e => {
      if (!ctrl.signal.aborted) {
        setError((e && e.message) || String(e) || 'Failed to load file');
      }
    });
  }

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

  const mdHtml = useMemo(
    () => (ext === "md" || ext === "markdown") && fileData?.content ? renderMarkdown(fileData.content) : null,
    [fileData?.content, ext]
  );

  const highlightedHtml = useMemo(() => {
    if (!fileData?.content || fileData.is_binary || fileData.is_directory) return null;
    try {
      const lang = hlLang(ext, filePath);
      if (lang) {
        return hljs.highlight(fileData.content, { language: lang }).value;
      }
      // Auto-detect language for extensions not in EXT_TO_LANG
      const result = hljs.highlightAuto(fileData.content);
      if (result.relevance > 5) return result.value;
      return null; // low confidence -- fall back to plain text
    } catch {
      return null;
    }
  }, [fileData?.content, ext, filePath]);

  return (
    <>
      <div class="file-viewer-header">
        <div class="diff-panel-title">{breadcrumb}</div>
        <div class="diff-panel-branch">{modified ? "Modified " + fmtTimestamp(modified) : ""}</div>
      </div>
      <div class="diff-panel-body">
        {error ? <div class="diff-empty">{error}</div>
          : fileData === null ? <div class="diff-empty">Loading file...</div>
          : fileData.is_directory
            ? <div class="file-viewer-content file-viewer-directory">
                <div class="file-viewer-dir-list">
                  {fileData.files.map((entry, i) => (
                    <div key={i} class="file-viewer-dir-entry" onClick={() => pushPanel("file", entry.path)}>
                      <span class="file-viewer-dir-name">{entry.is_dir ? entry.name + "/" : entry.name}</span>
                      {!entry.is_dir && <span class="file-viewer-dir-meta">{formatBytes(entry.size)}</span>}
                    </div>
                  ))}
                  {fileData.files.length === 0 && <div class="diff-empty">Empty directory</div>}
                </div>
              </div>
          : fileData.is_binary && isImage && fileData.content
            ? <div class="file-viewer-content file-viewer-image">
                <img src={`data:${fileData.content_type};base64,${fileData.content}`} alt={filePath} />
              </div>
          : fileData.is_binary
            ? <div class="diff-empty">Binary file ({fileData.size} bytes)</div>
          : mdHtml
            ? <div class="file-viewer-content md-content" dangerouslySetInnerHTML={{ __html: mdHtml }} />
          : isHtml && fileData.content
            ? <div class="file-viewer-html-container">
                <div class="file-viewer-html-toolbar">
                  <button
                    class="file-viewer-open-tab-btn"
                    title="Open in new tab"
                    onClick={() => {
                      window.open(`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(filePath, currentTeam.value))}`, "_blank");
                    }}
                  >
                    {/* External link icon - simple SVG */}
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5">
                      <path d="M6 3H3v10h10v-3" />
                      <path d="M9 2h5v5" />
                      <path d="M14 2L7 9" />
                    </svg>
                  </button>
                </div>
                <iframe
                  class="file-viewer-iframe"
                  src={`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(filePath, currentTeam.value))}`}
                  sandbox="allow-same-origin"
                />
              </div>
            : highlightedHtml
              ? <div class="file-viewer-content"><pre class="file-viewer-code"><code class="hljs" dangerouslySetInnerHTML={{ __html: highlightedHtml }} /></pre></div>
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
    return taskIdStr(entry.target) + (t ? " " + t.title : "");
  }
  if (entry.type === "agent") return cap(entry.target || "");
  if (entry.type === "file") return (entry.target || "").split("/").pop() || "File";
  return "";
}

// ── Agent action buttons (Nudge / Interrupt) ──
function AgentActionButtons({ agentName }) {
  const team = currentTeam.value;
  const turnState = allTeamsTurnState.value;
  const turn = turnState[team]?.[agentName];
  const isRunning = !!turn?.inTurn;

  const [nudging, setNudging] = useState(false);
  const [interrupting, setInterrupting] = useState(false);

  const onNudge = async () => {
    setNudging(true);
    try {
      await api.nudgeAgent(agentName, team);
      showToast(`Nudged ${agentName}`, "success");
    } catch (e) {
      showToast(`Nudge failed: ${e.message}`, "error");
    } finally {
      setNudging(false);
    }
  };

  const onInterrupt = async () => {
    setInterrupting(true);
    try {
      await api.interruptAgent(agentName, team);
      showToast(`Interrupted ${agentName}`, "success");
    } catch (e) {
      showToast(`Interrupt failed: ${e.message}`, "error");
    } finally {
      setInterrupting(false);
    }
  };

  return (
    <div class="agent-action-buttons">
      <button class="btn-secondary" disabled={nudging} onClick={onNudge}>
        {nudging ? "..." : "Nudge"}
      </button>
      {isRunning && (
        <button class="btn-secondary btn-interrupt" disabled={interrupting} onClick={onInterrupt}>
          {interrupting ? "..." : "Interrupt"}
        </button>
      )}
    </div>
  );
}

// ── Agent turn timer (inner component to isolate setInterval hook) ──
function AgentTurnTimer({ agentName }) {
  const team = currentTeam.value;
  const turnState = allTeamsTurnState.value;
  const turn = turnState[team]?.[agentName];
  const startedAt = turn?.inTurn ? turn.startedAt : null;
  const elapsed = useLiveTimer(startedAt);
  if (!elapsed) return null;
  return <span class="live-timer">{elapsed}</span>;
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
          {mode === "diff" && <div class="diff-panel-title">{taskIdStr(target)}</div>}
          {mode === "agent" && (
            <div class="diff-panel-title diff-panel-title-agent">
              <span>{cap(target || "")}</span>
              <AgentActionButtons agentName={target} />
              <AgentTurnTimer agentName={target} />
            </div>
          )}
          {mode === "file" && null /* FileView renders its own title */}
          <button class="diff-panel-close" onClick={close}>&times;</button>
        </div>
        {mode === "diff" && <DiffView taskId={target} />}
        {mode === "agent" && <AgentView key={target} agentName={target} />}
        {mode === "file" && <FileView filePath={target} />}
      </div>
      <div class={"diff-backdrop" + (isOpen ? " open" : "")} onClick={close}></div>
    </>
  );
}
