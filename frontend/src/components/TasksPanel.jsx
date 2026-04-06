import { useState, useEffect, useMemo, useCallback, useRef } from "preact/hooks";
import { currentTeam, tasks, activeTab, openPanel, taskTeamFilter, teams, getWorkflowStages, isInputFocused } from "../state.js";
import { cap, prettyName, fmtStatus, taskIdStr } from "../utils.js";
import { playTaskSound, playApprovalSound } from "../audio.js";
import { seedTaskCache } from "./TaskSidePanel.jsx";
import { FilterBar, applyFilters } from "./FilterBar.jsx";
import { fetchMergeOrder, fetchReviewer, setReviewer, fetchTaskFreeze, setTaskFreeze, fetchMaxTasks, setMaxTasks } from "../api.js";
import { PillSelect } from "./PillSelect.jsx";
import { CopyBtn } from "./CopyBtn.jsx";

// ── Fallback status options (used when no workflow is loaded) ──
const FALLBACK_STATUS_OPTIONS = [
  "todo", "in_progress", "in_review", "in_approval", "merging", "done", "rejected", "merge_failed", "cancelled",
];
const PRIORITY_OPTIONS = ["low", "medium", "high", "critical"];
const APPROVAL_OPTIONS = ["approved", "rejected", "(none)"];

const DEFAULT_FILTERS = [
  { field: "status", operator: "noneOf", values: ["done", "cancelled"] }
];

export function TasksPanel() {
  const team = currentTeam.value;
  const allTasks = tasks.value;
  const teamFilter = taskTeamFilter.value;
  const allTeams = teams.value || [];

  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [collapsedTeams, setCollapsedTeams] = useState(new Set());
  const [mergeSort, setMergeSort] = useState(false);
  const [mergeOrder, setMergeOrder] = useState(null);
  const [reviewerAI, setReviewerAI] = useState(false);
  const [autoMerge, setAutoMerge] = useState(false);
  const [taskFreezeOn, setTaskFreezeOn] = useState(false);
  const [maxTasksEnabled, setMaxTasksEnabled] = useState(false);
  const [maxTasksInProgress, setMaxTasksInProgress] = useState(5);
  const [maxTasksQueued, setMaxTasksQueued] = useState(10);
  const searchTimerRef = useRef(null);
  const prevStatusRef = useRef({});

  // Restore filters from session storage on mount
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("taskFilters2");
      if (!raw) return;
      const saved = JSON.parse(raw);
      if (saved.filters) setFilters(saved.filters);
      if (saved.search) {
        setSearchQuery(saved.search);
        setSearchExpanded(true); // Expand if there was saved search text
      }
      if (saved.mergeSort) setMergeSort(true);
    } catch (e) { }
  }, []);

  // Save filters to session storage
  useEffect(() => {
    try {
      sessionStorage.setItem("taskFilters2", JSON.stringify({
        filters, search: searchQuery, mergeSort,
      }));
    } catch (e) { }
  }, [filters, searchQuery, mergeSort]);

  // History API: push state on filter change
  const filtersRef = useRef(filters);
  const searchRef = useRef(searchQuery);
  useEffect(() => {
    // Skip the initial mount (no push on restore)
    if (filtersRef.current === filters && searchRef.current === searchQuery) return;
    filtersRef.current = filters;
    searchRef.current = searchQuery;
    window.history.pushState(
      { taskFilters: filters, taskSearch: searchQuery },
      "",
    );
  }, [filters, searchQuery]);

  // History API: restore on popstate
  useEffect(() => {
    const handler = (e) => {
      if (e.state && e.state.taskFilters !== undefined) {
        setFilters(e.state.taskFilters);
        setSearchQuery(e.state.taskSearch || "");
      }
    };
    window.addEventListener("popstate", handler);
    return () => window.removeEventListener("popstate", handler);
  }, []);

  // Task status change sound
  useEffect(() => {
    let approvalNeeded = false;
    let doneNeeded = false;
    for (const t of allTasks) {
      const prev = prevStatusRef.current[t.id];
      if (prev && prev !== t.status) {
        if (t.status === "in_approval") approvalNeeded = true;
        if (t.status === "done") doneNeeded = true;
      }
      prevStatusRef.current[t.id] = t.status;
    }
    if (approvalNeeded) playApprovalSound();
    if (doneNeeded) playTaskSound();
  }, [allTasks]);

  // Fetch merge order when toggle is on
  useEffect(() => {
    if (!mergeSort) { setMergeOrder(null); return; }
    let cancelled = false;
    fetchMergeOrder(team).then(data => { if (!cancelled) setMergeOrder(data); });
    return () => { cancelled = true; };
  }, [mergeSort, team, allTasks]);

  // Fetch reviewer state (includes auto_merge) on mount / team change
  useEffect(() => {
    let cancelled = false;
    fetchReviewer(team).then(data => {
      if (cancelled) return;
      setReviewerAI(data?.mode === "ai");
      setAutoMerge(!!data?.auto_merge);
    });
    return () => { cancelled = true; };
  }, [team]);

  // Fetch task-freeze state on mount / team change
  useEffect(() => {
    let cancelled = false;
    fetchTaskFreeze(team).then(data => { if (!cancelled) setTaskFreezeOn(!!data?.enabled); });
    return () => { cancelled = true; };
  }, [team]);

  // Fetch max-tasks config on mount / team change
  useEffect(() => {
    let cancelled = false;
    fetchMaxTasks(team).then(data => {
      if (!cancelled) {
        setMaxTasksEnabled(!!data?.enabled);
        setMaxTasksInProgress(data?.limit_in_progress ?? 5);
        setMaxTasksQueued(data?.limit_queued ?? 10);
      }
    });
    return () => { cancelled = true; };
  }, [team]);

  const toggleReviewer = useCallback(() => {
    const next = !reviewerAI;
    setReviewerAI(next);
    // AI Review ON → auto-merge is forced ON
    if (next) setAutoMerge(true);
    setReviewer(team, { mode: next ? "ai" : "human" });
  }, [reviewerAI, team]);

  const toggleAutoMerge = useCallback(() => {
    // Cannot turn off auto-merge while AI Review is on
    if (reviewerAI) return;
    const next = !autoMerge;
    setAutoMerge(next);
    setReviewer(team, { auto_merge: next });
  }, [autoMerge, reviewerAI, team]);

  const toggleTaskFreeze = useCallback(() => {
    const next = !taskFreezeOn;
    setTaskFreezeOn(next);
    setTaskFreeze(team, { enabled: next });
  }, [taskFreezeOn, team]);

  const toggleMaxTasks = useCallback(() => {
    const next = !maxTasksEnabled;
    setMaxTasksEnabled(next);
    setMaxTasks(team, { enabled: next, limit_in_progress: maxTasksInProgress, limit_queued: maxTasksQueued });
  }, [maxTasksEnabled, team, maxTasksInProgress, maxTasksQueued]);

  const updateMaxTasksInProgress = useCallback((val) => {
    const n = Math.max(1, parseInt(val) || 5);
    setMaxTasksInProgress(n);
    if (maxTasksEnabled) {
      setMaxTasks(team, { enabled: true, limit_in_progress: n });
    }
  }, [maxTasksEnabled, team]);

  const updateMaxTasksQueued = useCallback((val) => {
    const n = Math.max(1, parseInt(val) || 10);
    setMaxTasksQueued(n);
    if (maxTasksEnabled) {
      setMaxTasks(team, { enabled: true, limit_queued: n });
    }
  }, [maxTasksEnabled, team]);

  // Build dynamic field config from task data
  const fieldConfig = useMemo(() => {
    const assigneeSet = new Set();
    const driSet = new Set();
    const repoSet = new Set();
    const tagSet = new Set();

    for (const t of allTasks) {
      if (t.assignee) assigneeSet.add(t.assignee);
      if (t.dri) driSet.add(t.dri);
      if (t.repo) {
        const repos = Array.isArray(t.repo) ? t.repo : [t.repo];
        repos.forEach(r => { if (r) repoSet.add(r); });
      }
      if (t.tags) {
        const tags = Array.isArray(t.tags) ? t.tags : [t.tags];
        tags.forEach(tag => { if (tag) tagSet.add(tag); });
      }
    }

    // Use workflow stages if available, otherwise fall back to hardcoded
    const wfStages = getWorkflowStages(team, "default");
    const statusOpts = wfStages
      ? wfStages.map(s => s.key)
      : FALLBACK_STATUS_OPTIONS;

    return [
      { key: "status", label: "Status", options: statusOpts },
      { key: "assignee", label: "Assignee", options: [...assigneeSet].sort() },
      { key: "dri", label: "DRI", options: [...driSet].sort() },
      { key: "priority", label: "Priority", options: PRIORITY_OPTIONS },
      { key: "repo", label: "Repo", options: [...repoSet].sort() },
      { key: "tags", label: "Tags", options: [...tagSet].sort() },
      { key: "approval_status", label: "Approval", options: APPROVAL_OPTIONS },
    ];
  }, [allTasks]);

  // Apply filters + search + sort
  const filtered = useMemo(() => {
    let list = applyFilters(allTasks, filters);
    const sq = searchQuery.toLowerCase().trim();
    if (sq) {
      list = list.filter(t =>
        (t.title || "").toLowerCase().includes(sq) ||
        (t.description || "").toLowerCase().includes(sq)
      );
    }
    if (mergeSort && mergeOrder?.order?.length) {
      const idxMap = new Map(mergeOrder.order.map((id, i) => [id, i]));
      return [...list].sort((a, b) => {
        const ai = idxMap.has(a.id) ? idxMap.get(a.id) : Infinity;
        const bi = idxMap.has(b.id) ? idxMap.get(b.id) : Infinity;
        if (ai !== bi) return ai - bi;
        return b.id - a.id;
      });
    }
    return [...list].sort((a, b) => b.id - a.id);
  }, [allTasks, filters, searchQuery, mergeSort, mergeOrder]);

  const onSearchInput = useCallback((e) => {
    const val = e.target.value;
    clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setSearchQuery(val), 300);
  }, []);

  const searchInputRef = useRef();

  const handleSearchExpand = useCallback(() => {
    setSearchExpanded(true);
    // Focus input after state update
    setTimeout(() => searchInputRef.current?.focus(), 0);
  }, []);

  const handleSearchBlur = useCallback(() => {
    if (!searchQuery.trim()) {
      setSearchExpanded(false);
    }
  }, [searchQuery]);

  const handleSearchKeyDown = useCallback((e) => {
    if (e.key === "Escape" && !searchQuery.trim()) {
      setSearchExpanded(false);
      searchInputRef.current?.blur();
    }
  }, [searchQuery]);

  // Reset selection when filters change
  useEffect(() => {
    setSelectedIndex(-1);
  }, [filters, searchQuery]);

  const searchIcon = (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
    </svg>
  );

  const handleTeamFilterChange = useCallback((val) => {
    taskTeamFilter.value = val;
    setCollapsedTeams(new Set());
    setSelectedIndex(-1);
  }, []);

  const toggleTeamGroup = useCallback((teamName) => {
    setCollapsedTeams(prev => {
      const next = new Set(prev);
      if (next.has(teamName)) {
        next.delete(teamName);
      } else {
        next.add(teamName);
      }
      return next;
    });
  }, []);

  // Group tasks by team when viewing "all"
  const groupedTasks = useMemo(() => {
    if (teamFilter !== "all") {
      return { [team]: filtered };
    }
    const groups = {};
    for (const t of filtered) {
      const tTeam = t.team || team;
      if (!groups[tTeam]) groups[tTeam] = [];
      groups[tTeam].push(t);
    }
    return groups;
  }, [filtered, teamFilter, team]);

  const isGroupedView = teamFilter === "all";

  // Build flat task list for keyboard navigation (respecting collapsed state)
  const flatTaskList = useMemo(() => {
    const list = [];
    Object.entries(groupedTasks).forEach(([teamName, teamTasks]) => {
      if (!isGroupedView || !collapsedTeams.has(teamName)) {
        list.push(...teamTasks);
      }
    });
    return list;
  }, [groupedTasks, isGroupedView, collapsedTeams]);

  // Keep a ref for selectedIndex so the keyboard handler always reads the
  // latest value without needing to re-register on every selection change.
  // (Avoids a stale-closure race where Enter fires before useEffect
  // re-attaches the handler with the updated selectedIndex.)
  const selectedIndexRef = useRef(selectedIndex);
  selectedIndexRef.current = selectedIndex;

  // Update keyboard navigation to use flatTaskList
  useEffect(() => {
    const handleKeyDown = (e) => {
      if (activeTab.value !== "tasks") return;
      if (isInputFocused()) return;

      const len = flatTaskList.length;
      if (len === 0) return;

      if (e.key === "j" || e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        setSelectedIndex(prev => {
          if (prev === -1) return 0;
          return (prev + 1) % len;
        });
      } else if (e.key === "k" || e.key === "ArrowUp") {
        e.preventDefault();
        e.stopPropagation();
        setSelectedIndex(prev => {
          if (prev === -1) return len - 1;
          return (prev - 1 + len) % len;
        });
      } else if (e.key === "Enter") {
        const idx = selectedIndexRef.current;
        if (idx >= 0 && idx < len) {
          e.preventDefault();
          e.stopPropagation();
          seedTaskCache(flatTaskList[idx].id, flatTaskList[idx]);
          openPanel("task", flatTaskList[idx].id);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        setSelectedIndex(-1);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [flatTaskList]);

  return (
    <div class={`panel${activeTab.value === "tasks" ? " active" : ""}`}>
      <div class="task-filters">
        <PillSelect
          label="Project"
          value={teamFilter}
          options={[
            { value: "current", label: prettyName(team) },
            { value: "all", label: "All" },
            ...allTeams.filter(t => t.name !== team).map(t => ({
              value: t.name,
              label: prettyName(t.name)
            }))
          ]}
          onChange={handleTeamFilterChange}
        />
        <FilterBar
          filters={filters}
          onFiltersChange={setFilters}
          fieldConfig={fieldConfig}
        />
        <button
          class={`merge-sort-toggle${mergeSort ? " active" : ""}`}
          onClick={() => setMergeSort(v => !v)}
          title={mergeSort ? "Showing merge-optimal order" : "Sort by suggested merge order"}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor"
               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M3 2v10M3 12l-2-2M3 12l2-2M8 3h5M8 7h3M8 11h1" />
          </svg>
          Merge order
        </button>
        <button
          class={`merge-sort-toggle${reviewerAI ? " active" : ""}`}
          onClick={toggleReviewer}
          title={reviewerAI ? "AI Review is ON — AI reviews in_approval tasks" : "Enable AI reviewer"}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor"
               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="7" cy="7" r="5.5" />
            <path d="M5 7l1.5 1.5L9 5.5" />
          </svg>
          AI Review
        </button>
        <button
          class={`merge-sort-toggle${autoMerge ? " active" : ""}${reviewerAI ? " locked" : ""}`}
          onClick={toggleAutoMerge}
          title={reviewerAI ? "Auto Merge is locked ON while AI Review is enabled" : autoMerge ? "Auto Merge is ON — approved tasks merge automatically" : "Enable auto merge for approved tasks"}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor"
               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 2v10M4 9l3 3 3-3" />
          </svg>
          Auto Merge
        </button>
        <button
          class={`merge-sort-toggle${taskFreezeOn ? " active" : ""}`}
          onClick={toggleTaskFreeze}
          title={taskFreezeOn ? "Task freeze is ON — manager will not create new tasks" : "Freeze task creation"}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor"
               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="3" width="8" height="8" rx="1.5" />
          </svg>
          Task freeze
        </button>
        <button
          class={`merge-sort-toggle${maxTasksEnabled ? " active" : ""}`}
          onClick={toggleMaxTasks}
          title={maxTasksEnabled ? `Max tasks ON — in-progress: ${maxTasksInProgress}, queued: ${maxTasksQueued}` : "Enable max tasks limit"}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor"
               strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <path d="M7 2v10M4 5l3-3 3 3" />
          </svg>
          Max tasks
        </button>
        {maxTasksEnabled && (
          <>
            <span style={{ marginLeft: "6px", fontSize: "11px", color: "var(--text-secondary)" }}>WIP</span>
            <input
              type="number"
              min="1"
              max="100"
              value={maxTasksInProgress}
              onInput={(e) => updateMaxTasksInProgress(e.target.value)}
              onFocus={() => { isInputFocused.value = true; }}
              onBlur={() => { isInputFocused.value = false; }}
              class="max-tasks-input"
              style={{ width: "40px", marginLeft: "2px", padding: "2px 4px", fontSize: "12px", borderRadius: "4px", border: "1px solid var(--border)", background: "var(--bg-secondary)", color: "var(--text-primary)", textAlign: "center" }}
              title="Max in-progress tasks"
            />
            <span style={{ marginLeft: "6px", fontSize: "11px", color: "var(--text-secondary)" }}>Queue</span>
            <input
              type="number"
              min="1"
              max="100"
              value={maxTasksQueued}
              onInput={(e) => updateMaxTasksQueued(e.target.value)}
              onFocus={() => { isInputFocused.value = true; }}
              onBlur={() => { isInputFocused.value = false; }}
              class="max-tasks-input"
              style={{ width: "40px", marginLeft: "2px", padding: "2px 4px", fontSize: "12px", borderRadius: "4px", border: "1px solid var(--border)", background: "var(--bg-secondary)", color: "var(--text-primary)", textAlign: "center" }}
              title="Max queued tasks"
            />
          </>
        )}
        <div style={{ flex: 1 }} />
        <div class={searchExpanded ? "filter-search-wrap expanded" : "filter-search-wrap"}>
          {!searchExpanded ? (
            <button
              class="filter-search-icon-btn"
              onClick={handleSearchExpand}
              title="Search tasks"
            >
              {searchIcon}
            </button>
          ) : (
            <>
              {searchIcon}
              <input
                ref={searchInputRef}
                type="text"
                class="filter-search"
                placeholder="Search tasks..."
                value={searchQuery}
                onInput={onSearchInput}
                onBlur={handleSearchBlur}
                onKeyDown={handleSearchKeyDown}
              />
            </>
          )}
        </div>
      </div>
      <div>
        {!allTasks.length ? (
          <div class="panel-empty-state">
            <svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="3" width="18" height="18" rx="2" />
              <path d="M9 12l2 2 4-4" />
            </svg>
            <span class="panel-empty-title">No tasks yet</span>
            <span class="panel-empty-sub">Tasks will appear here once agents start working</span>
          </div>
        ) : !filtered.length ? (
          <div class="panel-empty-state">
            <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round">
              <circle cx="11" cy="11" r="8" />
              <line x1="21" y1="21" x2="16.65" y2="16.65" />
            </svg>
            <span class="panel-empty-title">No matching tasks</span>
            <span class="panel-empty-sub">Try adjusting your filters or search query</span>
          </div>
        ) : isGroupedView ? (
          <div class="task-list-grouped">
            {Object.entries(groupedTasks).map(([teamName, teamTasks]) => {
              const isCollapsed = collapsedTeams.has(teamName);
              return (
                <div key={teamName} class="task-team-group">
                  <div class="task-team-header" onClick={() => toggleTeamGroup(teamName)}>
                    <span class="task-team-toggle">{isCollapsed ? "\u25B6" : "\u25BC"}</span>
                    <span class="task-team-name">{prettyName(teamName)}</span>
                    <span class="task-team-count">{teamTasks.length}</span>
                  </div>
                  {!isCollapsed && (
                    <div class="task-list">
                      {teamTasks.map((t) => {
                        const globalIdx = flatTaskList.findIndex(ft => ft.id === t.id);
                        return (
                          <div
                            key={t.id}
                            class={`task-row${globalIdx === selectedIndex ? " selected" : ""}`}
                            onClick={() => { seedTaskCache(t.id, t); openPanel("task", t.id); }}
                          >
                            <div class="task-summary">
                              <span class="task-id copyable">{taskIdStr(t.id, t.prefix, t.seq)}<CopyBtn text={taskIdStr(t.id, t.prefix, t.seq)} /></span>
                              <span class="task-title">{t.title}</span>
                              <span><span class={"badge badge-" + t.status}>{fmtStatus(t.status)}</span></span>
                              <span class="task-assignee">{t.assignee ? cap(t.assignee) : "\u2014"}</span>
                              <span class="task-priority">{cap(t.priority)}</span>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        ) : (
          <div class="task-list">
            {filtered.map((t, idx) => (
              <div
                key={t.id}
                class={`task-row${idx === selectedIndex ? " selected" : ""}`}
                onClick={() => { seedTaskCache(t.id, t); openPanel("task", t.id); }}
              >
                <div class="task-summary">
                  <span class="task-id copyable">{taskIdStr(t.id, t.prefix, t.seq)}<CopyBtn text={taskIdStr(t.id, t.prefix, t.seq)} /></span>
                  <span class="task-title">{t.title}</span>
                  <span><span class={"badge badge-" + t.status}>{fmtStatus(t.status)}</span></span>
                  <span class="task-assignee">{t.assignee ? cap(t.assignee) : "\u2014"}</span>
                  <span class="task-priority">{cap(t.priority)}</span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
