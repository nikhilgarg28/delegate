import { useState, useEffect } from "preact/hooks";
import { currentTeam, openPanel } from "../state.js";
import * as api from "../api.js";
import { cap } from "../utils.js";

/**
 * Renders /status command output - system status summary.
 * This component is fully client-side and fetches data from existing endpoints.
 * @param {Object} props
 * @param {Object|null} props.result - Cached result data, or null to fetch live
 */
export function StatusBlock({ result }) {
  const [data, setData] = useState(result);
  const [loading, setLoading] = useState(!result);

  useEffect(() => {
    if (result) return; // Use cached result if provided

    const fetchStatus = async () => {
      try {
        const team = currentTeam.value;
        const [agentsData, tasksData] = await Promise.all([
          api.fetchAgents(team),
          api.fetchTasks(team),
        ]);

        // Build status summary
        const agents = agentsData.map(a => ({
          name: a.name,
          status: a.status || 'idle',
          current_task: a.current_task_id || null,
          last_turn: a.last_turn_at || null,
        }));

        const taskCounts = {
          todo: tasksData.filter(t => t.status === 'todo').length,
          in_progress: tasksData.filter(t => t.status === 'in_progress').length,
          in_review: tasksData.filter(t => t.status === 'in_review').length,
          in_approval: tasksData.filter(t => t.status === 'in_approval').length,
          total: tasksData.filter(t => t.status !== 'done' && t.status !== 'cancelled').length,
        };

        setData({ agents, taskCounts });
        setLoading(false);
      } catch (err) {
        console.error('Failed to fetch status:', err);
        setData({ error: err.message });
        setLoading(false);
      }
    };

    fetchStatus();
  }, [result]);

  if (loading) {
    return (
      <div class="status-block loading">
        <div class="status-header">System Status</div>
        <div class="status-body">Loading...</div>
      </div>
    );
  }

  if (data?.error) {
    return (
      <div class="status-block error">
        <div class="status-header">System Status</div>
        <div class="status-body">Error: {data.error}</div>
      </div>
    );
  }

  const { agents = [], taskCounts = {} } = data || {};

  const formatLastTurn = (timestamp) => {
    if (!timestamp) return 'never';
    const diff = Date.now() - new Date(timestamp).getTime();
    const minutes = Math.floor(diff / 60000);
    if (minutes < 1) return 'just now';
    if (minutes === 1) return '1m ago';
    if (minutes < 60) return `${minutes}m ago`;
    const hours = Math.floor(minutes / 60);
    if (hours === 1) return '1h ago';
    if (hours < 24) return `${hours}h ago`;
    const days = Math.floor(hours / 24);
    return `${days}d ago`;
  };

  const handleAgentClick = (e, agentName) => {
    e.preventDefault();
    openPanel('agent', agentName);
  };

  const handleTaskClick = (e, taskId) => {
    e.preventDefault();
    openPanel('task', taskId);
  };

  return (
    <div class="status-block">
      <div class="status-header">System Status</div>
      <div class="status-body">
        <div class="status-section">
          <div class="status-section-title">Agents</div>
          {agents.map(a => (
            <div key={a.name} class="status-agent-row">
              <a
                href="#"
                class="status-agent-name"
                onClick={(e) => handleAgentClick(e, a.name)}
              >
                {cap(a.name)}
              </a>
              <span class={`status-badge status-${a.status}`}>
                {a.status}
              </span>
              {a.current_task && (
                <a
                  href="#"
                  class="status-task-link"
                  onClick={(e) => handleTaskClick(e, a.current_task)}
                >
                  T{String(a.current_task).padStart(4, '0')}
                </a>
              )}
              <span class="status-last-turn">{formatLastTurn(a.last_turn)}</span>
            </div>
          ))}
        </div>

        <div class="status-section">
          <div class="status-section-title">Tasks</div>
          <div class="status-task-counts">
            {taskCounts.in_progress > 0 && (
              <span class="status-count">{taskCounts.in_progress} in_progress</span>
            )}
            {taskCounts.in_review > 0 && (
              <span class="status-count">{taskCounts.in_review} in_review</span>
            )}
            {taskCounts.in_approval > 0 && (
              <span class="status-count">{taskCounts.in_approval} in_approval</span>
            )}
            {taskCounts.todo > 0 && (
              <span class="status-count">{taskCounts.todo} todo</span>
            )}
          </div>
          <div class="status-total">{taskCounts.total} total open</div>
        </div>
      </div>
    </div>
  );
}
