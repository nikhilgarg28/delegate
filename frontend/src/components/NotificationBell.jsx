import { actionItemCount, bellPopoverOpen } from "../state.js";

export function NotificationBell() {
  const count = actionItemCount.value;

  const handleClick = () => {
    bellPopoverOpen.value = !bellPopoverOpen.value;
  };

  return (
    <button class="notif-bell" onClick={handleClick} aria-label="Notifications">
      <svg
        width="18"
        height="18"
        viewBox="0 0 16 16"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
      >
        <path
          d="M8 2C6.34315 2 5 3.34315 5 5V8.26756C5 8.63047 4.88976 8.98556 4.68198 9.28859L3.5547 10.8944C3.20038 11.4026 3.56299 12.1111 4.18934 12.1111H11.8107C12.437 12.1111 12.7996 11.4026 12.4453 10.8944L11.318 9.28859C11.1102 8.98556 11 8.63047 11 8.26756V5C11 3.34315 9.65685 2 8 2Z"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
        <path
          d="M9.5 12.1111V12.6667C9.5 13.4951 8.82843 14.1667 8 14.1667C7.17157 14.1667 6.5 13.4951 6.5 12.6667V12.1111"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
      {count > 0 && (
        <span class="notif-badge">{count}</span>
      )}
    </button>
  );
}
