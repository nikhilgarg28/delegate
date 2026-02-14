import { useState, useRef, useEffect, useCallback } from "preact/hooks";
import { currentTeam, teams, tasks, agents, navigate, activeTab, taskTeamFilter } from "../state.js";
import { cap } from "../utils.js";

/**
 * Cmd+K team quick-switcher modal.
 * Searchable team list with fuzzy filtering.
 */
export function TeamSwitcher({ open, onClose }) {
  const [query, setQuery] = useState("");
  const [selectedIndex, setSelectedIndex] = useState(0);
  const inputRef = useRef();
  const listRef = useRef();

  const teamList = teams.value;
  const allTasks = tasks.value;
  const allAgents = agents.value;

  // Filter teams by search query
  // For current team, use live data (more accurate). For other teams, use /teams snapshot.
  const filteredTeams = teamList
    .filter(t => {
      const name = typeof t === "object" ? t.name : t;
      return name.toLowerCase().includes(query.toLowerCase());
    })
    .map(t => {
      const teamObj = typeof t === "object" ? t : { name: t };
      const name = teamObj.name;
      const isCurrent = name === currentTeam.value;
      return {
        name,
        agentCount: isCurrent ? allAgents.length : (teamObj.agent_count || 0),
        taskCount: isCurrent
          ? allTasks.filter(task => ["todo", "in_progress", "in_review"].includes(task.status)).length
          : (teamObj.task_count || 0),
        humanCount: teamObj.human_count || 0,
        isCurrent,
      };
    });

  // Focus input when modal opens
  useEffect(() => {
    if (open && inputRef.current) {
      inputRef.current.focus();
      setQuery("");
      setSelectedIndex(0);
    }
  }, [open]);

  // Close on Escape or outside click
  useEffect(() => {
    if (!open) return;
    const handleKeyDown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        setSelectedIndex(i => Math.min(i + 1, filteredTeams.length - 1));
      } else if (e.key === "ArrowUp") {
        e.preventDefault();
        setSelectedIndex(i => Math.max(i - 1, 0));
      } else if (e.key === "Enter") {
        e.preventDefault();
        if (filteredTeams[selectedIndex]) {
          selectTeam(filteredTeams[selectedIndex].name);
        }
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, selectedIndex, filteredTeams]);

  // Scroll selected item into view
  useEffect(() => {
    if (!open || !listRef.current) return;
    const selected = listRef.current.querySelector(".team-switcher-item.selected");
    if (selected) {
      selected.scrollIntoView({ block: "nearest" });
    }
  }, [selectedIndex, open]);

  const selectTeam = useCallback((teamName) => {
    const current = currentTeam.value;
    if (teamName !== current) {
      const currentTab = activeTab.value || "chat";
      navigate(teamName, currentTab);
      if (currentTab === "tasks") {
        taskTeamFilter.value = teamName;
      }
    }
    onClose();
  }, [onClose]);

  if (!open) return null;

  return (
    <div class="team-switcher-backdrop" onClick={onClose}>
      <div class="team-switcher-modal" onClick={(e) => e.stopPropagation()}>
        <div class="team-switcher-header">
          <svg class="team-switcher-icon" width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
            <circle cx="6" cy="6" r="4.5" /><line x1="9.5" y1="9.5" x2="13" y2="13" />
          </svg>
          <input
            ref={inputRef}
            type="text"
            class="team-switcher-input"
            placeholder="Switch team..."
            value={query}
            onInput={(e) => {
              setQuery(e.target.value);
              setSelectedIndex(0);
            }}
          />
        </div>
        <div class="team-switcher-list" ref={listRef}>
          {filteredTeams.length === 0 ? (
            <div class="team-switcher-empty">No teams found</div>
          ) : (
            filteredTeams.map((team, idx) => (
              <div
                key={team.name}
                class={`team-switcher-item${idx === selectedIndex ? " selected" : ""}${team.isCurrent ? " current" : ""}`}
                onClick={() => selectTeam(team.name)}
              >
                <div class="team-switcher-item-name">{cap(team.name)}</div>
                <div class="team-switcher-item-meta">
                  {team.agentCount} {team.agentCount === 1 ? "agent" : "agents"}
                  {team.humanCount > 0 && (
                    <>
                      {" • "}
                      {team.humanCount} {team.humanCount === 1 ? "human" : "humans"}
                    </>
                  )}
                  {" • "}
                  {team.taskCount} {team.taskCount === 1 ? "task" : "tasks"}
                </div>
              </div>
            ))
          )}
        </div>
        <div class="team-switcher-footer">
          <span class="team-switcher-hint">↑↓ Navigate</span>
          <span class="team-switcher-hint">Enter Select</span>
          <span class="team-switcher-hint">Esc Close</span>
        </div>
      </div>
    </div>
  );
}
