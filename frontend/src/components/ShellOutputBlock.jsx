import { useState, useRef, useEffect } from "preact/hooks";
import { handleCopyClick } from "../utils.js";

/**
 * Renders shell command output in the chat.
 * @param {Object} props
 * @param {Object|null} props.result - { stdout, stderr, exit_code, cwd, duration_ms } or null if running
 * @param {Function} [props.onErrorState] - Optional callback called with error state (true/false)
 */
export function ShellOutputBlock({ result, onErrorState }) {
  const [expanded, setExpanded] = useState(false);
  const [shouldCollapse, setShouldCollapse] = useState(false);
  const contentRef = useRef();

  const running = !result;

  useEffect(() => {
    if (!contentRef.current || running) return;
    const lineCount = (result.stdout || '').split('\n').length;
    setShouldCollapse(lineCount > 20);
  }, [result, running]);

  if (running) {
    return (
      <div class="shell-output-block running">
        <div class="shell-output-header">
          <span class="shell-output-cwd">Running...</span>
        </div>
        <div class="shell-output-body">
          <div class="shell-output-loading">
            <span class="dot"></span>
            <span class="dot"></span>
            <span class="dot"></span>
          </div>
        </div>
      </div>
    );
  }

  const { stdout = '', stderr = '', exit_code = 0, cwd = '', duration_ms = 0, error = '' } = result;
  const hasError = exit_code !== 0 || !!error;
  const durationSec = (duration_ms / 1000).toFixed(2);

  // Notify parent of error state
  useEffect(() => {
    if (onErrorState) {
      onErrorState(hasError);
    }
  }, [hasError, onErrorState]);

  const lines = stdout.split('\n');
  const displayContent = (shouldCollapse && !expanded) ? lines.slice(0, 20).join('\n') : stdout;
  const lineCount = lines.length;

  const handleCopy = (e) => {
    e.stopPropagation();
    const text = stderr ? `${stdout}\n\nstderr:\n${stderr}` : stdout;
    navigator.clipboard.writeText(text);
    const btn = e.currentTarget;
    const originalHTML = btn.innerHTML;
    btn.innerHTML = '✓';
    setTimeout(() => { btn.innerHTML = originalHTML; }, 1500);
  };

  const ClipboardIcon = () => (
    <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <rect x="5" y="5" width="9" height="9" rx="1.5" />
      <path d="M5 11H3.5A1.5 1.5 0 0 1 2 9.5V3.5A1.5 1.5 0 0 1 3.5 2h6A1.5 1.5 0 0 1 11 3.5V5" />
    </svg>
  );

  return (
    <div class="shell-output-block">
      <div class="shell-output-body" ref={contentRef}>
        <button class="shell-output-copy-icon" onClick={handleCopy} title="Copy output">
          <ClipboardIcon />
        </button>
        <pre class="shell-output-stdout">{displayContent}</pre>
        {shouldCollapse && !expanded && (
          <button class="shell-expand-btn" onClick={() => setExpanded(true)}>
            Show all ({lineCount} lines)
          </button>
        )}
        {error && (
          <div class="shell-output-stderr">
            <div class="shell-output-stderr-label">Error:</div>
            <pre>{error}</pre>
          </div>
        )}
        {stderr && (
          <div class="shell-output-stderr">
            <div class="shell-output-stderr-label">stderr:</div>
            <pre>{stderr}</pre>
          </div>
        )}
      </div>
    </div>
  );
}
