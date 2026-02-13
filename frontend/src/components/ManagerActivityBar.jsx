import { useEffect } from "preact/hooks";
import { managerTurnContext, agentLastActivity, agents } from "../state.js";
import { taskIdStr } from "../utils.js";

function cap(str) {
  return str ? str.charAt(0).toUpperCase() + str.slice(1) : "";
}

function truncate(str, maxLen) {
  if (!str || str.length <= maxLen) return str;
  return str.slice(0, maxLen) + "\u2026";
}

export function ManagerActivityBar() {
  const turnCtx = managerTurnContext.value;
  const lastActivity = agentLastActivity.value;

  // Safety timeout: clear if no activity for 30s (SSE stall / disconnect).
  useEffect(() => {
    if (!turnCtx) return;
    const timer = setTimeout(() => {
      if (managerTurnContext.value && managerTurnContext.value.agent === turnCtx.agent) {
        managerTurnContext.value = null;
      }
    }, 30000);
    return () => clearTimeout(timer);
  }, [turnCtx?.agent, turnCtx?.timestamp]);

  // Always render the wrapper so the reserved space stays.
  // Content is only shown when the manager has an active turn.
  const agentList = agents.value;
  const managerAgent = agentList.find(a => a.role === "manager");
  const isActive = turnCtx && managerAgent && managerAgent.name === turnCtx.agent;

  if (!isActive) {
    return <div class="manager-activity-bar" />;
  }

  // --- Context: <Manager> | TNNN  or  <Manager> → <Sender> ---
  const managerName = cap(turnCtx.agent);
  let context;
  if (turnCtx.task_id != null) {
    context = (
      <>
        <span class="mgr-name">{managerName}</span>
        <span class="mgr-sep"> | </span>
        <span class="mgr-task">{taskIdStr(turnCtx.task_id)}</span>
      </>
    );
  } else {
    context = (
      <>
        <span class="mgr-name">{managerName}</span>
        <span class="mgr-sep"> → </span>
        <span class="mgr-sender">{cap(turnCtx.sender || "")}</span>
      </>
    );
  }

  // --- Status: "thinking..." or "<tool>: <detail>" ---
  let status;
  const activity = lastActivity[turnCtx.agent];
  if (activity && activity.tool) {
    const ageMs = Date.now() - new Date(activity.timestamp).getTime();
    if (ageMs < 10000) {
      const detail = activity.detail ? ": " + truncate(activity.detail, 48) : "";
      status = (
        <span class="mgr-status">
          <span class="mgr-tool">{activity.tool.toLowerCase()}</span>{detail}
        </span>
      );
    }
  }
  if (!status) {
    status = <span class="mgr-status mgr-thinking">thinking…</span>;
  }

  return (
    <div class="manager-activity-bar">
      <div class="manager-typing-dots">
        <span class="manager-typing-dot" />
        <span class="manager-typing-dot" />
        <span class="manager-typing-dot" />
      </div>
      <div class="manager-activity-content">
        <span class="mgr-context">{context}</span>
        {status}
      </div>
    </div>
  );
}
