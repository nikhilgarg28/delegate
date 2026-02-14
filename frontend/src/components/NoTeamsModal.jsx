import { teams } from "../state.js";

export function NoTeamsModal() {
  // Only show when teams array is loaded AND empty
  // Need to distinguish "not yet fetched" from "fetched but empty"
  if (teams.value === null || teams.value.length > 0) return null;

  return (
    <div class="no-teams-backdrop">
      <div class="no-teams-modal">
        <div class="no-teams-header">
          <h2>No teams configured</h2>
        </div>
        <div class="no-teams-body">
          <p>Create a team to get started:</p>
          <div class="no-teams-commands">
            <code>delegate team create &lt;name&gt;</code>
            <code>delegate agent add &lt;team&gt; &lt;name&gt;</code>
          </div>
          <p class="no-teams-hint">The page will update automatically once a team is created.</p>
        </div>
      </div>
    </div>
  );
}
