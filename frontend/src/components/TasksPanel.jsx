import { useState, useEffect, useMemo, useCallback, useRef } from "preact/hooks";
import { currentTeam, tasks, activeTab, taskPanelId } from "../state.js";
import { cap, esc, fmtStatus, taskIdStr } from "../utils.js";
import { playTaskSound } from "../audio.js";

export function TasksPanel() {
  const team = currentTeam.value;
  const allTasks = tasks.value;

  const [filterStatus, setFilterStatus] = useState("");
  const [filterAssignee, setFilterAssignee] = useState("");
  const [filterPriority, setFilterPriority] = useState("");
  const [filterRepo, setFilterRepo] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const searchTimerRef = useRef(null);
  const prevStatusRef = useRef({});

  // Restore filters from session storage
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem("taskFilters");
      if (!raw) return;
      const f = JSON.parse(raw);
      if (f.search) setSearchQuery(f.search);
      if (f.status) setFilterStatus(f.status);
      if (f.assignee) setFilterAssignee(f.assignee);
      if (f.priority) setFilterPriority(f.priority);
      if (f.repo) setFilterRepo(f.repo);
    } catch (e) { }
  }, []);

  // Save filters
  useEffect(() => {
    try {
      sessionStorage.setItem("taskFilters", JSON.stringify({
        search: searchQuery, status: filterStatus,
        assignee: filterAssignee, priority: filterPriority, repo: filterRepo,
      }));
    } catch (e) { }
  }, [searchQuery, filterStatus, filterAssignee, filterPriority, filterRepo]);

  // Task status change sound
  useEffect(() => {
    let soundNeeded = false;
    for (const t of allTasks) {
      const prev = prevStatusRef.current[t.id];
      if (prev && prev !== t.status && (t.status === "done" || t.status === "in_review")) {
        soundNeeded = true;
      }
      prevStatusRef.current[t.id] = t.status;
    }
    if (soundNeeded) playTaskSound();
  }, [allTasks]);

  // Dynamic filter options
  const { assignees, repos } = useMemo(() => {
    const assigneeSet = new Set();
    const repoSet = new Set();
    for (const t of allTasks) {
      if (t.assignee) assigneeSet.add(t.assignee);
      if (t.repo) {
        const taskRepos = Array.isArray(t.repo) ? t.repo : [t.repo];
        taskRepos.forEach(r => { if (r) repoSet.add(r); });
      }
    }
    return { assignees: [...assigneeSet].sort(), repos: [...repoSet].sort() };
  }, [allTasks]);

  // Filter + sort
  const filtered = useMemo(() => {
    let list = allTasks;
    if (filterStatus) list = list.filter(t => t.status === filterStatus);
    if (filterPriority) list = list.filter(t => t.priority === filterPriority);
    if (filterAssignee) list = list.filter(t => t.assignee === filterAssignee);
    if (filterRepo) list = list.filter(t => {
      const r = Array.isArray(t.repo) ? t.repo : [t.repo];
      return r.includes(filterRepo);
    });
    const sq = searchQuery.toLowerCase().trim();
    if (sq) list = list.filter(t =>
      (t.title || "").toLowerCase().includes(sq) ||
      (t.description || "").toLowerCase().includes(sq)
    );
    return [...list].sort((a, b) => b.id - a.id);
  }, [allTasks, filterStatus, filterPriority, filterAssignee, filterRepo, searchQuery]);

  const onSearchInput = useCallback((e) => {
    const val = e.target.value;
    clearTimeout(searchTimerRef.current);
    searchTimerRef.current = setTimeout(() => setSearchQuery(val), 300);
  }, []);

  const searchIcon = (
    <svg class="filter-search-icon" width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
    </svg>
  );

  return (
    <div class="panel" style={{ display: activeTab.value === "tasks" ? "" : "none" }}>
      <div class="task-filters">
        <div class="filter-search-wrap">
          {searchIcon}
          <input
            type="text"
            class="filter-search"
            placeholder="Search tasks..."
            value={searchQuery}
            onInput={onSearchInput}
          />
        </div>
        <span class="filter-label">Status</span>
        <select value={filterStatus} onChange={e => setFilterStatus(e.target.value)}>
          <option value="">All</option>
          <option value="todo">Todo</option>
          <option value="in_progress">In Progress</option>
          <option value="in_review">In Review</option>
          <option value="in_approval">In Approval</option>
          <option value="done">Done</option>
          <option value="rejected">Rejected</option>
        </select>
        <span class="filter-label">Assignee</span>
        <select value={filterAssignee} onChange={e => setFilterAssignee(e.target.value)}>
          <option value="">All</option>
          {assignees.map(n => <option key={n} value={n}>{cap(n)}</option>)}
        </select>
        <span class="filter-label">Priority</span>
        <select value={filterPriority} onChange={e => setFilterPriority(e.target.value)}>
          <option value="">All</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
          <option value="critical">Critical</option>
        </select>
        <span class="filter-label">Repo</span>
        <select value={filterRepo} onChange={e => setFilterRepo(e.target.value)}>
          <option value="">All</option>
          {repos.map(r => <option key={r} value={r}>{r}</option>)}
        </select>
      </div>
      <div>
        {!allTasks.length ? (
          <p style={{ color: "var(--text-secondary)" }}>No tasks yet.</p>
        ) : !filtered.length ? (
          <p style={{ color: "var(--text-secondary)" }}>No tasks match filters.</p>
        ) : (
          <div class="task-list">
            {filtered.map(t => (
              <div
                key={t.id}
                class="task-row"
                onClick={() => { taskPanelId.value = t.id; }}
              >
                <div class="task-summary">
                  <span class="task-id">{taskIdStr(t.id)}</span>
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
