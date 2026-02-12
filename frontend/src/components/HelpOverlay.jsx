import { helpOverlayOpen } from "../state.js";

export function HelpOverlay() {
  const isOpen = helpOverlayOpen.value;

  if (!isOpen) return null;

  const shortcuts = [
    { key: "/", description: "Focus chat input" },
    { key: "Esc", description: "Close panels / defocus chat input" },
    { key: "s", description: "Toggle sidebar" },
    { key: "?", description: "Show/hide keyboard shortcuts" },
  ];

  const handleBackdropClick = (e) => {
    if (e.target === e.currentTarget) {
      helpOverlayOpen.value = false;
    }
  };

  return (
    <>
      <div class="help-backdrop open" onClick={handleBackdropClick} />
      <div class="help-overlay open">
        <div class="help-overlay-header">
          <h2 class="help-overlay-title">Keyboard Shortcuts</h2>
        </div>
        <div class="help-overlay-body">
          {shortcuts.map(({ key, description }) => (
            <div class="help-shortcut-row" key={key}>
              <kbd class="help-shortcut-key">{key}</kbd>
              <span class="help-shortcut-desc">{description}</span>
            </div>
          ))}
        </div>
      </div>
    </>
  );
}
