import { useState, useEffect, useRef } from "preact/hooks";
import {
  actionItems, bellPopoverOpen, awaySummary,
  openPanel, navigateTab, isInputFocused
} from "../state.js";
import { taskIdStr, fmtRelativeTime, fmtStatus } from "../utils.js";

export function NotificationPopover() {
  const isOpen = bellPopoverOpen.value;
  const [highlightIndex, setHighlightIndex] = useState(-1);
  const popoverRef = useRef(null);

  if (!isOpen) return null;

  const items = actionItems.value;
  const away = awaySummary.value;
  const hasActionItems = items.length > 0;
  const hasCompleted = away && away.completed && away.completed.length > 0;
  const hasUnread = away && away.unreadCount > 0;
  const isEmpty = !hasActionItems && !hasCompleted && !hasUnread;

  // Build clickable items list for keyboard navigation
  const clickableItems = [];
  items.forEach((task, idx) => {
    clickableItems.push({ type: "action", task, index: idx });
  });
  if (hasCompleted) {
    away.completed.forEach((task, idx) => {
      clickableItems.push({ type: "completed", task, index: idx });
    });
  }
  if (hasUnread) {
    clickableItems.push({ type: "unread" });
  }

  const handleClose = () => {
    bellPopoverOpen.value = false;
    setHighlightIndex(-1);
  };

  const handleBackdropClick = (e) => {
    if (e.target === e.currentTarget) {
      handleClose();
    }
  };

  const handleTaskClick = (taskId) => {
    handleClose();
    openPanel("task", taskId);
  };

  const handleUnreadClick = () => {
    handleClose();
    navigateTab("chat");
  };

  const handleItemAction = (item) => {
    if (item.type === "unread") {
      handleUnreadClick();
    } else {
      handleTaskClick(item.task.id);
    }
  };

  // Keyboard navigation
  useEffect(() => {
    if (!isOpen) return;

    const handler = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        handleClose();
        return;
      }
      if (isInputFocused()) return;

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setHighlightIndex(prev => {
          const next = prev + 1;
          return next >= clickableItems.length ? 0 : next;
        });
        return;
      }

      if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setHighlightIndex(prev => {
          const next = prev - 1;
          return next < 0 ? clickableItems.length - 1 : next;
        });
        return;
      }

      if (e.key === "Enter") {
        e.preventDefault();
        e.stopPropagation();
        if (highlightIndex >= 0 && highlightIndex < clickableItems.length) {
          handleItemAction(clickableItems[highlightIndex]);
        }
        return;
      }
    };

    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [isOpen, highlightIndex, clickableItems.length]);

  return (
    <>
      <div class="notif-backdrop" onClick={handleBackdropClick} />
      <div class="notif-popover" ref={popoverRef}>
        <div class="notif-popover-header">
          <h3>Notifications</h3>
          <button class="notif-close-btn" onClick={handleClose} aria-label="Close">
            ×
          </button>
        </div>
        <div class="notif-popover-body">
          {isEmpty && (
            <div class="notif-empty-state">
              All clear -- nothing needs your attention.
            </div>
          )}

          {hasActionItems && (
            <div class="notif-section">
              <div class="notif-section-header">ACTION ITEMS</div>
              {items.map((task, idx) => {
                const itemIdx = clickableItems.findIndex(
                  item => item.type === "action" && item.index === idx
                );
                const isHighlighted = itemIdx === highlightIndex;
                return (
                  <div
                    key={task.id}
                    class={`notif-item ${isHighlighted ? "highlighted" : ""}`}
                    onClick={() => handleTaskClick(task.id)}
                  >
                    <div class="notif-item-main">
                      <span class={`status-dot status-${task.status}`} />
                      <span class="notif-task-id">{taskIdStr(task.id)}</span>
                      <span class="notif-task-title">{task.title}</span>
                    </div>
                    <div class="notif-item-meta">
                      <span class={`status-badge status-${task.status}`}>
                        {fmtStatus(task.status)}
                      </span>
                      <span class="notif-time">
                        {fmtRelativeTime(task.updated_at)}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {hasCompleted && (
            <div class="notif-section">
              <div class="notif-section-header">
                COMPLETED WHILE AWAY ({away.completed.length})
              </div>
              {away.completed.map((task, idx) => {
                const itemIdx = clickableItems.findIndex(
                  item => item.type === "completed" && item.index === idx
                );
                const isHighlighted = itemIdx === highlightIndex;
                return (
                  <div
                    key={task.id}
                    class={`notif-item ${isHighlighted ? "highlighted" : ""}`}
                    onClick={() => handleTaskClick(task.id)}
                  >
                    <div class="notif-item-main">
                      <span class="notif-task-id">{taskIdStr(task.id)}</span>
                      <span class="notif-task-title">{task.title}</span>
                      <span class="notif-time">
                        {fmtRelativeTime(task.updated_at)}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {hasUnread && (
            <div class="notif-section">
              <div
                class={`notif-unread-item ${
                  highlightIndex === clickableItems.findIndex(item => item.type === "unread")
                    ? "highlighted"
                    : ""
                }`}
                onClick={handleUnreadClick}
              >
                {away.unreadCount} unread message{away.unreadCount !== 1 ? "s" : ""}
                <span class="notif-view-link">View →</span>
              </div>
            </div>
          )}
        </div>
      </div>
    </>
  );
}
