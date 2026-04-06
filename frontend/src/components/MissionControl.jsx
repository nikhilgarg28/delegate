import { useEffect, useRef, useState } from "preact/hooks";
import {
  allTeamsAgents, allTeamsTurnState, tasks,
  agentActivityLog, agentThinking,
  openPanel, currentTeam,
} from "../state.js";
import { cap, taskIdStr, renderMarkdown, fmtStatus, useLiveTimer, useStreamingText, formatToolDetail } from "../utils.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build enriched agent data for ALL agents, sorted: active first, then idle alpha. */
function buildAgentList(agentsList, turnState, allTasks) {
  const result = [];

  for (const a of agentsList) {
    const team = a.team || "unknown";
    const turn = (turnState[team] || {})[a.name];
    const inTurn = turn?.inTurn ?? false;
    const lastTaskId = turn?.taskId ?? null;
    const sender = turn?.sender ?? "";
    const turnStartedAt = inTurn ? (turn?.startedAt ?? null) : null;

    let taskTitle = "";
    let taskStatus = "";
    if (lastTaskId) {
      const task = allTasks.find(t => t.id === lastTaskId);
      if (task) {
        taskTitle = task.title || "";
        taskStatus = task.status || "";
      }
    }

    result.push({
      name: a.name,
      team,
      role: a.role || "engineer",
      model: a.model || "sonnet",
      inTurn,
      taskId: lastTaskId,
      taskTitle,
      taskStatus,
      sender,
      turnStartedAt,
    });
  }

  // Active first (preserve order), then idle alphabetically
  result.sort((a, b) => {
    if (a.inTurn !== b.inTurn) return a.inTurn ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return result;
}

/** Get activity entries for a given agent's *current turn* from the log. */
function getAgentActivities(log, agentName) {
  // Find the last turn_separator for this agent — only show tools from after it
  let startIdx = 0;
  for (let i = log.length - 1; i >= 0; i--) {
    if (log[i].agent === agentName && log[i].type === "turn_separator") {
      startIdx = i + 1;
      break;
    }
  }
  return log.slice(startIdx).filter(e => e.agent === agentName && e.type === "agent_activity");
}

// ---------------------------------------------------------------------------
// Status summary — fits on one line next to the name
// ---------------------------------------------------------------------------

function StatusSummary({ agent }) {
  if (agent.taskId && agent.taskTitle) {
    const verb = getStatusVerb(agent.taskStatus);
    return (
      <span class="mc-status">
        {verb && <span>{verb} </span>}
        <span class="mc-task-id">{taskIdStr(agent.taskId)}</span>
      </span>
    );
  }
  if (agent.sender) return <span class="mc-status">responding to {cap(agent.sender)}</span>;
  if (agent.inTurn) return <span class="mc-status">working</span>;
  return <span class="mc-status">idle</span>;
}

function getStatusVerb(taskStatus) {
  switch (taskStatus) {
    case "in_progress": return "working on";
    case "in_review":   return "reviewing";
    case "merge_failed": return "fixing merge for";
    case "todo":        return "assigned to";
    default: return null;
  }
}

// ---------------------------------------------------------------------------
// Cycling verb — shown before first thinking text arrives
// ---------------------------------------------------------------------------

const THINKING_WORDS = [
  "thinking", "pondering", "noodling", "considering", "mulling",
  "reasoning", "deliberating", "reflecting", "processing", "contemplating",
];

function CyclingVerb() {
  const [index, setIndex] = useState(0);
  const [fading, setFading] = useState(false);

  useEffect(() => {
    const interval = setInterval(() => {
      setFading(true);
      setTimeout(() => {
        setIndex(prev => (prev + 1) % THINKING_WORDS.length);
        setFading(false);
      }, 200);
    }, 2500);
    return () => clearInterval(interval);
  }, []);

  return (
    <span class={"mc-cycling" + (fading ? " mc-cycling-out" : "")}>
      {THINKING_WORDS[index]}…
    </span>
  );
}

// ---------------------------------------------------------------------------
// SVG Icons
// ---------------------------------------------------------------------------

/** Small thinking icon — aligned with the agent dot */
function ThinkingIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 16 16" fill="none"
         stroke="currentColor" strokeWidth="1.5"
         strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="6" r="5" />
      <path d="M6 11.5c0 1 .9 2.5 2 2.5s2-1.5 2-2.5" />
    </svg>
  );
}

function ToolIcon({ tool }) {
  const t = tool.toLowerCase();
  const p = {
    width: "12", height: "12", viewBox: "0 0 16 16",
    fill: "none", stroke: "currentColor",
    strokeWidth: "1.5", strokeLinecap: "round", strokeLinejoin: "round",
  };

  if (t.includes("read") || t.includes("cat") || t.includes("view")) {
    return (
      <svg {...p}>
        <path d="M1 8s3-5.5 7-5.5S15 8 15 8s-3 5.5-7 5.5S1 8 1 8z" />
        <circle cx="8" cy="8" r="2" />
      </svg>
    );
  }
  if (t.includes("write") || t.includes("edit") || t.includes("patch") || t.includes("replace")) {
    return <svg {...p}><path d="M11.5 1.5l3 3L5 14H2v-3z" /></svg>;
  }
  if (t.includes("bash") || t.includes("shell") || t.includes("exec") || t.includes("run")) {
    return <svg {...p}><path d="M2 12l4-4-4-4" /><path d="M8 12h6" /></svg>;
  }
  if (t.includes("search") || t.includes("grep") || t.includes("find")) {
    return <svg {...p}><circle cx="7" cy="7" r="4" /><path d="M10.5 10.5L14 14" /></svg>;
  }
  if (t.includes("list") || t.includes("ls") || t.includes("glob")) {
    return <svg {...p}><path d="M3 4h10" /><path d="M3 8h10" /><path d="M3 12h10" /></svg>;
  }
  return <svg {...p}><circle cx="8" cy="8" r="2.5" fill="currentColor" stroke="none" /></svg>;
}

// ---------------------------------------------------------------------------
// AgentRow — unified: one line for all agents, expandable body for active ones
// ---------------------------------------------------------------------------

function AgentRow({ agent, thinking, activities, suppressBody }) {
  const streamRef = useRef(null);
  const thinkingText = thinking?.text || "";
  const turnAge = useLiveTimer(agent.inTurn ? agent.turnStartedAt : null);
  const revealedLen = useStreamingText(thinkingText);

  // Auto-scroll as more words are revealed
  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [revealedLen]);

  const revealedText = thinkingText.slice(0, revealedLen);
  const hasThinking = revealedText.length > 0;

  // ── Tool display: show last 2 tools, but hide if thinking arrived after them ──
  const breaks = (thinkingText.match(/\n\n---\n\n/g) || []).length;
  const unconsumedTools = Math.max(0, activities.length - breaks);
  const recentTools = unconsumedTools > 0
    ? activities.slice(-Math.min(unconsumedTools, 2))
    : [];

  return (
    <div class={"mc-row" + (agent.inTurn ? " mc-row-active" : "")} onClick={() => openPanel("agent", agent.name)}>
      {/* ── Line 1: dot · name · status · timer ── */}
      <div class="mc-row-header">
        <span class={"mc-dot " + (agent.inTurn ? "dot-active" : "dot-idle")} />
        <span class="mc-name">{cap(agent.name)}</span>
        <StatusSummary agent={agent} />
        {agent.inTurn && turnAge && <span class="live-timer mc-timer">{turnAge}</span>}
      </div>

      {/* ── Active body: thinking stream + tools ── */}
      {agent.inTurn && !suppressBody && (
        <div class="mc-row-body">
          {/* Thinking stream (or cycling verb placeholder) */}
          {hasThinking ? (
            <div
              class="mc-thinking-stream"
              ref={streamRef}
              onClick={(e) => e.stopPropagation()}
              dangerouslySetInnerHTML={{ __html: renderMarkdown(revealedText) }}
            />
          ) : (
            <div class="mc-cycling-wrap"><CyclingVerb /></div>
          )}

          {/* Tool entries — last 2 from current epoch */}
          {recentTools.map((act, i) => {
            const detail = formatToolDetail(act.tool, act.detail);
            return (
              <div key={`${act.tool}-${act.timestamp}-${i}`} class="mc-tool-line" onClick={(e) => e.stopPropagation()}>
                <span class="mc-tool-name">{act.tool.toLowerCase()}</span>
                {detail && <span class="mc-tool-detail">{detail}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// TaskRow — one row per active task, with a live elapsed timer
// ---------------------------------------------------------------------------

function TaskRow({ task, active }) {
  const age = useLiveTimer(task.updated_at ?? null);
  return (
    <div class="mc-task-row" onClick={() => openPanel("task", task.id)}>
      <div class="mc-task-row-line1">
        <span class={"mc-dot " + (active ? "dot-active" : "dot-idle")} />
        <span class="mc-task-id">{taskIdStr(task.id, task.prefix, task.seq)}</span>
        <span class="mc-task-title-inline">{task.title}</span>
      </div>
      <div class="mc-task-row-line2">
        <span class="mc-task-assignee">{task.assignee ? cap(task.assignee) : "—"}</span>
        <span class="mc-task-meta-right">
          <span class={"badge badge-" + task.status}>{fmtStatus(task.status)}</span>
          {age && <span class="live-timer mc-timer">{age}</span>}
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// isTaskActive — true if any agent in the given team is inTurn on this taskId
// ---------------------------------------------------------------------------

function isTaskActive(taskId, turnState, team) {
  const teamTurns = turnState[team] || {};
  return Object.values(teamTurns).some(t => t.inTurn && t.taskId === taskId);
}

// ---------------------------------------------------------------------------
// MissionControl
// ---------------------------------------------------------------------------

export function MissionControl() {
  const turnState = allTeamsTurnState.value;
  const thinking = agentThinking.value;
  const allAgentsList = allTeamsAgents.value;
  const allTasks = tasks.value;
  const activityLog = agentActivityLog.value;
  const team = currentTeam.value;

  const teamAgents = allAgentsList.filter(a => a.team === team);
  const agentRows = buildAgentList(teamAgents, turnState, allTasks);

  // Manager (Delegate) lives in the chat footer — exclude from sidebar
  const subAgentRows = agentRows.filter(a => a.role !== "manager");

  const EXCLUDED_STATUSES = new Set(["done", "cancelled", "rejected"]);
  const activeTasks = allTasks
    .filter(t => t.team === team && !EXCLUDED_STATUSES.has(t.status))
    .sort((a, b) => (b.updated_at || "").localeCompare(a.updated_at || ""));

  return (
    <div class="mc">
      <div class="mc-panel mc-agents-panel">
        {/* Section 1: Sub-agents (Delegate shown in chat footer) */}
        <div class="mc-section-heading">Agents</div>
        {subAgentRows.length === 0
          ? <div class="mc-empty">No agents</div>
          : subAgentRows.map(a => (
              <AgentRow
                key={`${a.team}-${a.name}`}
                agent={a}
                thinking={thinking[a.name]}
                activities={getAgentActivities(activityLog, a.name)}
              />
            ))
        }
      </div>

      <div class="mc-panel mc-tasks-panel">
        {/* Section 2: Active Tasks */}
        <div class="mc-section-heading">Active Tasks</div>
        {activeTasks.length === 0
          ? <div class="mc-empty">No active tasks</div>
          : activeTasks.map(task => (
              <TaskRow
                key={task.id}
                task={task}
                active={isTaskActive(task.id, turnState, team)}
              />
            ))
        }
      </div>
    </div>
  );
}
