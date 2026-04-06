import { useState, useEffect } from "preact/hooks";
import { currentTeam, openPanel } from "../state.js";
import * as api from "../api.js";
import { taskIdStr } from "../utils.js";

/**
 * Renders /status command output - task-focused status summary.
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
        const tasksData = await api.fetchTasks(team);

        // Compute "today" and "this week" start times in UTC
        const now = new Date();
        const todayStart = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
        const dayOfWeek = now.getUTCDay(); // 0=Sunday, 1=Monday, ...
        const daysToMonday = dayOfWeek === 0 ? 6 : dayOfWeek - 1; // Sunday -> 6, Monday -> 0, Tuesday -> 1, etc.
        const weekStart = new Date(todayStart.getTime() - daysToMonday * 24 * 60 * 60 * 1000);

        // Count done tasks
        const doneTasks = tasksData.filter(t => t.status === 'done' && t.completed_at);
        const doneToday = doneTasks.filter(t => new Date(t.completed_at) >= todayStart).length;
        const doneThisWeek = doneTasks.filter(t => new Date(t.completed_at) >= weekStart).length;

        // Count pending tasks (non-done, non-cancelled)
        const pending = tasksData.filter(t => t.status !== 'done' && t.status !== 'cancelled').length;

        // Build per-status task ID arrays
        const statuses = {
          in_progress: tasksData.filter(t => t.status === 'in_progress').map(t => t.id),
          in_review: tasksData.filter(t => t.status === 'in_review').map(t => t.id),
          in_approval: tasksData.filter(t => t.status === 'in_approval').map(t => t.id),
          merge_failed: tasksData.filter(t => t.status === 'merge_failed').map(t => t.id),
          rejected: tasksData.filter(t => t.status === 'rejected').map(t => t.id),
          todo: tasksData.filter(t => t.status === 'todo').map(t => t.id),
        };

        setData({ doneToday, doneThisWeek, pending, statuses });
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
        <div class="status-header">Status</div>
        <div class="status-body">Loading...</div>
      </div>
    );
  }

  if (data?.error) {
    return (
      <div class="status-block error">
        <div class="status-header">Status</div>
        <div class="status-body">Error: {data.error}</div>
      </div>
    );
  }

  const { doneToday = 0, doneThisWeek = 0, pending = 0, statuses = {} } = data || {};

  const handleTaskClick = (e, taskId) => {
    e.preventDefault();
    openPanel('task', taskId);
  };

  const statusLabels = {
    in_progress: 'In Progress',
    in_review: 'In Review',
    in_approval: 'In Approval',
    merge_failed: 'Merge Failed',
    rejected: 'Rejected',
    todo: 'Todo',
  };

  const statusOrder = ['in_progress', 'in_review', 'in_approval', 'merge_failed', 'rejected', 'todo'];

  return (
    <div class="status-block">
      <div class="status-header">Status</div>
      <div class="status-body">
        <div class="status-done-line">
          Done: {doneToday} today, {doneThisWeek} this week
        </div>
        <div class="status-pending-line">
          Pending: {pending} total
        </div>
        <div class="status-breakdown">
          {statusOrder.map(status => {
            const taskIds = statuses[status] || [];
            if (taskIds.length === 0) return null;
            return (
              <div key={status} class="status-breakdown-row">
                <span class="status-breakdown-label">{statusLabels[status]}:</span>
                <span class="status-breakdown-count">{taskIds.length}</span>
                <span class="status-breakdown-tasks">
                  {taskIds.map((id, idx) => (
                    <>
                      {idx > 0 && ', '}
                      <a
                        href="#"
                        onClick={(e) => handleTaskClick(e, id)}
                      >
                        {taskIdStr(id)}
                      </a>
                    </>
                  ))}
                </span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
