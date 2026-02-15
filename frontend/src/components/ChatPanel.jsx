import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, messages, agents, activeTab,
  chatFilterDirection, openPanel,
  knownAgentNames, isMuted, humanName, expandedMessages,
  commandMode, commandCwd, teams, navigate,
  loadTeamCwd, saveTeamCwd, loadTeamHistory, addToHistory,
  uploadingFiles,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtTimestamp, renderMarkdown,
  linkifyTaskRefs, linkifyFilePaths, agentifyRefs, msgStatusIcon, taskIdStr,
  handleCopyClick, toApiPath,
} from "../utils.js";
import { playMsgSound } from "../audio.js";
import { showToast } from "../toast.js";
import { CopyBtn } from "./CopyBtn.jsx";
import { CustomSelect } from "./CustomSelect.jsx";
import { PillSelect } from "./PillSelect.jsx";
import { ManagerActivityBar } from "./ManagerActivityBar.jsx";
import { SelectionTooltip } from "./SelectionTooltip.jsx";
import { CommandAutocomplete } from "./CommandAutocomplete.jsx";
import { ShellOutputBlock } from "./ShellOutputBlock.jsx";
import { StatusBlock } from "./StatusBlock.jsx";
import { DiffCommandBlock } from "./DiffCommandBlock.jsx";
import { CostBlock } from "./CostBlock.jsx";
import { parseCommand, filterCommands, COMMANDS } from "../commands.js";

// ── Command message wrapper to track error state ──
function CommandMessage({ message, parsed }) {
  const [hasError, setHasError] = useState(false);

  // Detect diff command errors
  useEffect(() => {
    if (parsed?.name === 'diff' && message.result?.error) {
      setHasError(true);
    }
  }, [parsed, message.result]);

  // For shell commands, include duration in header
  const isShell = parsed?.name === 'shell';
  const duration = isShell && message.result?.duration_ms
    ? `${(message.result.duration_ms / 1000).toFixed(2)}s`
    : null;

  return (
    <div class={`msg-command ${hasError ? 'msg-command-error' : ''}`}>
      <div class="msg-command-header">
        <span class="msg-command-sender">{cap(message.sender)}:</span>
        <span class="msg-command-text">{message.content}</span>
        <span class="msg-time">{fmtTimestamp(message.timestamp)}</span>
        <span class="msg-checkmark-spacer" />
      </div>
      {parsed?.name === 'shell' && (
        <ShellOutputBlock result={message.result} onErrorState={setHasError} />
      )}
      {parsed?.name === 'status' && <StatusBlock result={message.result} />}
      {parsed?.name === 'diff' && !message.result?.error && (
        <DiffCommandBlock result={message.result} />
      )}
      {parsed?.name === 'diff' && message.result?.error && (
        <div class="shell-output-stderr">
          <div class="shell-output-stderr-label">Error:</div>
          <pre>{message.result.error}</pre>
        </div>
      )}
      {parsed?.name === 'cost' && <CostBlock result={message.result} />}
    </div>
  );
}

// ── Linked content with event delegation ──
function LinkedDiv({ html, class: cls, style, ref: externalRef }) {
  const handler = useCallback((e) => {
    // Copy button click
    const copyBtn = e.target.closest(".copy-btn");
    if (copyBtn) {
      e.stopPropagation(); e.preventDefault(); handleCopyClick(copyBtn); return;
    }
    const taskLink = e.target.closest("[data-task-id]");
    if (taskLink) {
      e.stopPropagation();
      openPanel("task", parseInt(taskLink.dataset.taskId, 10));
      return;
    }
    const agentLink = e.target.closest("[data-agent-name]");
    if (agentLink) { e.stopPropagation(); openPanel("agent", agentLink.dataset.agentName); return; }
    const fileLink = e.target.closest("[data-file-path]");
    if (fileLink) {
      e.stopPropagation();
      const fpath = fileLink.dataset.filePath;
      openPanel("file", fpath);
      return;
    }
  }, []);

  return <div ref={externalRef} class={cls} style={style} onClick={handler} dangerouslySetInnerHTML={{ __html: html }} />;
}

// ── Collapsible long message ──
const COLLAPSE_THRESHOLD = 90; // ~4 lines at 14px * 1.6 line-height = 89.6px

function CollapsibleMessage({ html, messageId, isBoss }) {
  const wrapperRef = useRef();
  const [isLong, setIsLong] = useState(false);
  const isExpanded = expandedMessages.value.has(messageId);

  useEffect(() => {
    // Query the content div directly instead of passing ref through LinkedDiv
    const el = wrapperRef.current?.querySelector('.msg-content');
    if (!el) return;

    const checkOverflow = () => {
      if (el.scrollHeight > COLLAPSE_THRESHOLD) {
        setIsLong(true);
      } else {
        setIsLong(false);
      }
    };

    // Use requestAnimationFrame to ensure layout is complete
    requestAnimationFrame(checkOverflow);

    // Re-check on resize
    const observer = new ResizeObserver(checkOverflow);
    observer.observe(el);
    return () => observer.disconnect();
  }, [html]);

  const toggle = useCallback(() => {
    const next = new Set(expandedMessages.value);
    if (next.has(messageId)) {
      next.delete(messageId);
    } else {
      next.add(messageId);
    }
    expandedMessages.value = next;
  }, [messageId]);

  const wrapperClass = "msg-content-wrapper" + (isLong && !isExpanded ? " collapsed" : "");

  const contentClass = isBoss ? "msg-content md-content content-boss" : "msg-content md-content content-regular";

  return (
    <>
      <div class={wrapperClass} ref={wrapperRef}>
        <LinkedDiv class={contentClass} html={html} />
        {isLong && !isExpanded && (
          <div class="msg-fade-overlay" />
        )}
      </div>
      {isLong && (
        <button class="msg-expand-btn" onClick={toggle}>
          {isExpanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </>
  );
}

// ── Collapsible event message ──
function CollapsibleEventMessage({ html, messageId }) {
  const wrapperRef = useRef();
  const [isLong, setIsLong] = useState(false);
  const isExpanded = expandedMessages.value.has(messageId);

  useEffect(() => {
    // Query the content div directly instead of passing ref through LinkedDiv
    const el = wrapperRef.current?.querySelector('.msg-event-text');
    if (!el) return;

    const checkOverflow = () => {
      if (el.scrollHeight > COLLAPSE_THRESHOLD) {
        setIsLong(true);
      } else {
        setIsLong(false);
      }
    };

    // Use requestAnimationFrame to ensure layout is complete
    requestAnimationFrame(checkOverflow);

    // Re-check on resize
    const observer = new ResizeObserver(checkOverflow);
    observer.observe(el);
    return () => observer.disconnect();
  }, [html]);

  const toggle = useCallback(() => {
    const next = new Set(expandedMessages.value);
    if (next.has(messageId)) {
      next.delete(messageId);
    } else {
      next.add(messageId);
    }
    expandedMessages.value = next;
  }, [messageId]);

  const wrapperClass = "msg-event-content-wrapper" + (isLong && !isExpanded ? " collapsed" : "");

  return (
    <>
      <div class={wrapperClass} ref={wrapperRef}>
        <LinkedDiv class="msg-event-text" html={html} />
        {isLong && !isExpanded && (
          <div class="msg-event-fade-overlay" />
        )}
      </div>
      {isLong && (
        <button class="msg-expand-btn" onClick={toggle}>
          {isExpanded ? 'Show less' : 'Show more'}
        </button>
      )}
    </>
  );
}

// ── Voice-to-text hook ──
function useSpeechRecognition(inputRef) {
  const [active, setActive] = useState(false);
  const recRef = useRef(null);
  const baseTextRef = useRef("");
  const finalTextRef = useRef("");
  const stoppingRef = useRef(false);
  const supported = useRef(false);

  useEffect(() => {
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) return;
    supported.current = true;
    const rec = new SR();
    rec.continuous = true;
    rec.interimResults = true;
    rec.lang = navigator.language || "en-US";
    rec.onresult = (e) => {
      let interim = "";
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalTextRef.current += e.results[i][0].transcript;
        else interim += e.results[i][0].transcript;
      }
      if (inputRef.current) {
        inputRef.current.textContent = baseTextRef.current + finalTextRef.current + interim;
        inputRef.current.style.height = "auto";
        inputRef.current.style.height = inputRef.current.scrollHeight + "px";
      }
    };
    rec.onend = () => { setActive(false); stoppingRef.current = false; };
    rec.onerror = (e) => {
      if (e.error !== "aborted" && e.error !== "no-speech") showToast("Voice input error: " + e.error, "error");
      setActive(false); stoppingRef.current = false;
    };
    recRef.current = rec;
  }, []);

  const toggle = useCallback(() => {
    if (!recRef.current || stoppingRef.current) return;
    if (active) {
      stoppingRef.current = true;
      recRef.current.stop();
    } else {
      const el = inputRef.current;
      baseTextRef.current = el && el.textContent ? el.textContent + " " : "";
      finalTextRef.current = "";
      try { recRef.current.start(); } catch (e) { return; }
      setActive(true);
    }
  }, [active]);

  return { active, toggle, supported: supported.current };
}

// ── Bell/mute icon ──
function BellIcon({ muted }) {
  if (muted) {
    return (
      <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M4.5 6.5V6a3.5 3.5 0 017 0v.5c0 2 1 3 1 3H3.5s1-1 1-3z" />
        <path d="M6.5 13a1.5 1.5 0 003 0" />
        <line x1="2" y1="2" x2="14" y2="14" />
      </svg>
    );
  }
  return (
    <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4.5 6.5V6a3.5 3.5 0 017 0v.5c0 2 1 3 1 3H3.5s1-1 1-3z" />
      <path d="M6.5 13a1.5 1.5 0 003 0" />
    </svg>
  );
}

export function ChatPanel() {
  const team = currentTeam.value;
  const allAgents = agents.value;
  const agNames = knownAgentNames.value;
  const teamList = teams.value || [];

  const [msgs, setMsgs] = useState([]);
  const [filterFrom, setFilterFrom] = useState("");
  const [filterTo, setFilterTo] = useState("");
  const [filterSearch, setFilterSearch] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [typeFilter, setTypeFilter] = useState("all");
  const [recipient, setRecipient] = useState("");
  const [inputVal, setInputVal] = useState("");
  const [sendBtnActive, setSendBtnActive] = useState(false);
  const [acIndex, setAcIndex] = useState(0);       // autocomplete selected index
  const acRef = useRef({ visible: false, commands: [], index: 0 }); // refs for keydown handler
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  const [hasMoreMessages, setHasMoreMessages] = useState(true);
  const [showDropZone, setShowDropZone] = useState(false);

  // Command history state
  const [historyIndex, setHistoryIndex] = useState(-1); // -1 = not navigating history
  const historyRef = useRef([]); // Current team's command history
  const draftInputRef = useRef(''); // Store current draft when navigating history

  const direction = chatFilterDirection.value;
  const logRef = useRef();
  const inputRef = useRef();
  const fileInputRef = useRef();
  const searchTimerRef = useRef(null);
  const lastMsgTsRef = useRef("");
  const cooldownRef = useRef(false);
  const isAtBottomRef = useRef(true);
  const [showJumpBtn, setShowJumpBtn] = useState(false);
  const initialScrollDone = useRef(false);
  const oldestMsgIdRef = useRef(null);
  const newestMsgTsRef = useRef("");
  const pollingIntervalRef = useRef(null);
  const draftsByTeam = useRef({}); // Store drafts per team
  const lastTeamRef = useRef(team); // Track last team to detect switches
  const mic = useSpeechRecognition(inputRef);
  const muted = isMuted.value;

  // ── File upload handlers ──
  const handleFileSelect = useCallback(async (event) => {
    const files = Array.from(event.target.files || []);
    if (files.length === 0) return;

    // Reset file input
    if (fileInputRef.current) fileInputRef.current.value = '';

    // Client-side validation
    const ALLOWED_EXTENSIONS = new Set([
      'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg',
      'pdf', 'md', 'txt', 'csv', 'json', 'yaml', 'yml',
      'zip', 'html', 'css', 'js', 'py'
    ]);
    const MAX_FILE_SIZE = 50 * 1024 * 1024; // 50MB

    const validFiles = [];
    for (const file of files) {
      const ext = file.name.split('.').pop().toLowerCase();
      if (!ALLOWED_EXTENSIONS.has(ext)) {
        showToast(`File ${file.name} has unsupported type`, 'error');
        continue;
      }
      if (file.size > MAX_FILE_SIZE) {
        showToast(`File ${file.name} exceeds 50MB limit`, 'error');
        continue;
      }
      validFiles.push(file);
    }

    if (validFiles.length === 0) return;

    // Add to uploadingFiles
    const uploadEntries = validFiles.map(f => ({
      name: f.name,
      progress: 0,
      error: null,
    }));
    uploadingFiles.value = [...uploadingFiles.value, ...uploadEntries];

    try {
      // Upload files
      const result = await api.uploadFiles(team, validFiles, (progress) => {
        // Update progress for all files (XHR gives total progress)
        uploadingFiles.value = uploadingFiles.value.map(entry => {
          if (validFiles.some(f => f.name === entry.name)) {
            return { ...entry, progress };
          }
          return entry;
        });
      });

      // Insert file references into chat input
      const tokens = result.uploaded.map(f => `[file:${f.url}]`).join(' ');
      if (inputRef.current) {
        const currentText = inputRef.current.textContent || '';
        const newText = currentText ? `${currentText} ${tokens}` : tokens;
        inputRef.current.textContent = newText;

        // Trigger input event to update state
        const event = new Event('input', { bubbles: true });
        inputRef.current.dispatchEvent(event);

        // Focus and move cursor to end
        inputRef.current.focus();
        const range = document.createRange();
        const sel = window.getSelection();
        range.selectNodeContents(inputRef.current);
        range.collapse(false);
        sel.removeAllRanges();
        sel.addRange(range);
      }

      // Remove from uploadingFiles after 3 seconds
      setTimeout(() => {
        uploadingFiles.value = uploadingFiles.value.filter(
          entry => !validFiles.some(f => f.name === entry.name)
        );
      }, 3000);
    } catch (error) {
      // Set error on failed files
      uploadingFiles.value = uploadingFiles.value.map(entry => {
        if (validFiles.some(f => f.name === entry.name)) {
          return { ...entry, error: error.message || 'Upload failed' };
        }
        return entry;
      });
    }
  }, [team]);

  const triggerFileInput = useCallback(() => {
    if (fileInputRef.current) {
      fileInputRef.current.click();
    }
  }, []);

  const cancelUpload = useCallback((file) => {
    uploadingFiles.value = uploadingFiles.value.filter(f => f.name !== file.name);
  }, []);

  // Drag-and-drop handlers
  const handleDragEnter = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.dataTransfer.items && e.dataTransfer.items.length > 0) {
      setShowDropZone(true);
    }
  }, []);

  const handleDragOver = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
  }, []);

  const handleDragLeave = useCallback((e) => {
    e.preventDefault();
    e.stopPropagation();
    // Only hide if leaving the container itself, not a child element
    if (e.target === e.currentTarget) {
      setShowDropZone(false);
    }
  }, []);

  const handleDrop = useCallback(async (e) => {
    e.preventDefault();
    e.stopPropagation();
    setShowDropZone(false);

    const files = Array.from(e.dataTransfer.files || []);
    if (files.length > 0) {
      // Simulate file input event
      const fakeEvent = { target: { files } };
      handleFileSelect(fakeEvent);
    }
  }, [handleFileSelect]);

  // ── Autocomplete state ──
  // Show autocomplete only while typing the command name (before first space).
  // Once a space follows a recognized command, we're in "argument entry" mode.
  // Reset autocomplete index when input changes
  const acQueryKey = inputVal.startsWith("/") ? inputVal.split(" ")[0] : "";
  useEffect(() => { setAcIndex(0); }, [acQueryKey]);

  const acParsed = parseCommand(inputVal);
  const acIsTypingName = inputVal.startsWith("/") && !inputVal.includes(" ");
  const acCommands = acIsTypingName ? filterCommands(acParsed?.name || "") : [];
  const acVisible = acCommands.length > 0;
  // Derive which recognized command we're working with (for argument hints)
  const recognizedCmd = (!acIsTypingName && acParsed && COMMANDS[acParsed.name]) ? COMMANDS[acParsed.name] : null;

  // Keep ref in sync so the keydown handler always reads current values
  acRef.current = { visible: acVisible, commands: acCommands, index: acIndex };

  // Check if text contains markdown syntax we support
  const hasMarkdown = useCallback((text) => {
    if (!text) return false;
    // Check for code blocks, inline code, bullet lists, or numbered lists
    return /```|`[^`\n]+`|^\s*[-*+]\s+|^\s*\d+\.\s+/m.test(text);
  }, []);

  // Render inline markdown subset (code blocks, inline code, lists)
  const renderInlineMarkdown = useCallback((text) => {
    if (!text) return '';

    // Helper to process inline code within a text segment
    const processInlineCode = (segment) => {
      return segment.replace(/`([^`\n]+)`/g, (_, code) => {
        return `<code>${esc(code)}</code>`;
      });
    };

    // Helper to escape and process inline code for text that's not a code block
    const escapeAndProcessInline = (segment) => {
      // Split by inline code backticks to preserve them
      const parts = [];
      let lastIndex = 0;
      const regex = /`([^`\n]+)`/g;
      let match;

      while ((match = regex.exec(segment)) !== null) {
        // Add escaped plain text before the code
        if (match.index > lastIndex) {
          parts.push(esc(segment.substring(lastIndex, match.index)));
        }
        // Add the code element
        parts.push(`<code>${esc(match[1])}</code>`);
        lastIndex = regex.lastIndex;
      }

      // Add any remaining text
      if (lastIndex < segment.length) {
        parts.push(esc(segment.substring(lastIndex)));
      }

      return parts.join('');
    };

    // Step 1: Extract and process code blocks first
    const codeBlockParts = [];
    const withoutCodeBlocks = text.replace(/```([a-zA-Z]*)\n([\s\S]*?)```/g, (match, lang, code) => {
      const placeholder = `\x00CODEBLOCK_${codeBlockParts.length}\x00`;
      codeBlockParts.push(`<pre><code class="language-${esc(lang || 'text')}">${esc(code.trim())}</code></pre>`);
      return placeholder;
    });

    // Step 2: Process lists line by line
    const lines = withoutCodeBlocks.split('\n');
    const formatted = [];
    let inList = false;
    let listType = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Check for code block placeholder - pass through as-is
      if (line.includes('\x00CODEBLOCK_')) {
        if (inList) {
          formatted.push(listType === 'ul' ? '</ul>' : '</ol>');
          inList = false;
          listType = null;
        }
        formatted.push(line);
        continue;
      }

      // Bullet list (-, *, +)
      const bulletMatch = line.match(/^(\s*)([-*+])\s+(.*)$/);
      if (bulletMatch) {
        if (!inList || listType !== 'ul') {
          if (inList) formatted.push('</ol>'); // Close previous ol if switching
          formatted.push('<ul>');
          inList = true;
          listType = 'ul';
        }
        formatted.push(`<li>${escapeAndProcessInline(bulletMatch[3])}</li>`);
        continue;
      }

      // Numbered list (1., 2., etc.)
      const numberedMatch = line.match(/^(\s*)(\d+)\.\s+(.*)$/);
      if (numberedMatch) {
        if (!inList || listType !== 'ol') {
          if (inList) formatted.push('</ul>'); // Close previous ul if switching
          formatted.push('<ol>');
          inList = true;
          listType = 'ol';
        }
        formatted.push(`<li>${escapeAndProcessInline(numberedMatch[3])}</li>`);
        continue;
      }

      // Not a list item
      if (inList) {
        formatted.push(listType === 'ul' ? '</ul>' : '</ol>');
        inList = false;
        listType = null;
      }

      formatted.push(escapeAndProcessInline(line));
    }

    // Close any open list
    if (inList) {
      formatted.push(listType === 'ul' ? '</ul>' : '</ol>');
    }

    // Step 3: Restore code blocks
    let html = formatted.join('\n');
    codeBlockParts.forEach((block, i) => {
      html = html.replace(`\x00CODEBLOCK_${i}\x00`, block);
    });

    return html;
  }, []);

  // Save and restore drafts when team changes
  useEffect(() => {
    const prevTeam = lastTeamRef.current;
    if (prevTeam && prevTeam !== team) {
      // Save draft from previous team
      if (inputRef.current) {
        draftsByTeam.current[prevTeam] = inputRef.current.textContent || "";
      }
      // Restore draft for new team
      const draft = draftsByTeam.current[team] || "";
      if (inputRef.current) {
        inputRef.current.textContent = draft;
        inputRef.current.style.height = "auto";
        inputRef.current.style.height = inputRef.current.scrollHeight + "px";
      }
      setInputVal(draft);
      setSendBtnActive(!!draft.trim());
      // Reset command mode on team switch — re-derive from restored draft
      commandMode.value = draft.startsWith('/');
      if (!draft.startsWith('/')) commandCwd.value = '';
    }

    // Load CWD and command history for the current team
    if (team) {
      commandCwd.value = loadTeamCwd(team);
      historyRef.current = loadTeamHistory(team);
      setHistoryIndex(-1); // Reset history navigation
      draftInputRef.current = ''; // Clear draft
    }

    lastTeamRef.current = team;
  }, [team]);

  // Restore filters from session storage
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("chatFilters");
      if (!raw) return;
      const f = JSON.parse(raw);
      if (f.search) {
        setFilterSearch(f.search);
        setSearchExpanded(true); // Expand if there was saved search text
      }
      if (f.from) setFilterFrom(f.from);
      if (f.to) setFilterTo(f.to);
      // Backward compat: convert old showEvents boolean to typeFilter
      if (f.typeFilter) setTypeFilter(f.typeFilter);
      else if (f.showEvents === false) setTypeFilter("chat");
      if (f.direction === "bidi") chatFilterDirection.value = "bidi";
    } catch (e) { }
  }, []);

  // Save filters
  useEffect(() => {
    try {
      sessionStorage.setItem("chatFilters", JSON.stringify({
        search: filterSearch, from: filterFrom, to: filterTo,
        typeFilter, direction,
      }));
    } catch (e) { }
  }, [filterSearch, filterFrom, filterTo, typeFilter, direction]);

  // Initial load: fetch last 100 messages
  useEffect(() => {
    if (!team) return;
    let active = true;
    (async () => {
      try {
        const initial = await api.fetchMessages(team, { limit: 100 });
        if (!active) return;
        setMsgs(initial);
        if (initial.length > 0) {
          oldestMsgIdRef.current = initial[0].id;
          newestMsgTsRef.current = initial[initial.length - 1].timestamp;
        }
        if (initial.length < 100) {
          setHasMoreMessages(false);
        }
      } catch (e) {
        console.error("Failed to fetch initial messages:", e);
      }
    })();
    return () => { active = false; };
  }, [team]);

  // Polling: fetch new messages every 2 seconds using `since`
  useEffect(() => {
    if (!team) return;
    const poll = async () => {
      try {
        const newMsgs = await api.fetchMessages(team, { since: newestMsgTsRef.current });
        if (newMsgs.length > 0) {
          setMsgs(prev => {
            const combined = [...prev, ...newMsgs];
            // Deduplicate by id
            const seen = new Set();
            const unique = combined.filter(m => {
              if (seen.has(m.id)) return false;
              seen.add(m.id);
              return true;
            });
            return unique;
          });
          newestMsgTsRef.current = newMsgs[newMsgs.length - 1].timestamp;
        }
      } catch (e) {
        console.error("Polling error:", e);
      }
    };
    pollingIntervalRef.current = setInterval(poll, 2000);
    return () => {
      if (pollingIntervalRef.current) {
        clearInterval(pollingIntervalRef.current);
      }
    };
  }, [team]);

  // Auto-select recipient when agents load
  useEffect(() => {
    if (!recipient && allAgents.length) {
      const mgrs = allAgents.filter(a => a.role === "manager").sort((a, b) => a.name.localeCompare(b.name));
      if (mgrs.length) setRecipient(mgrs[0].name);
      else if (allAgents.length) setRecipient(allAgents[0].name);
    }
  }, [allAgents]);

  // Sound on new messages
  useEffect(() => {
    const chatMsgs = msgs.filter(m => m.type === "chat");
    if (chatMsgs.length > 0) {
      const newest = chatMsgs[chatMsgs.length - 1].timestamp || "";
      if (lastMsgTsRef.current && newest > lastMsgTsRef.current && !cooldownRef.current) {
        playMsgSound();
      }
      lastMsgTsRef.current = newest;
    }
  }, [msgs]);

  // Filter + sort messages
  const filteredMsgs = useMemo(() => {
    let filtered = msgs;
    // Type filter: "all", "chat", or "events"
    if (typeFilter === "chat") filtered = filtered.filter(m => m.type !== "event");
    if (typeFilter === "events") filtered = filtered.filter(m => m.type === "event");
    const between = direction === "bidi" && !!(filterFrom && filterTo);
    if (filterFrom || filterTo) {
      filtered = filtered.filter(m => {
        if (m.type === "event") return true;
        if (between) return (m.sender === filterFrom && m.recipient === filterTo) || (m.sender === filterTo && m.recipient === filterFrom);
        if (filterFrom && m.sender !== filterFrom) return false;
        if (filterTo && m.recipient !== filterTo) return false;
        return true;
      });
    }
    const sq = filterSearch.toLowerCase().trim();
    if (sq) filtered = filtered.filter(m => (m.content || "").toLowerCase().includes(sq));
    return filtered;
  }, [msgs, typeFilter, filterFrom, filterTo, filterSearch, direction]);

  // Track scroll position and load older messages
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const onScroll = async () => {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      isAtBottomRef.current = nearBottom;
      setShowJumpBtn(!nearBottom);

      // Load older messages when scrolled to top
      const nearTop = el.scrollTop < 100;
      if (nearTop && !isLoadingOlder && hasMoreMessages && oldestMsgIdRef.current) {
        setIsLoadingOlder(true);
        try {
          const older = await api.fetchMessages(team, { before_id: oldestMsgIdRef.current, limit: 50 });
          if (older.length > 0) {
            const prevScrollHeight = el.scrollHeight;
            setMsgs(prev => {
              const combined = [...older, ...prev];
              // Deduplicate by id
              const seen = new Set();
              const unique = combined.filter(m => {
                if (seen.has(m.id)) return false;
                seen.add(m.id);
                return true;
              });
              return unique;
            });
            oldestMsgIdRef.current = older[0].id;
            // Maintain scroll position after prepending
            requestAnimationFrame(() => {
              const newScrollHeight = el.scrollHeight;
              el.scrollTop = newScrollHeight - prevScrollHeight;
            });
          }
          if (older.length < 50) {
            setHasMoreMessages(false);
          }
        } catch (e) {
          console.error("Failed to load older messages:", e);
        } finally {
          setIsLoadingOlder(false);
        }
      }
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, [team, isLoadingOlder, hasMoreMessages]);

  // Scroll to bottom on initial load
  useEffect(() => {
    const el = logRef.current;
    if (!el || !filteredMsgs.length || initialScrollDone.current) return;
    initialScrollDone.current = true;
    requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
  }, [filteredMsgs]);

  // Auto-scroll when new messages arrive — only if already at bottom
  useEffect(() => {
    if (!initialScrollDone.current) return;
    const el = logRef.current;
    if (el && isAtBottomRef.current) {
      requestAnimationFrame(() => { el.scrollTop = el.scrollHeight; });
    }
  }, [filteredMsgs]);

  const jumpToBottom = useCallback(() => {
    const el = logRef.current;
    if (el) {
      el.scrollTop = el.scrollHeight;
      isAtBottomRef.current = true;
      setShowJumpBtn(false);
    }
  }, []);

  // Agent options for filters and recipient
  const agentOptions = useMemo(() => {
    const names = new Set(allAgents.map(a => a.name));
    msgs.forEach(m => { if (m.type === "chat") { names.add(m.sender); names.add(m.recipient); } });
    const roleMap = {};
    allAgents.forEach(a => { roleMap[a.name] = a.role || "worker"; });
    return [...names].sort().map(n => ({
      value: n,
      label: roleMap[n] ? `${cap(n)} (${roleMap[n]})` : cap(n),
      role: roleMap[n] || "worker",
    }));
  }, [allAgents, msgs]);


  // Execute a magic command
  const executeCommand = useCallback(async (cmd) => {
    const placeholderId = `cmd-${Date.now()}`;
    const now = new Date().toISOString();

    // Add placeholder message
    const placeholder = {
      id: placeholderId,
      type: 'command',
      content: cmd.raw,
      sender: humanName.value || 'human',
      recipient: humanName.value || 'human',
      timestamp: now,
      result: null, // null = running
    };
    setMsgs(prev => [...prev, placeholder]);
    newestMsgTsRef.current = now;

    // Scroll to bottom
    isAtBottomRef.current = true;
    setShowJumpBtn(false);
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        const el = logRef.current;
        if (el) el.scrollTop = el.scrollHeight;
      });
    });

    try {
      let result;
      if (cmd.name === 'shell') {
        if (!cmd.args) {
          result = { error: 'Usage: /shell [command]', exit_code: -1 };
        } else {
          // Strip -d <path> from args since it's already captured in commandCwd
          const shellArgs = cmd.args.replace(/-d\s+\S+/, '').trim();
          result = await api.execShell(team, shellArgs, commandCwd.value || undefined);
        }
      } else if (cmd.name === 'status') {
        // Status is client-side, build result from API calls
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

        result = { doneToday, doneThisWeek, pending, statuses };
      } else if (cmd.name === 'diff') {
        if (!cmd.args) {
          result = { error: 'Usage: /diff [task_id]', exit_code: -1 };
        } else {
          // Parse task ID - strip leading T/t if present
          const idStr = cmd.args.trim().replace(/^[Tt]/, '');
          const taskId = parseInt(idStr, 10);
          if (isNaN(taskId)) {
            result = { error: `Invalid task ID: ${cmd.args}`, exit_code: -1 };
          } else {
            const diffData = await api.fetchTaskDiffGlobal(taskId);
            if (!diffData) {
              result = { error: `Task ${taskId} not found or has no diff`, exit_code: -1 };
            } else {
              result = diffData;
            }
          }
        }
      } else if (cmd.name === 'cost') {
        result = await api.fetchCostSummary(team);
      } else {
        result = { error: `Unknown command: /${cmd.name}. Available: /shell, /status, /diff, /cost`, exit_code: -1 };
      }

      // Persist to DB
      const saved = await api.saveCommand(team, cmd.raw, result);

      // Add to command history (only shell commands, skip if it was an error)
      if (cmd.name === 'shell' && cmd.args && (!result.error || result.exit_code === 0)) {
        addToHistory(team, cmd.args);
        historyRef.current = loadTeamHistory(team); // Reload history
      }

      // Update placeholder with real result
      setMsgs(prev => prev.map(m =>
        m.id === placeholderId ? { ...m, id: saved.id, result } : m
      ));
    } catch (err) {
      // Update placeholder with error
      const errorResult = { error: err.message, exit_code: -1 };
      setMsgs(prev => prev.map(m =>
        m.id === placeholderId ? { ...m, result: errorResult } : m
      ));
    }
  }, [team]);

  const handleSend = useCallback(async () => {
    if (mic.active) mic.toggle();
    const val = inputRef.current ? (inputRef.current.textContent || "").trim() : "";
    if (!val || !team) return;

    // Check for command
    const cmd = parseCommand(val);
    if (cmd && COMMANDS[cmd.name]) {
      if (inputRef.current) {
        inputRef.current.textContent = "";
        inputRef.current.style.height = "auto";
      }
      setInputVal("");
      setSendBtnActive(false);
      commandMode.value = false;
      setHistoryIndex(-1); // Reset history navigation
      draftInputRef.current = '';
      await executeCommand(cmd);
      return;
    }

    // Regular message flow — auto-select recipient if not yet set
    let target = recipient;
    if (!target) {
      const cur = agents.value;
      const mgr = cur.find(a => a.role === "manager");
      target = mgr ? mgr.name : cur.length ? cur[0].name : null;
      if (target) setRecipient(target);
    }
    if (!target) return;
    cooldownRef.current = true;
    setTimeout(() => { cooldownRef.current = false; }, 4000);
    try {
      await api.sendMessage(team, target, val);

      // Optimistic insert: add message to local state immediately
      const now = new Date().toISOString();
      const optimistic = {
        id: `optimistic-${Date.now()}`,
        sender: humanName.value || "human",
        recipient: target,
        content: val,
        created_at: now,
        timestamp: now,
        read_at: null,
        task_id: null,
        type: "chat",
      };
      setMsgs(prev => [...prev, optimistic]);
      newestMsgTsRef.current = now;

      if (inputRef.current) { inputRef.current.textContent = ""; inputRef.current.style.height = "auto"; }
      setInputVal("");
      setSendBtnActive(false);
      isAtBottomRef.current = true;
      setShowJumpBtn(false);

      // Double-rAF to ensure scroll runs AFTER Preact flushes the render
      requestAnimationFrame(() => {
        requestAnimationFrame(() => {
          const el = logRef.current;
          if (el) el.scrollTop = el.scrollHeight;
        });
      });
    } catch (e) {
      showToast("Failed to send message", "error");
    }
  }, [team, recipient, mic, executeCommand]);

  const handleKeydown = useCallback((e) => {
    const ac = acRef.current; // always-current autocomplete state

    // Escape always exits command mode (whether autocomplete is visible or not)
    if (e.key === "Escape" && commandMode.value) {
      e.preventDefault();
      commandMode.value = false;
      if (inputRef.current) {
        inputRef.current.textContent = "";
        inputRef.current.style.height = "auto";
      }
      setInputVal("");
      setSendBtnActive(false);
      setHistoryIndex(-1); // Reset history navigation
      draftInputRef.current = '';
      return;
    }

    // Autocomplete keyboard navigation (only when dropdown is visible)
    if (ac.visible) {
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setAcIndex(i => (i + 1) % ac.commands.length);
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setAcIndex(i => (i - 1 + ac.commands.length) % ac.commands.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        const cmd = ac.commands[ac.index];
        if (cmd) selectAutocomplete(cmd);
        return;
      }
    }

    // Command history navigation (ArrowUp/ArrowDown in shell command mode)
    // Only when autocomplete is NOT visible and we're in shell command mode
    if (!ac.visible && commandMode.value) {
      const currentVal = inputRef.current?.textContent || "";
      const cmd = parseCommand(currentVal);

      if (cmd && cmd.name === 'shell') {
        const history = historyRef.current;

        if (e.key === "ArrowUp" && history.length > 0) {
          e.preventDefault();

          // Save current draft if we're starting to navigate history
          if (historyIndex === -1) {
            draftInputRef.current = cmd.args || '';
          }

          // Move back in history
          const newIndex = historyIndex === -1 ? history.length - 1 : Math.max(0, historyIndex - 1);
          setHistoryIndex(newIndex);

          // Populate input with historical command
          const historicalCmd = history[newIndex];
          const newVal = `/shell ${historicalCmd}`;
          if (inputRef.current) {
            inputRef.current.textContent = newVal;
            inputRef.current.style.height = "auto";
            inputRef.current.style.height = inputRef.current.scrollHeight + "px";
            moveCursorToEnd(inputRef.current);
          }
          setInputVal(newVal);
          setSendBtnActive(!!historicalCmd.trim());
          return;
        }

        if (e.key === "ArrowDown" && historyIndex !== -1) {
          e.preventDefault();

          if (historyIndex === history.length - 1) {
            // At the newest entry, restore the draft
            setHistoryIndex(-1);
            const newVal = draftInputRef.current ? `/shell ${draftInputRef.current}` : '/shell ';
            if (inputRef.current) {
              inputRef.current.textContent = newVal;
              inputRef.current.style.height = "auto";
              inputRef.current.style.height = inputRef.current.scrollHeight + "px";
              moveCursorToEnd(inputRef.current);
            }
            setInputVal(newVal);
            setSendBtnActive(!!draftInputRef.current.trim());
            draftInputRef.current = '';
          } else {
            // Move forward in history
            const newIndex = historyIndex + 1;
            setHistoryIndex(newIndex);
            const historicalCmd = history[newIndex];
            const newVal = `/shell ${historicalCmd}`;
            if (inputRef.current) {
              inputRef.current.textContent = newVal;
              inputRef.current.style.height = "auto";
              inputRef.current.style.height = inputRef.current.scrollHeight + "px";
              moveCursorToEnd(inputRef.current);
            }
            setInputVal(newVal);
            setSendBtnActive(!!historicalCmd.trim());
          }
          return;
        }
      }
    }

    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend, historyIndex]);

  const handlePaste = useCallback(async (e) => {
    // Check for image in clipboard
    const items = e.clipboardData.items;
    for (let item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const blob = item.getAsFile();
        const timestamp = Date.now();
        const file = new File([blob], `pasted-image-${timestamp}.png`, { type: blob.type });
        // Simulate file input event
        const fakeEvent = { target: { files: [file] } };
        await handleFileSelect(fakeEvent);
        return;
      }
    }

    // Strip HTML formatting on paste for text
    e.preventDefault();
    const text = e.clipboardData.getData('text/plain');
    document.execCommand('insertText', false, text);
  }, [handleFileSelect]);


  // Select a command from autocomplete — fill the input and enter argument mode
  const selectAutocomplete = useCallback((cmd) => {
    if (inputRef.current) {
      const newVal = `/${cmd.name} `;
      inputRef.current.textContent = newVal;
      moveCursorToEnd(inputRef.current);
      inputRef.current.focus();
      setInputVal(newVal);
      setSendBtnActive(true);
      setAcIndex(0);
    }
  }, []);

  // Move cursor to end of contenteditable element
  const moveCursorToEnd = useCallback((el) => {
    if (!el) return;
    const range = document.createRange();
    const sel = window.getSelection();
    range.selectNodeContents(el);
    range.collapse(false);
    sel.removeAllRanges();
    sel.addRange(range);
  }, []);

  const toggleDirection = useCallback(() => {
    chatFilterDirection.value = direction === "one-way" ? "bidi" : "one-way";
  }, [direction]);

  const toggleMute = useCallback(() => {
    const next = !isMuted.value;
    isMuted.value = next;
    localStorage.setItem("delegate-muted", next ? "true" : "false");
  }, []);

  const onSearchInput = useCallback((e) => {
    const val = e.target.value;
    clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setFilterSearch(val), 300);
  }, []);

  const searchInputRef = useRef();

  const handleSearchExpand = useCallback(() => {
    setSearchExpanded(true);
    // Focus input after state update
    setTimeout(() => searchInputRef.current?.focus(), 0);
  }, []);

  const handleSearchBlur = useCallback(() => {
    if (!filterSearch.trim()) {
      setSearchExpanded(false);
    }
  }, [filterSearch]);

  const handleSearchKeyDown = useCallback((e) => {
    if (e.key === "Escape" && !filterSearch.trim()) {
      setSearchExpanded(false);
      searchInputRef.current?.blur();
    }
  }, [filterSearch]);

  // Team selector options
  const teamOptions = useMemo(() => {
    return teamList.map(t => {
      const name = typeof t === "object" ? t.name : t;
      return { value: name, label: cap(name) };
    });
  }, [teamList]);

  const handleTeamChange = useCallback((newTeam) => {
    if (newTeam !== team) {
      navigate(newTeam, activeTab.value);
    }
  }, [team]);

  return (
    <div
      class="panel active"
      style={{ display: activeTab.value === "chat" ? "" : "none" }}
      onDragEnter={handleDragEnter}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
    >
      {/* Drop zone overlay */}
      {showDropZone && (
        <div class="drop-zone-overlay">
          <div class="drop-zone-content">Drop files here</div>
        </div>
      )}

      {/* Consolidated filter bar with team selector */}
      <div class="chat-filters">
        <PillSelect
          label="Team"
          value={team}
          options={teamOptions}
          onChange={handleTeamChange}
        />
        <PillSelect
          label="From"
          value={filterFrom}
          options={[{ value: "", label: "All" }, ...agentOptions]}
          onChange={setFilterFrom}
        />
        <span
          class={"filter-arrow" + (direction === "bidi" ? " bidi" : "")}
          onClick={toggleDirection}
          title="Toggle direction"
        >
          {direction === "bidi" ? "\u2194" : "\u2192"}
        </span>
        <PillSelect
          label="To"
          value={filterTo}
          options={[{ value: "", label: "All" }, ...agentOptions]}
          onChange={setFilterTo}
        />
        <PillSelect
          label="Type"
          value={typeFilter}
          options={[
            { value: "all", label: "All" },
            { value: "chat", label: "Chat" },
            { value: "events", label: "Events" }
          ]}
          onChange={setTypeFilter}
        />
        <div style={{ flex: 1 }} />
        <div class={searchExpanded ? "filter-search-wrap expanded" : "filter-search-wrap"}>
          {!searchExpanded ? (
            <button
              class="filter-search-icon-btn"
              onClick={handleSearchExpand}
              title="Search messages"
            >
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
              </svg>
            </button>
          ) : (
            <>
              <svg class="filter-search-icon" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
              </svg>
              <input
                ref={searchInputRef}
                type="text"
                class="filter-search"
                placeholder="Search messages..."
                value={filterSearch}
                onInput={onSearchInput}
                onBlur={handleSearchBlur}
                onKeyDown={handleSearchKeyDown}
              />
            </>
          )}
        </div>
      </div>

      {/* Message list */}
      <div class="chat-log" ref={logRef}>
        {filteredMsgs.map((m, i) => {
          if (m.type === "event") {
            const eventHtml = agentifyRefs(linkifyFilePaths(linkifyTaskRefs(esc(m.content))), agNames);
            return (
              <div key={m.id || i} class="msg-event">
                <CollapsibleEventMessage html={eventHtml} messageId={m.id || `event-${i}`} />
                <span class="msg-event-time">{fmtTimestamp(m.timestamp)}</span>
                <span class="msg-event-check-spacer" />
              </div>
            );
          }
          if (m.type === "command") {
            const parsed = parseCommand(m.content);
            return <CommandMessage key={m.id || i} message={m} parsed={parsed} />;
          }
          const contentHtml = linkifyFilePaths(linkifyTaskRefs(renderMarkdown(m.content)));
          const senderLower = m.sender.toLowerCase();
          const human = (humanName.value || "human").toLowerCase();
          const isHuman = senderLower === human;
          const isToHuman = (m.recipient || "").toLowerCase() === human;
          const isBoss = isHuman || isToHuman;
          const msgClass = isBoss ? "msg msg-boss" : "msg";
          const senderClass = isBoss ? "msg-sender msg-sender-boss copyable" : "msg-sender copyable";
          return (
            <div key={m.id || i} class={msgClass}>
              <div class="msg-body">
                <div class="msg-header">
                  <span
                    class={senderClass}
                    onClick={() => { openPanel("agent", m.sender); }}
                  >
                    {cap(m.sender)}<CopyBtn text={m.sender} />
                  </span>
                  <span class="msg-recipient"> → {cap(m.recipient)}</span>
                  {m.task_id != null && (
                    <>
                      <span class="msg-task-sep">|</span>
                      <span
                        class="msg-task-badge copyable"
                        title={`Task ${taskIdStr(m.task_id)}`}
                        onClick={(e) => { e.stopPropagation(); openPanel("task", m.task_id); }}
                      >
                        {taskIdStr(m.task_id)}<CopyBtn text={taskIdStr(m.task_id)} />
                      </span>
                    </>
                  )}
                  <span class="msg-time" dangerouslySetInnerHTML={{ __html: fmtTimestamp(m.timestamp) }} />
                  <span class="msg-checkmark" dangerouslySetInnerHTML={{ __html: msgStatusIcon(m) }} />
                </div>
                <CollapsibleMessage html={contentHtml} messageId={m.id} isBoss={isBoss} />
              </div>
            </div>
          );
        })}
      </div>

      {/* Jump to bottom */}
      {showJumpBtn && (
        <button class="chat-jump-btn" onClick={jumpToBottom} title="Jump to latest">
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="7" y1="2" x2="7" y2="12" /><polyline points="3,8 7,12 11,8" />
          </svg>
        </button>
      )}

      {/* Manager activity indicator */}
      <ManagerActivityBar />

      {/* Text selection tooltip */}
      <SelectionTooltip containerRef={logRef} chatInputRef={inputRef} />

      {/* Upload progress area */}
      {uploadingFiles.value.length > 0 && (
        <div class="upload-progress-area">
          {uploadingFiles.value.map((f, i) => (
            <div key={i} class="upload-progress-item">
              <span class="upload-filename">{f.name}</span>
              {f.error ? (
                <span class="upload-error">{f.error}</span>
              ) : (
                <div class="upload-progress-bar">
                  <div class="upload-progress-fill" style={`width: ${f.progress}%`} />
                </div>
              )}
              <button class="upload-cancel-btn" onClick={() => cancelUpload(f)} title="Cancel">×</button>
            </div>
          ))}
        </div>
      )}

      {/* Chat input — Cursor-style: contenteditable on top, toolbar on bottom */}
      <div class={`chat-input-box ${commandMode.value ? 'command-mode' : ''}`}>
          {/* Autocomplete dropdown — only while typing command name */}
          {acVisible && (
            <CommandAutocomplete
              commands={acCommands}
              selectedIndex={acIndex}
              onSelect={selectAutocomplete}
            />
          )}
          <div
            ref={inputRef}
            class="chat-input"
            contentEditable="plaintext-only"
            onKeyDown={handleKeydown}
            onPaste={handlePaste}
            onInput={(e) => {
              const val = e.target.textContent || "";
              e.target.style.height = "auto";
              e.target.style.height = e.target.scrollHeight + "px";
              setSendBtnActive(!!val.trim());
              setInputVal(val);

              // Detect command mode
              commandMode.value = val.startsWith('/');

              // Update CWD from parsed command if it has -d flag
              const cmd = parseCommand(val);
              if (cmd && cmd.name === 'shell' && cmd.args.includes('-d')) {
                const match = cmd.args.match(/-d\s+(\S+)/);
                if (match) {
                  commandCwd.value = match[1];
                  saveTeamCwd(team, match[1]);
                }
              }
            }}
          />
        {/* Argument-mode hints: show CWD for /shell, or usage for recognized command */}
        {commandMode.value && recognizedCmd && recognizedCmd.name === 'shell' && (
          <div class="chat-cwd-badge">
            <span class="chat-cwd-label">cwd:</span>
            <input
              type="text"
              class="chat-cwd-input"
              value={commandCwd.value || '~'}
              placeholder="~"
              onInput={(e) => {
                commandCwd.value = e.target.value;
                saveTeamCwd(team, e.target.value);
              }}
            />
          </div>
        )}
        {commandMode.value && recognizedCmd && recognizedCmd.name !== 'shell' && !acParsed?.args && (
          <div class="command-hint">
            <span class="command-hint-text">{recognizedCmd.description} — press Enter to run</span>
          </div>
        )}
        <div class="chat-input-toolbar">
          <div class="chat-input-toolbar-spacer" />
          {!commandMode.value && (
            <button
              class="chat-tool-btn upload-btn"
              onClick={triggerFileInput}
              title="Upload file"
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 6.5a3.5 3.5 0 0 1-7 0V3a2 2 0 1 1 4 0v4.5a1 1 0 0 1-2 0V3.5" />
              </svg>
            </button>
          )}
          <input
            type="file"
            ref={fileInputRef}
            style="display:none"
            multiple
            accept=".png,.jpg,.jpeg,.gif,.webp,.svg,.pdf,.md,.txt,.csv,.json,.yaml,.yml,.zip,.html,.css,.js,.py"
            onChange={handleFileSelect}
          />
          {mic.supported && !commandMode.value && (
            <button
              class={"chat-tool-btn" + (mic.active ? " recording" : "")}
              onClick={mic.toggle}
              title={mic.active ? "Stop recording" : "Voice input"}
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="5.5" y="1" width="5" height="9" rx="2.5" />
                <path d="M3 7.5a5 5 0 0 0 10 0" />
                <line x1="8" y1="12.5" x2="8" y2="15" />
              </svg>
            </button>
          )}
          {!commandMode.value && (
            <button
              class="chat-tool-btn"
              onClick={toggleMute}
              title={muted ? "Unmute notifications" : "Mute notifications"}
            >
              <BellIcon muted={muted} />
            </button>
          )}
          <button
            class={"chat-tool-btn send-btn" + (sendBtnActive ? " active" : "")}
            onClick={handleSend}
            title={commandMode.value ? "Run command" : "Send message"}
          >
            {commandMode.value ? (
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="4,12 8,8 4,4" />
                <line x1="12" y1="4" x2="12" y2="12" />
              </svg>
            ) : (
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <line x1="8" y1="12" x2="8" y2="4" />
                <polyline points="4.5,7.5 8,4 11.5,7.5" />
              </svg>
            )}
          </button>
        </div>
      </div>
    </div>
  );
}
