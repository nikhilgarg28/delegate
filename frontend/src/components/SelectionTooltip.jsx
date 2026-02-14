import { useState, useEffect, useCallback, useRef } from "preact/hooks";

/**
 * SelectionTooltip - shows Copy and Reply buttons when text is selected in chat messages
 *
 * Props:
 * - containerRef: ref to the element containing selectable text (the chat log)
 * - chatInputRef: ref to the chat textarea where reply text should be inserted
 */
export function SelectionTooltip({ containerRef, chatInputRef }) {
  const [visible, setVisible] = useState(false);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const [selectedText, setSelectedText] = useState("");
  const [copied, setCopied] = useState(false);
  const tooltipRef = useRef();

  const hideTooltip = useCallback(() => {
    setVisible(false);
    setSelectedText("");
    setCopied(false);
  }, []);

  const handleSelection = useCallback(() => {
    const selection = window.getSelection();
    const text = selection.toString().trim();

    if (!text) {
      hideTooltip();
      return;
    }

    // Check if selection is within the container (chat log)
    if (!containerRef.current || !selection.rangeCount) {
      hideTooltip();
      return;
    }

    const range = selection.getRangeAt(0);
    const container = containerRef.current;

    // Check if the selection is within our container
    if (!container.contains(range.commonAncestorContainer)) {
      hideTooltip();
      return;
    }

    setSelectedText(text);

    // Position the tooltip near the selection
    const rect = range.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();

    // Calculate position - try to show above the selection
    let top = rect.top - 40; // 40px is approximate tooltip height
    let left = rect.left + (rect.width / 2);

    // If tooltip would go above viewport, show below selection instead
    if (top < 10) {
      top = rect.bottom + 10;
    }

    // Ensure tooltip stays within viewport horizontally
    // We'll adjust this after render if needed, but start centered
    const tooltipWidth = 120; // approximate width
    if (left + tooltipWidth / 2 > window.innerWidth - 10) {
      left = window.innerWidth - tooltipWidth / 2 - 10;
    }
    if (left - tooltipWidth / 2 < 10) {
      left = tooltipWidth / 2 + 10;
    }

    setPosition({ top, left });
    setVisible(true);
    setCopied(false);
  }, [containerRef, hideTooltip]);

  const handleCopy = useCallback((e) => {
    e.stopPropagation();
    e.preventDefault();
    if (!selectedText) return;

    navigator.clipboard.writeText(selectedText).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {});
  }, [selectedText]);

  const handleReply = useCallback((e) => {
    e.stopPropagation();
    e.preventDefault();
    if (!selectedText || !chatInputRef.current) return;

    // Format as blockquote: prefix each line with "> "
    const lines = selectedText.split("\n");
    const blockquote = lines.map(line => `> ${line}`).join("\n");

    const el = chatInputRef.current;
    const currentValue = (el.textContent || "").trim();

    const newValue = currentValue
      ? currentValue + "\n\n" + blockquote + "\n\n\n"
      : blockquote + "\n\n\n";

    el.textContent = newValue;

    // Auto-resize
    el.style.height = "auto";
    el.style.height = el.scrollHeight + "px";

    // Clear selection and hide tooltip
    window.getSelection().removeAllRanges();
    hideTooltip();

    // Trigger input event to update Preact state in ChatPanel
    el.dispatchEvent(new Event("input", { bubbles: true }));

    // Focus and place cursor at the end AFTER state update completes
    setTimeout(() => {
      el.focus();
      // Move cursor to end of contentEditable
      const range = document.createRange();
      range.selectNodeContents(el);
      range.collapse(false);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    }, 50);
  }, [selectedText, chatInputRef, hideTooltip]);

  useEffect(() => {
    if (!containerRef.current) return;

    const container = containerRef.current;

    // Listen for mouseup to detect selection
    const onMouseUp = () => {
      // Small delay to let selection complete
      setTimeout(handleSelection, 10);
    };

    // Hide tooltip when clicking outside or selection changes
    const onMouseDown = (e) => {
      if (tooltipRef.current && !tooltipRef.current.contains(e.target)) {
        hideTooltip();
      }
    };

    const onSelectionChange = () => {
      const selection = window.getSelection();
      if (!selection.toString().trim()) {
        hideTooltip();
      }
    };

    container.addEventListener("mouseup", onMouseUp);
    document.addEventListener("mousedown", onMouseDown);
    document.addEventListener("selectionchange", onSelectionChange);

    return () => {
      container.removeEventListener("mouseup", onMouseUp);
      document.removeEventListener("mousedown", onMouseDown);
      document.removeEventListener("selectionchange", onSelectionChange);
    };
  }, [containerRef, handleSelection, hideTooltip]);

  if (!visible) return null;

  return (
    <div
      ref={tooltipRef}
      class="selection-tooltip"
      style={{
        position: "fixed",
        top: `${position.top}px`,
        left: `${position.left}px`,
        transform: "translateX(-50%)",
      }}
    >
      <button
        class="selection-tooltip-btn"
        onClick={handleCopy}
        title={copied ? "Copied!" : "Copy"}
      >
        {copied ? (
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 8 7 12 13 4" />
          </svg>
        ) : (
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
            <rect x="5" y="5" width="9" height="9" rx="1.5" />
            <path d="M5 11H3.5A1.5 1.5 0 0 1 2 9.5v-7A1.5 1.5 0 0 1 3.5 1h7A1.5 1.5 0 0 1 12 2.5V5" />
          </svg>
        )}
        <span class="selection-tooltip-label">{copied ? "Copied" : "Copy"}</span>
      </button>
      <button
        class="selection-tooltip-btn"
        onClick={handleReply}
        title="Reply with quote"
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
          <path d="M3 10 L3 4 L9 4" />
          <path d="M3 4 L8 9" />
        </svg>
        <span class="selection-tooltip-label">Reply</span>
      </button>
    </div>
  );
}
