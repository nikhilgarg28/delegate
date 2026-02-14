import { useMemo } from "preact/hooks";
import { diff2HtmlParse } from "../utils.js";

/**
 * Renders /diff command output - inline task diff view.
 * This is a simplified read-only diff renderer (no commenting).
 * @param {Object} props
 * @param {Object} props.result - Diff data from API
 */
export function DiffCommandBlock({ result }) {
  if (!result) {
    return (
      <div class="diff-command-block">
        <div class="diff-command-header">Loading diff...</div>
      </div>
    );
  }

  const { task_id, branch, diff } = result;

  // Flatten diff dict (repo -> unified diff string) into a single string
  const diffText = useMemo(() => {
    if (!diff || typeof diff !== 'object') return '';
    return Object.values(diff).join('\n');
  }, [diff]);

  // Parse the unified diff
  const files = useMemo(() => {
    if (!diffText) return [];
    try {
      return diff2HtmlParse(diffText);
    } catch (e) {
      console.error('Failed to parse diff:', e);
      return [];
    }
  }, [diffText]);

  if (!files.length) {
    return (
      <div class="diff-command-block">
        <div class="diff-command-header">
          Diff for T{String(task_id).padStart(4, '0')}
          {branch && <span class="diff-command-branch">{branch}</span>}
        </div>
        <div class="diff-command-empty">No changes</div>
      </div>
    );
  }

  return (
    <div class="diff-command-block">
      <div class="diff-command-header">
        Diff for T{String(task_id).padStart(4, '0')}
        {branch && <span class="diff-command-branch">{branch}</span>}
      </div>
      <div class="diff-command-body">
        {files.map((file, idx) => {
          const fileName = file.newName === '/dev/null' ? file.oldName : file.newName || file.oldName || 'unknown';
          const isDeleted = file.newName === '/dev/null';
          const isAdded = file.oldName === '/dev/null';

          return (
            <div key={idx} class="diff-command-file">
              <div class="diff-command-file-header">
                <span class="diff-command-file-name">{fileName}</span>
                {isDeleted && <span class="diff-command-file-badge deleted">deleted</span>}
                {isAdded && <span class="diff-command-file-badge added">added</span>}
                <span class="diff-command-file-stats">
                  <span class="diff-command-stats-add">+{file.addedLines}</span>
                  <span class="diff-command-stats-del">-{file.deletedLines}</span>
                </span>
              </div>
              <div class="diff-command-file-body">
                <table class="diff-command-table">
                  <tbody>
                    {file.blocks.map((block, blockIdx) => (
                      block.lines.map((line, lineIdx) => {
                        const lineClass =
                          line.type === 'insert' ? 'diff-line-add' :
                          line.type === 'delete' ? 'diff-line-del' :
                          line.type === 'context' ? 'diff-line-ctx' :
                          '';

                        // For hunk headers (@@)
                        if (line.type === 'd2h-info') {
                          return (
                            <tr key={`${blockIdx}-${lineIdx}`} class="diff-line-hunk">
                              <td class="diff-gutter"></td>
                              <td class="diff-gutter"></td>
                              <td class="diff-code" colSpan="1">
                                <pre class="diff-code-pre">{line.content}</pre>
                              </td>
                            </tr>
                          );
                        }

                        return (
                          <tr key={`${blockIdx}-${lineIdx}`} class={`diff-line ${lineClass}`}>
                            <td class="diff-gutter diff-gutter-old">{line.oldNumber || ''}</td>
                            <td class="diff-gutter diff-gutter-new">{line.newNumber || ''}</td>
                            <td class="diff-code">
                              <pre class="diff-code-pre">{line.content}</pre>
                            </td>
                          </tr>
                        );
                      })
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
