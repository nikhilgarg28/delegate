import { openPanel } from "../state.js";

/**
 * Renders /cost command output - cost analytics summary.
 * @param {Object} props
 * @param {Object|null} props.result - Cost summary data from backend
 */
export function CostBlock({ result }) {
  if (!result) {
    return (
      <div class="cost-block error">
        <div class="cost-header">Cost Summary</div>
        <div class="cost-body">Error loading cost data</div>
      </div>
    );
  }

  const { today, this_week, top_tasks } = result;

  const formatCost = (amount) => {
    return `$${amount.toFixed(2)}`;
  };

  const formatTaskTitle = (title) => {
    if (!title) return "Unknown";
    return title.length > 40 ? title.slice(0, 40) + "..." : title;
  };

  const handleTaskClick = (e, taskId) => {
    e.preventDefault();
    openPanel('task', taskId);
  };

  return (
    <div class="cost-block">
      <div class="cost-header">Cost Summary</div>
      <div class="cost-body">
        <div class="cost-section">
          <div class="cost-row">
            <span class="cost-label">Today:</span>
            <span class="cost-value">
              {formatCost(today.total_cost_usd)} across {today.task_count} task{today.task_count !== 1 ? 's' : ''}
              {today.task_count > 0 && ` (avg ${formatCost(today.avg_cost_per_task)}/task)`}
            </span>
          </div>
          <div class="cost-row">
            <span class="cost-label">This Week:</span>
            <span class="cost-value">
              {formatCost(this_week.total_cost_usd)} across {this_week.task_count} task{this_week.task_count !== 1 ? 's' : ''}
              {this_week.task_count > 0 && ` (avg ${formatCost(this_week.avg_cost_per_task)}/task)`}
            </span>
          </div>
        </div>

        {top_tasks && top_tasks.length > 0 && (
          <div class="cost-section">
            <div class="cost-section-title">Top Tasks by Cost:</div>
            {top_tasks.map(task => (
              <div key={task.task_id} class="cost-task-row">
                <a
                  href="#"
                  class="cost-task-id"
                  onClick={(e) => handleTaskClick(e, task.task_id)}
                >
                  T{String(task.task_id).padStart(4, '0')}
                </a>
                <span class="cost-task-title">{formatTaskTitle(task.title)}</span>
                <span class="cost-task-amount">{formatCost(task.cost_usd)}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
