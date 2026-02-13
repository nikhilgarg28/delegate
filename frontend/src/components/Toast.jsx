import { toasts, dismissToast } from "../toast.js";
import { openPanel } from "../state.js";

function Toast({ toast }) {
  const { id, message, type, title, body, taskId, openBell } = toast;

  const handleDismiss = () => {
    dismissToast(id);
  };

  const handleView = () => {
    if (openBell) {
      // Open bell popover (will be imported from state.js when available)
      // For now, this is a placeholder for the bellPopoverOpen signal
      import("../state.js").then(state => {
        if (state.bellPopoverOpen) {
          state.bellPopoverOpen.value = true;
        }
      });
    } else if (taskId) {
      openPanel("task", taskId);
    }
    dismissToast(id);
  };

  // Action toast layout (with title + body)
  if (title || body) {
    return (
      <div class={`toast toast-${type}`}>
        <div class="toast-content">
          {title && <div class="toast-title">{title}</div>}
          {body && <div class="toast-body">{body}</div>}
        </div>
        <div class="toast-actions">
          {(taskId || openBell) && (
            <button class="toast-action" onClick={handleView}>
              View
            </button>
          )}
          <button class="toast-close" onClick={handleDismiss} aria-label="Close">
            ×
          </button>
        </div>
      </div>
    );
  }

  // Simple toast layout (original)
  return (
    <div class={`toast toast-${type}`}>
      <div class="toast-message">{message}</div>
      <button class="toast-close" onClick={handleDismiss} aria-label="Close">
        ×
      </button>
    </div>
  );
}

export function ToastContainer() {
  const toastList = toasts.value;

  if (toastList.length === 0) return null;

  return (
    <div class="toast-container">
      {toastList.map(toast => (
        <Toast key={toast.id} toast={toast} />
      ))}
    </div>
  );
}
