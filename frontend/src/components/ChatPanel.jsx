import { useState, useEffect, useCallback, useRef, useMemo } from "preact/hooks";
import {
  currentTeam, messages, agents, activeTab,
  chatFilterDirection, diffPanelMode, diffPanelTarget, taskPanelId,
  knownAgentNames,
} from "../state.js";
import * as api from "../api.js";
import {
  cap, esc, fmtTimestamp, renderMarkdown, avatarColor, avatarInitial,
  linkifyTaskRefs, linkifyFilePaths, agentifyRefs, msgStatusIcon,
} from "../utils.js";
import { playMsgSound } from "../audio.js";

// ── Linked content with event delegation ──
function LinkedDiv({ html, class: cls, style }) {
  const ref = useRef();

  useEffect(() => {
    if (!ref.current) return;
    const handler = (e) => {
      const taskLink = e.target.closest("[data-task-id]");
      if (taskLink) { e.stopPropagation(); taskPanelId.value = parseInt(taskLink.dataset.taskId, 10); return; }
      const agentLink = e.target.closest("[data-agent-name]");
      if (agentLink) { e.stopPropagation(); diffPanelMode.value = "agent"; diffPanelTarget.value = agentLink.dataset.agentName; return; }
      const fileLink = e.target.closest("[data-file-path]");
      if (fileLink) { e.stopPropagation(); diffPanelMode.value = "file"; diffPanelTarget.value = fileLink.dataset.filePath; return; }
    };
    ref.current.addEventListener("click", handler);
    return () => ref.current && ref.current.removeEventListener("click", handler);
  }, [html]);

  return <div ref={ref} class={cls} style={style} dangerouslySetInnerHTML={{ __html: html }} />;
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
      if (e.error !== "aborted" && e.error !== "no-speech") console.warn("Speech error:", e.error);
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

  const mic = useSpeechRecognition(inputRef);

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

  // Auto-scroll
  useEffect(() => {
    const el = logRef.current;
    if (el) {
      const wasNearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
      if (wasNearBottom) el.scrollTop = el.scrollHeight;
    }
  }, [filteredMsgs]);

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

  const recipientOptions = useMemo(() => {
    const mgrs = allAgents.filter(a => a.role === "manager").sort((a, b) => a.name.localeCompare(b.name));
    const others = allAgents.filter(a => a.role !== "manager").sort((a, b) => a.name.localeCompare(b.name));
    return [...mgrs, ...others];
  }, [allAgents]);

  const handleSend = useCallback(async () => {
    if (mic.active) mic.toggle();
    const val = inputRef.current ? inputRef.current.value.trim() : "";
    if (!val || !team || !recipient) return;
    cooldownRef.current = true;
    setTimeout(() => { cooldownRef.current = false; }, 4000);
    try {
      await api.sendMessage(team, recipient, val);
      if (inputRef.current) { inputRef.current.value = ""; inputRef.current.style.height = "auto"; }
      setInputVal("");
      setSendBtnActive(false);
    } catch (e) {
      console.error("Send error:", e);
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

  const onSearchInput = useCallback((e) => {
    const val = e.target.value;
    clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setFilterSearch(val), 300);
  }, []);

  // ── Render ──
  const searchIcon = (
    <svg class="filter-search-icon" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
    </svg>
  );

  return (
    <div class="panel active" style={{ display: activeTab.value === "chat" ? "" : "none" }}>
      <div class="chat-filters">
        <div class="filter-search-wrap">
          {searchIcon}
          <input
            type="text"
            class="filter-search"
            placeholder="Search messages..."
            value={filterSearch}
            onInput={onSearchInput}
          />
        </div>
        <select value={filterFrom} onChange={e => setFilterFrom(e.target.value)}>
          <option value="">Anyone</option>
          {agentOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <span
          class={"filter-arrow" + (direction === "bidi" ? " bidi" : "")}
          onClick={toggleDirection}
          title="Toggle direction: one-way / bidirectional"
        >
          {direction === "bidi" ? "\u2194" : "\u2192"}
        </span>
        <select value={filterTo} onChange={e => setFilterTo(e.target.value)}>
          <option value="">Anyone</option>
          {agentOptions.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
        </select>
        <label>
          <input type="checkbox" checked={showEvents} onChange={e => setShowEvents(e.target.checked)} />
          {" "}Activity
        </label>
      </div>
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
          const c = avatarColor(m.sender);
          const contentHtml = linkifyFilePaths(linkifyTaskRefs(renderMarkdown(m.content)));
          return (
            <div key={m.id || i} class="msg">
              <div class="msg-avatar" style={{ background: c }}>{avatarInitial(m.sender)}</div>
              <div class="msg-body">
                <div class="msg-header">
                  <span
                    class="msg-sender"
                    style={{ cursor: "pointer" }}
                    onClick={() => { diffPanelMode.value = "agent"; diffPanelTarget.value = m.sender; }}
                  >
                    {cap(m.sender)}
                  </span>
                  <span class="msg-recipient">&rarr; {cap(m.recipient)}</span>
                  <span class="msg-time">{fmtTimestamp(m.timestamp)}</span>
                </div>
                <LinkedDiv class="msg-content md-content" html={contentHtml} />
              </div>
            </div>
          );
        })}
      </div>
      <div class="chat-input-container">
        <div class="chat-input-top">
          <select value={recipient} onChange={e => setRecipient(e.target.value)}>
            {recipientOptions.map(a => (
              <option key={a.name} value={a.name}>{cap(a.name)} ({a.role || "worker"})</option>
            ))}
          </select>
        </div>
        <textarea
          ref={inputRef}
          placeholder="Send a message..."
          rows="1"
          onKeyDown={handleKeydown}
          onInput={(e) => {
            e.target.style.height = "auto";
            e.target.style.height = e.target.scrollHeight + "px";
            setSendBtnActive(!!e.target.value.trim());
          }}
        />
        <div class="chat-input-actions">
          {mic.supported && (
            <button
              class={"mic-btn" + (mic.active ? " recording" : "")}
              onClick={mic.toggle}
              title={mic.active ? "Stop recording" : "Voice input"}
              aria-label="Voice input"
            >
              <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
                <rect x="5.5" y="1" width="5" height="9" rx="2.5" />
                <path d="M3 7.5a5 5 0 0 0 10 0" />
                <line x1="8" y1="12.5" x2="8" y2="15" />
                <line x1="5.5" y1="15" x2="10.5" y2="15" />
              </svg>
            </button>
          )}
          <button
            class={"send-btn" + (sendBtnActive ? " active" : "")}
            onClick={handleSend}
            title="Send message"
            aria-label="Send message"
          >
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="8" cy="8" r="7" />
              <line x1="8" y1="11" x2="8" y2="5" />
              <polyline points="5.5,7.5 8,5 10.5,7.5" />
            </svg>
          </button>
        </div>
      </div>
    </div>
  );
}
