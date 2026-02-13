import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, messages, agents, activeTab,
  chatFilterDirection, openPanel,
  knownAgentNames, isMuted, humanName, expandedMessages,
  commandMode, commandCwd, teams, navigate,
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
import { ManagerActivityBar } from "./ManagerActivityBar.jsx";
import { SelectionTooltip } from "./SelectionTooltip.jsx";
import { CommandAutocomplete } from "./CommandAutocomplete.jsx";
import { ShellOutputBlock } from "./ShellOutputBlock.jsx";
import { StatusBlock } from "./StatusBlock.jsx";
import { parseCommand, COMMANDS } from "../commands.js";

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
      const fname = fpath.split("/").pop();
      const isHtmlFile = /\.html?$/i.test(fname);
      if (isHtmlFile) {
        window.open(`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(fpath, currentTeam.value))}`, "_blank");
      } else {
        openPanel("file", fpath);
      }
      return;
    }
  }, []);

  return <div ref={externalRef} class={cls} style={style} onClick={handler} dangerouslySetInnerHTML={{ __html: html }} />;
}

// ── Collapsible long message ──
const COLLAPSE_THRESHOLD = 90; // ~4 lines at 14px * 1.6 line-height = 89.6px

function CollapsibleMessage({ html, messageId, isHuman }) {
  const contentRef = useRef();
  const [isLong, setIsLong] = useState(false);
  const isExpanded = expandedMessages.value.has(messageId);

  useEffect(() => {
    if (!contentRef.current) return;
    const checkOverflow = () => {
      const el = contentRef.current;
      if (el && el.scrollHeight > COLLAPSE_THRESHOLD) {
        setIsLong(true);
      } else {
        setIsLong(false);
      }
    };
    checkOverflow();

    // Re-check on resize
    const observer = new ResizeObserver(checkOverflow);
    if (contentRef.current) observer.observe(contentRef.current);
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

  const contentClass = isHuman ? "msg-content md-content" : "msg-content md-content msg-content-dim";

  return (
    <>
      <div class={wrapperClass}>
        <LinkedDiv class={contentClass} html={html} ref={contentRef} />
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
        inputRef.current.value = baseTextRef.current + finalTextRef.current + interim;
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
      baseTextRef.current = el && el.value ? el.value + " " : "";
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
  const teamList = teams.value;

  const [msgs, setMsgs] = useState([]);
  const [filterFrom, setFilterFrom] = useState("");
  const [filterTo, setFilterTo] = useState("");
  const [filterSearch, setFilterSearch] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [showEvents, setShowEvents] = useState(true);
  const [recipient, setRecipient] = useState("");
  const [inputVal, setInputVal] = useState("");
  const [sendBtnActive, setSendBtnActive] = useState(false);
  const [isLoadingOlder, setIsLoadingOlder] = useState(false);
  const [hasMoreMessages, setHasMoreMessages] = useState(true);

  const direction = chatFilterDirection.value;
  const logRef = useRef();
  const inputRef = useRef();
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
        draftsByTeam.current[prevTeam] = inputRef.current.value;
      }
      // Restore draft for new team
      const draft = draftsByTeam.current[team] || "";
      if (inputRef.current) {
        inputRef.current.value = draft;
        inputRef.current.style.height = "auto";
        inputRef.current.style.height = inputRef.current.scrollHeight + "px";
      }
      setInputVal(draft);
      setSendBtnActive(!!draft.trim());
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
      if (f.showEvents === false) setShowEvents(false);
      if (f.direction === "bidi") chatFilterDirection.value = "bidi";
    } catch (e) { }
  }, []);

  // Save filters
  useEffect(() => {
    try {
      sessionStorage.setItem("chatFilters", JSON.stringify({
        search: filterSearch, from: filterFrom, to: filterTo,
        showEvents, direction,
      }));
    } catch (e) { }
  }, [filterSearch, filterFrom, filterTo, showEvents, direction]);

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
    if (!showEvents) filtered = filtered.filter(m => m.type !== "event");
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
  }, [msgs, showEvents, filterFrom, filterTo, filterSearch, direction]);

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
          result = await api.execShell(team, cmd.args, commandCwd.value || undefined);
        }
      } else if (cmd.name === 'status') {
        // Status is client-side, build result from API calls
        const [agentsData, tasksData] = await Promise.all([
          api.fetchAgents(team),
          api.fetchTasks(team),
        ]);
        const agents = agentsData.map(a => ({
          name: a.name,
          status: a.status || 'idle',
          current_task: a.current_task_id || null,
          last_turn: a.last_turn_at || null,
        }));
        const taskCounts = {
          todo: tasksData.filter(t => t.status === 'todo').length,
          in_progress: tasksData.filter(t => t.status === 'in_progress').length,
          in_review: tasksData.filter(t => t.status === 'in_review').length,
          in_approval: tasksData.filter(t => t.status === 'in_approval').length,
          total: tasksData.filter(t => t.status !== 'done' && t.status !== 'cancelled').length,
        };
        result = { agents, taskCounts };
      } else {
        result = { error: `Unknown command: /${cmd.name}. Available: /shell, /status`, exit_code: -1 };
      }

      // Persist to DB
      const saved = await api.saveCommand(team, cmd.raw, result);

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
    const val = inputRef.current ? inputRef.current.value.trim() : "";
    if (!val || !team) return;

    // Check for command
    const cmd = parseCommand(val);
    if (cmd && COMMANDS[cmd.name]) {
      if (inputRef.current) {
        inputRef.current.value = "";
        inputRef.current.style.height = "auto";
      }
      setInputVal("");
      setSendBtnActive(false);
      commandMode.value = false;
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

      if (inputRef.current) { inputRef.current.value = ""; inputRef.current.style.height = "auto"; }
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
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

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
    return teamList.map(t => ({ value: t, label: cap(t) }));
  }, [teamList]);

  const handleTeamChange = useCallback((newTeam) => {
    if (newTeam !== team) {
      navigate(newTeam, activeTab.value);
    }
  }, [team]);

  return (
    <div class="panel active" style={{ display: activeTab.value === "chat" ? "" : "none" }}>
      {/* Consolidated filter bar with team selector */}
      <div class="chat-filters">
        <CustomSelect
          className="chat-team-select"
          value={team}
          options={teamOptions}
          onChange={handleTeamChange}
        />
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
        <CustomSelect
          className="chat-filter-select"
          value={filterFrom}
          options={[{ value: "", label: "From: All" }, ...agentOptions]}
          onChange={setFilterFrom}
        />
        <span
          class={"filter-arrow" + (direction === "bidi" ? " bidi" : "")}
          onClick={toggleDirection}
          title="Toggle direction"
        >
          {direction === "bidi" ? "\u2194" : "\u2192"}
        </span>
        <CustomSelect
          className="chat-filter-select"
          value={filterTo}
          options={[{ value: "", label: "To: All" }, ...agentOptions]}
          onChange={setFilterTo}
        />
        <label>
          <input type="checkbox" checked={showEvents} onChange={e => setShowEvents(e.target.checked)} />
          {" "}Events
        </label>
      </div>

      {/* Message list */}
      <div class="chat-log" ref={logRef}>
        {filteredMsgs.map((m, i) => {
          if (m.type === "event") {
            const eventHtml = agentifyRefs(linkifyFilePaths(linkifyTaskRefs(esc(m.content))), agNames);
            return (
              <div key={m.id || i} class="msg-event">
                <div class="msg-event-icon">
                  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M1 7a6 6 0 1 0 2-4.5" /><polyline points="1 1 1 3.5 3.5 3.5" />
                  </svg>
                </div>
                <LinkedDiv class="msg-event-text" html={eventHtml} />
                <span class="msg-event-time">{fmtTimestamp(m.timestamp)}</span>
              </div>
            );
          }
          if (m.type === "command") {
            const parsed = parseCommand(m.content);
            return (
              <div key={m.id || i} class="msg-command">
                <div class="msg-command-header">
                  <span class="msg-command-sender">{cap(m.sender)}</span>
                  <span class="msg-command-text">{m.content}</span>
                  <span class="msg-time">{fmtTimestamp(m.timestamp)}</span>
                </div>
                {parsed?.name === 'shell' && <ShellOutputBlock result={m.result} />}
                {parsed?.name === 'status' && <StatusBlock result={m.result} />}
              </div>
            );
          }
          const contentHtml = linkifyFilePaths(linkifyTaskRefs(renderMarkdown(m.content)));
          const senderLower = m.sender.toLowerCase();
          const human = (humanName.value || "human").toLowerCase();
          const isHuman = senderLower === human;
          const isToHuman = (m.recipient || "").toLowerCase() === human;
          return (
            <div key={m.id || i} class="msg">
              <div class="msg-body">
                <div class="msg-header">
                  <span
                    class="msg-sender copyable"
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
                <CollapsibleMessage html={contentHtml} messageId={m.id} isHuman={isHuman || isToHuman} />
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

      {/* Command autocomplete dropdown - positioned relative to input box */}
      <div class="chat-input-wrapper">
        {/* Chat input — Cursor-style: textarea on top, toolbar on bottom */}
        <div class={`chat-input-box ${commandMode.value ? 'command-mode' : ''}`}>
          {commandMode.value && (
            <CommandAutocomplete
              input={inputVal}
              onSelect={(cmd) => {
                if (inputRef.current) {
                  inputRef.current.value = `/${cmd.name} `;
                  inputRef.current.focus();
                  setInputVal(`/${cmd.name} `);
                  setSendBtnActive(true);
                }
              }}
              onDismiss={() => {
                commandMode.value = false;
              }}
            />
          )}
          <textarea
            ref={inputRef}
            class={!commandMode.value && inputVal && hasMarkdown(inputVal) ? "chat-input-has-overlay" : ""}
            placeholder="Send a message..."
            rows="2"
            onKeyDown={handleKeydown}
            onInput={(e) => {
              const val = e.target.value;
              e.target.style.height = "auto";
              e.target.style.height = e.target.scrollHeight + "px";
              setSendBtnActive(!!val.trim());
              setInputVal(val);

              // Detect command mode
              commandMode.value = val.startsWith('/');

              // Update CWD from parsed command if it has -d flag
              const cmd = parseCommand(val);
              if (cmd && cmd.name === 'shell' && cmd.args.includes('-d')) {
                // Simple -d flag parsing (not perfect but works for basic usage)
                const match = cmd.args.match(/-d\s+(\S+)/);
                if (match) commandCwd.value = match[1];
              }
            }}
          />
          {!commandMode.value && inputVal && hasMarkdown(inputVal) && (
            <div
              class="chat-input-overlay"
              dangerouslySetInnerHTML={{ __html: renderInlineMarkdown(inputVal) }}
            />
          )}
        </div>
        {commandMode.value && parseCommand(inputVal)?.name === 'shell' && (
          <div class="chat-cwd-badge">
            <span class="chat-cwd-label">cwd:</span>
            <input
              type="text"
              class="chat-cwd-input"
              value={commandCwd.value || '~'}
              placeholder="~"
              onInput={(e) => { commandCwd.value = e.target.value; }}
            />
          </div>
        )}
        <div class="chat-input-toolbar">
          <div class="chat-input-toolbar-spacer" />
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
    </div>
  );
}
