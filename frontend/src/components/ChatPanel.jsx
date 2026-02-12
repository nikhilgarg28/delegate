import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, messages, agents, activeTab,
  chatFilterDirection, diffPanelMode, diffPanelTarget, taskPanelId,
  knownAgentNames, isMuted, bossName, expandedMessages,
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
      taskPanelId.value = parseInt(taskLink.dataset.taskId, 10);
      return;
    }
    const agentLink = e.target.closest("[data-agent-name]");
    if (agentLink) { e.stopPropagation(); diffPanelMode.value = "agent"; diffPanelTarget.value = agentLink.dataset.agentName; return; }
    const fileLink = e.target.closest("[data-file-path]");
    if (fileLink) {
      e.stopPropagation();
      const fpath = fileLink.dataset.filePath;
      const fname = fpath.split("/").pop();
      const isHtmlFile = /\.html?$/i.test(fname);
      if (isHtmlFile) {
        window.open(`/teams/${currentTeam.value}/files/raw?path=${encodeURIComponent(toApiPath(fpath, currentTeam.value))}`, "_blank");
      } else {
        diffPanelMode.value = "file"; diffPanelTarget.value = fpath;
      }
      return;
    }
  }, []);

  return <div ref={externalRef} class={cls} style={style} onClick={handler} dangerouslySetInnerHTML={{ __html: html }} />;
}

// ── Collapsible long message ──
const COLLAPSE_THRESHOLD = 90; // ~4 lines at 14px * 1.6 line-height = 89.6px

function CollapsibleMessage({ html, messageId, isBoss }) {
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

  return (
    <>
      <div class={wrapperClass}>
        <LinkedDiv class="msg-content md-content" html={html} ref={contentRef} />
        {isLong && !isExpanded && (
          <div class={`msg-fade-overlay ${isBoss ? 'msg-fade-overlay-boss' : ''}`} />
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
  const msgs = messages.value;
  const allAgents = agents.value;
  const agNames = knownAgentNames.value;

  const [filterFrom, setFilterFrom] = useState("");
  const [filterTo, setFilterTo] = useState("");
  const [filterSearch, setFilterSearch] = useState("");
  const [showEvents, setShowEvents] = useState(true);
  const [recipient, setRecipient] = useState("");
  const [inputVal, setInputVal] = useState("");
  const [sendBtnActive, setSendBtnActive] = useState(false);

  const direction = chatFilterDirection.value;
  const logRef = useRef();
  const inputRef = useRef();
  const searchTimerRef = useRef(null);
  const lastMsgTsRef = useRef("");
  const cooldownRef = useRef(false);
  const isAtBottomRef = useRef(true);
  const [showJumpBtn, setShowJumpBtn] = useState(false);
  const initialScrollDone = useRef(false);

  const mic = useSpeechRecognition(inputRef);
  const muted = isMuted.value;

  // Restore filters from session storage
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("chatFilters");
      if (!raw) return;
      const f = JSON.parse(raw);
      if (f.search) setFilterSearch(f.search);
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

  // Track scroll position
  useEffect(() => {
    const el = logRef.current;
    if (!el) return;
    const onScroll = () => {
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      isAtBottomRef.current = nearBottom;
      setShowJumpBtn(!nearBottom);
    };
    el.addEventListener("scroll", onScroll, { passive: true });
    return () => el.removeEventListener("scroll", onScroll);
  }, []);

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


  const handleSend = useCallback(async () => {
    if (mic.active) mic.toggle();
    const val = inputRef.current ? inputRef.current.value.trim() : "";
    if (!val || !team || !recipient) return;
    cooldownRef.current = true;
    setTimeout(() => { cooldownRef.current = false; }, 4000);
    try {
      await api.sendMessage(team, recipient, val);

      // Optimistic insert: add message to signal immediately
      const now = new Date().toISOString();
      const optimistic = {
        id: `optimistic-${Date.now()}`,
        sender: bossName.value || "boss",
        recipient: recipient,
        content: val,
        created_at: now,
        timestamp: now,
        read_at: null,
        task_id: null,
        type: "chat",
      };
      messages.value = [...messages.value, optimistic];

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
  }, [team, recipient, mic]);

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

  return (
    <div class="panel active" style={{ display: activeTab.value === "chat" ? "" : "none" }}>
      {/* Minimal filter bar */}
      <div class="chat-filters">
        <div class="filter-search-wrap">
          <svg class="filter-search-icon" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
          </svg>
          <input
            type="text"
            class="filter-search"
            placeholder="Search messages..."
            value={filterSearch}
            onInput={onSearchInput}
          />
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
          const contentHtml = linkifyFilePaths(linkifyTaskRefs(renderMarkdown(m.content)));
          const senderLower = m.sender.toLowerCase();
          const boss = (bossName.value || "boss").toLowerCase();
          const isBoss = senderLower === boss;
          const isToBoss = (m.recipient || "").toLowerCase() === boss;
          const msgClass = (isBoss || isToBoss) ? "msg msg-boss" : "msg";
          return (
            <div key={m.id || i} class={msgClass}>
              <div class="msg-body">
                <div class="msg-header">
                  <span
                    class="msg-sender copyable"
                    onClick={() => { diffPanelMode.value = "agent"; diffPanelTarget.value = m.sender; }}
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
                        onClick={(e) => { e.stopPropagation(); taskPanelId.value = m.task_id; }}
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

      {/* Chat input — Cursor-style: textarea on top, toolbar on bottom */}
      <div class="chat-input-box">
        <textarea
          ref={inputRef}
          placeholder="Send a message..."
          rows="2"
          onKeyDown={handleKeydown}
          onInput={(e) => {
            e.target.style.height = "auto";
            e.target.style.height = e.target.scrollHeight + "px";
            setSendBtnActive(!!e.target.value.trim());
          }}
        />
        <div class="chat-input-toolbar">
          <div class="chat-input-toolbar-spacer" />
          {mic.supported && (
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
          <button
            class="chat-tool-btn"
            onClick={toggleMute}
            title={muted ? "Unmute notifications" : "Mute notifications"}
          >
            <BellIcon muted={muted} />
          </button>
          <button
            class={"chat-tool-btn send-btn" + (sendBtnActive ? " active" : "")}
            onClick={handleSend}
            title="Send message"
          >
            <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <line x1="8" y1="12" x2="8" y2="4" />
              <polyline points="4.5,7.5 8,4 11.5,7.5" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
