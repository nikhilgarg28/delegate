import { useState, useEffect, useMemo, useCallback, useRef } from "preact/hooks";
import { currentTeam, tasks, activeTab, openPanel, taskTeamFilter, teams, getWorkflowStages, isInputFocused } from "../state.js";
import { cap, fmtStatus, taskIdStr } from "../utils.js";
import { playTaskSound, playApprovalSound } from "../audio.js";
import { FilterBar, applyFilters } from "./FilterBar.jsx";
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
  const allTeams = teams.value;

  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [searchQuery, setSearchQuery] = useState("");
  const [searchExpanded, setSearchExpanded] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState(-1);
  const [collapsedTeams, setCollapsedTeams] = useState(new Set());
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
    } catch (e) { }
  }, []);

  // Save filters to session storage
  useEffect(() => {
    try {
      sessionStorage.setItem("taskFilters2", JSON.stringify({
        filters, search: searchQuery,
      }));
    } catch (e) { }
  }, [filters, searchQuery]);

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
    return [...list].sort((a, b) => b.id - a.id);
  }, [allTasks, filters, searchQuery]);

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
          label="Team"
          value={teamFilter}
          options={[
            { value: "current", label: cap(team) },
            { value: "all", label: "All teams" },
            ...allTeams.filter(t => t.name !== team).map(t => ({
              value: t.name,
              label: cap(t.name)
            }))
          ]}
          onChange={handleTeamFilterChange}
        />
        <FilterBar
          filters={filters}
          onFiltersChange={setFilters}
          fieldConfig={fieldConfig}
        />
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
          <p style={{ color: "var(--text-secondary)" }}>No tasks yet.</p>
        ) : !filtered.length ? (
          <p style={{ color: "var(--text-secondary)" }}>No tasks match filters.</p>
        ) : isGroupedView ? (
          <div class="task-list-grouped">
            {Object.entries(groupedTasks).map(([teamName, teamTasks]) => {
              const isCollapsed = collapsedTeams.has(teamName);
              return (
                <div key={teamName} class="task-team-group">
                  <div class="task-team-header" onClick={() => toggleTeamGroup(teamName)}>
                    <span class="task-team-toggle">{isCollapsed ? "\u25B6" : "\u25BC"}</span>
                    <span class="task-team-name">{cap(teamName)}</span>
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
                            onClick={() => { openPanel("task", t.id); }}
                          >
                            <div class="task-summary">
                              <span class="task-id copyable">{taskIdStr(t.id)}<CopyBtn text={taskIdStr(t.id)} /></span>
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
                onClick={() => { openPanel("task", t.id); }}
              >
                <div class="task-summary">
                  <span class="task-id copyable">{taskIdStr(t.id)}<CopyBtn text={taskIdStr(t.id)} /></span>
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
