import { signal } from "@preact/signals";

export const toasts = signal([]);

const MAX_TOASTS = 3;

export function showToast(message, type = "error") {
  const id = Date.now();

  // Add new toast
  const newToasts = [...toasts.value, { id, message, type }];

  // If exceeding max, remove oldest
  if (newToasts.length > MAX_TOASTS) {
    toasts.value = newToasts.slice(-MAX_TOASTS);
  } else {
    toasts.value = newToasts;
  }

  // Auto-dismiss after 5 seconds
  setTimeout(() => dismissToast(id), 5000);
}

export function showActionToast({ title, body, taskId, type = "info" }) {
  const id = Date.now();

  // Add new action toast
  const newToasts = [...toasts.value, { id, title, body, taskId, type }];

  // If exceeding max, remove oldest
  if (newToasts.length > MAX_TOASTS) {
    toasts.value = newToasts.slice(-MAX_TOASTS);
  } else {
    toasts.value = newToasts;
  }

  // Auto-dismiss after 8 seconds for action toasts
  setTimeout(() => dismissToast(id), 8000);
}

export function showReturnToast(summary) {
  const id = Date.now();

  // Build summary message
  const parts = [];
  if (summary.actionCount > 0) parts.push(`${summary.actionCount} task${summary.actionCount > 1 ? 's' : ''} need attention`);
  if (summary.completedCount > 0) parts.push(`${summary.completedCount} completed`);
  const body = parts.join(", ") || "No new activity";

  // Add return-from-away toast with special action to open bell popover
  const newToasts = [...toasts.value, {
    id,
    title: "While you were away",
    body,
    openBell: true,  // special flag to open bell popover instead of task panel
    type: "info"
  }];

  // If exceeding max, remove oldest
  if (newToasts.length > MAX_TOASTS) {
    toasts.value = newToasts.slice(-MAX_TOASTS);
  } else {
    toasts.value = newToasts;
  }

  // Auto-dismiss after 8 seconds
  setTimeout(() => dismissToast(id), 8000);
}

export function dismissToast(id) {
  toasts.value = toasts.value.filter(t => t.id !== id);
}
