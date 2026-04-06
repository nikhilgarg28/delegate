import { useEffect, useRef, useState } from "preact/hooks";
import {
  managerTurnContext, agentLastActivity, agentActivityLog,
  agentThinking, agents, tasks, openPanel, currentTeam,
} from "../state.js";
import { cap, taskIdStr, renderMarkdown, useStreamingText, formatToolDetail } from "../utils.js";
import { showToast } from "../toast.js";
import * as api from "../api.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const THINKING_WORDS = [
  "thinking",
  "pondering",
  "noodling",
  "considering",
  "mulling",
  "reasoning",
  "deliberating",
  "reflecting",
  "processing",
  "contemplating",
];

/**
 * Build a concise status string from turn context.
 *   - "responding to Nikhil"
 *   - "responding to Alex about T123"
 *   - "working on T123"
 *   - null (fallback → cycling verb)
 */
function buildStatus(turnCtx, allTasks) {
  if (!turnCtx) return null;

  const rawSender = turnCtx.sender || "";
  const sender = rawSender.toLowerCase() === "system" ? "" : (rawSender ? cap(rawSender) : "");
  const taskId = turnCtx.task_id > 0 ? turnCtx.task_id : null;

  if (sender && taskId) {
    return (<>responding to <span class="delegate-footer-sender">{sender}</span> · <span class="delegate-footer-taskid">{taskIdStr(taskId)}</span></>);
  }
  if (sender) {
    return (<>responding to <span class="delegate-footer-sender">{sender}</span></>);
  }
  if (taskId) {
    return (<>working on <span class="delegate-footer-taskid">{taskIdStr(taskId)}</span></>);
  }
  return null;
}

// ---------------------------------------------------------------------------
// CyclingVerb — animated thinking placeholder
// ---------------------------------------------------------------------------

function CyclingVerb() {
  const [index, setIndex] = useState(0);
  const [isTransitioning, setIsTransitioning] = useState(false);

  useEffect(() => {
    const interval = setInterval(() => {
      setIsTransitioning(true);
      setTimeout(() => {
        setIndex((prev) => (prev + 1) % THINKING_WORDS.length);
        setIsTransitioning(false);
      }, 200);
    }, 5000);
    return () => clearInterval(interval);
  }, []);

  return (
    <span class={"cycling-verb" + (isTransitioning ? " cycling-out" : " cycling-in")}>
      {THINKING_WORDS[index]}…
    </span>
  );
}

// ---------------------------------------------------------------------------
// DelegateThinkingFooter
//
// Glass card in the chat panel showing Delegate's live thinking.
//
// Header:  [•••]  Delegate  · responding to Nikhil
// Body:    streamed thinking text + tool entries
// ---------------------------------------------------------------------------

export function ManagerActivityBar() {
  const turnCtx = managerTurnContext.value;
  const thinking = agentThinking.value;
  const activityLog = agentActivityLog.value;
  const allTasks = tasks.value;
  const streamRef = useRef(null);

  // Identify the manager agent
  const agentList = agents.value;
  const managerAgent = agentList.find(a => a.role === "manager");
  const isActive = turnCtx && managerAgent && managerAgent.name === turnCtx.agent;

  // Derive manager data — always computed (never behind early returns)
  const managerName = turnCtx?.agent || "";
  const thinkingData = isActive ? thinking[managerName] : null;
  const thinkingText = thinkingData?.text || "";
  const revealedLen = useStreamingText(thinkingText);
  const revealedText = thinkingText.slice(0, revealedLen);
  const hasThinking = revealedText.length > 0;

  // Tool epoch logic (same as MissionControl AgentRow)
  // Only consider activities from the current turn (after the last turn_separator)
  const agentActivities = (() => {
    if (!isActive) return [];
    let startIdx = 0;
    for (let i = activityLog.length - 1; i >= 0; i--) {
      if (activityLog[i].agent === managerName && activityLog[i].type === "turn_separator") {
        startIdx = i + 1;
        break;
      }
    }
    return activityLog.slice(startIdx).filter(e => e.agent === managerName && e.type === "agent_activity");
  })();
  const breaks = (thinkingText.match(/\n\n---\n\n/g) || []).length;
  const unconsumedTools = Math.max(0, agentActivities.length - breaks);
  const recentTools = unconsumedTools > 0
    ? agentActivities.slice(-Math.min(unconsumedTools, 2))
    : [];

  // Status line: "responding to X" / "working on T123" / null
  const status = isActive ? buildStatus(turnCtx, allTasks) : null;

  // ── Hooks (always called unconditionally) ──

  // Safety timeout: clear if no activity for 120s (SSE stall / disconnect).
  useEffect(() => {
    if (!turnCtx) return;
    const timer = setTimeout(() => {
      if (managerTurnContext.value && managerTurnContext.value.agent === turnCtx.agent) {
        managerTurnContext.value = null;
      }
    }, 120000);
    return () => clearTimeout(timer);
  }, [turnCtx?.agent, turnCtx?.timestamp]);

  // Auto-scroll thinking stream as more words are revealed
  useEffect(() => {
    const el = streamRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [revealedLen]);

  // Keep chat log scrolled to bottom as footer grows/appears
  useEffect(() => {
    if (!isActive) return;
    const chatLog = document.querySelector(".chat-log");
    if (chatLog) {
      // Small delay lets the CSS transition start so the chat-log has resized
      requestAnimationFrame(() => { chatLog.scrollTop = chatLog.scrollHeight; });
    }
  }, [isActive, hasThinking]);

  // ── Render ──

  // Hidden — Delegate idle
  if (!isActive) {
    return <div class="delegate-footer" />;
  }

  const [interrupting, setInterrupting] = useState(false);
  const onInterrupt = async (e) => {
    e.stopPropagation();
    setInterrupting(true);
    try {
      await api.interruptAgent(managerName, currentTeam.value);
      showToast(`Interrupted ${cap(managerName)}`, "success");
    } catch (err) {
      showToast(`Interrupt failed: ${err.message}`, "error");
    } finally {
      setInterrupting(false);
    }
  };

  return (
    <div class={"delegate-footer delegate-footer-active" + (hasThinking ? " delegate-footer-expanded" : "")}>
      {/* Header: dots · Delegate · status — click to open side panel */}
      <div class="delegate-footer-bar" onClick={() => openPanel("agent", managerName)} style="cursor:pointer">
        <span class="delegate-footer-dots">
          <span class="delegate-dot" />
          <span class="delegate-dot" />
          <span class="delegate-dot" />
        </span>
        <span class="delegate-footer-name">{cap(managerName)}</span>
        {status
          ? <span class="delegate-footer-status">{status}</span>
          : <span class="delegate-footer-verb"><CyclingVerb /></span>
        }
        <button
          class="delegate-footer-interrupt"
          disabled={interrupting}
          onClick={onInterrupt}
          title="Stop this turn"
        >
          {interrupting ? "..." : "Stop"}
        </button>
      </div>

      {/* Body — thinking stream + tools */}
      {hasThinking && (
        <div class="delegate-footer-body">
          <div
            class="delegate-thinking-stream"
            ref={streamRef}
            dangerouslySetInnerHTML={{ __html: renderMarkdown(revealedText) }}
          />

          {recentTools.map((act, i) => {
            const detail = formatToolDetail(act.tool, act.detail);
            return (
              <div key={`${act.tool}-${act.timestamp}-${i}`} class="delegate-tool-line">
                <span class="delegate-tool-name">{act.tool.toLowerCase()}</span>
                {detail && <span class="delegate-tool-detail">{detail}</span>}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
