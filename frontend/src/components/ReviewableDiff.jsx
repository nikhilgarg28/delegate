/**
 * ReviewableDiff — A custom Preact diff renderer with inline commenting.
 *
 * Parses unified diff text via diff2html's `parse()` and renders a
 * fully interactive diff view where the reviewer can:
 *   - Click a "+" gutter button on any line to leave an inline comment
 *   - See existing comments rendered inline below the relevant line
 *   - See previous-attempt comments behind a toggle (dimmed)
 *
 * Component tree:
 *   ReviewableDiff
 *     └─ DiffFile (per file)
 *          └─ DiffBlock (per hunk / block)
 *               ├─ DiffLine (per line — with hover "+" button)
 *               └─ InlineComment (rendered between lines where comments exist)
 */

import { useState, useRef, useCallback, useMemo } from "preact/hooks";
import { diff2HtmlParse } from "../utils.js";
import * as api from "../api.js";
import { currentTeam } from "../state.js";
import { showToast } from "../toast.js";

// ── Helpers ──

/** Build a lookup key for comments: "file::line" */
function commentKey(file, line) {
  return `${file}::${line || "file"}`;
}

/** Group comments by file+line for O(1) lookup */
function indexComments(comments) {
  const map = {};
  for (const c of comments) {
    const key = commentKey(c.file, c.line);
    if (!map[key]) map[key] = [];
    map[key].push(c);
  }
  return map;
}

/** Get the display file name from a diff2html file object */
function fileName(f) {
  return (f.newName === "/dev/null" ? f.oldName : f.newName) || f.oldName || "unknown";
}

// ── InlineCommentForm ──

function InlineCommentForm({ file, line, taskId, onSaved, onCancel }) {
  const [body, setBody] = useState("");
  const [saving, setSaving] = useState(false);
  const ref = useRef();

  const handleSave = useCallback(async () => {
    if (!body.trim()) return;
    setSaving(true);
    try {
      const comment = await api.postReviewComment(currentTeam.value, taskId, {
        file, line, body: body.trim(),
      });
      setBody("");
      if (onSaved) onSaved(comment);
    } catch (e) {
      showToast("Failed to save comment", "error");
    } finally {
      setSaving(false);
    }
  }, [body, file, line, taskId, onSaved]);

  // Focus on mount
  const setRef = useCallback((el) => {
    ref.current = el;
    if (el) setTimeout(() => el.focus(), 50);
  }, []);

  return (
    <div class="rc-comment-form">
      <textarea
        ref={setRef}
        class="rc-comment-textarea"
        placeholder="Leave a comment..."
        value={body}
        onInput={(e) => setBody(e.target.value)}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleSave();
          if (e.key === "Escape") onCancel && onCancel();
        }}
        rows="2"
      />
      <div class="rc-comment-form-actions">
        <button class="rc-btn rc-btn-cancel" onClick={onCancel} disabled={saving}>Cancel</button>
        <button class="rc-btn rc-btn-save" onClick={handleSave} disabled={saving || !body.trim()}>
          {saving ? "Saving..." : "Comment"}
        </button>
      </div>
    </div>
  );
}

// ── InlineComment display ──

function InlineComment({ comment, dimmed }) {
  return (
    <div class={"rc-comment" + (dimmed ? " rc-comment-dimmed" : "")}>
      <div class="rc-comment-header">
        <span class="rc-comment-author">{comment.author}</span>
        {dimmed && comment.attempt && (
          <span class="rc-comment-attempt">Attempt {comment.attempt}</span>
        )}
      </div>
      <div class="rc-comment-body">{comment.body}</div>
    </div>
  );
}

// ── DiffLine ──

function DiffLine({ line, file, taskId, comments, oldComments, showOld, onCommentSaved }) {
  const [formOpen, setFormOpen] = useState(false);
  // Use the new line number for additions/context, old for deletions
  const lineNum = line.newNumber || line.oldNumber;
  const type = line.type; // "insert", "delete", "context"

  const lineClass =
    type === "insert" ? "rc-line-add" :
    type === "delete" ? "rc-line-del" :
    "rc-line-ctx";

  // Content: strip leading +/-/space from the raw content
  const content = line.content || "";

  return (
    <>
      <tr class={"rc-line " + lineClass}>
        <td class="rc-gutter rc-gutter-old">{line.oldNumber || ""}</td>
        <td class="rc-gutter rc-gutter-new">{line.newNumber || ""}</td>
        <td class="rc-gutter rc-gutter-action">
          {lineNum && !formOpen && (
            <button
              class="rc-add-comment-btn"
              title="Add comment"
              onClick={(e) => { e.stopPropagation(); setFormOpen(true); }}
            >+</button>
          )}
        </td>
        <td class="rc-code">
          <pre class="rc-code-pre">{content}</pre>
        </td>
      </tr>
      {/* Existing comments on this line (current attempt) */}
      {comments && comments.map((c, i) => (
        <tr key={"c-" + c.id} class="rc-comment-row">
          <td colSpan="4" class="rc-comment-cell">
            <InlineComment comment={c} />
          </td>
        </tr>
      ))}
      {/* Old attempt comments (dimmed) */}
      {showOld && oldComments && oldComments.map((c, i) => (
        <tr key={"oc-" + c.id} class="rc-comment-row">
          <td colSpan="4" class="rc-comment-cell">
            <InlineComment comment={c} dimmed />
          </td>
        </tr>
      ))}
      {/* Comment form */}
      {formOpen && (
        <tr class="rc-comment-row">
          <td colSpan="4" class="rc-comment-cell">
            <InlineCommentForm
              file={file}
              line={lineNum}
              taskId={taskId}
              onSaved={(c) => { setFormOpen(false); onCommentSaved && onCommentSaved(c); }}
              onCancel={() => setFormOpen(false)}
            />
          </td>
        </tr>
      )}
    </>
  );
}

// ── DiffBlock (hunk) ──

function DiffBlock({ block, file, taskId, commentIndex, oldCommentIndex, showOld, onCommentSaved }) {
  const header = block.header || "";

  return (
    <>
      {header && (
        <tr class="rc-hunk-header">
          <td colSpan="4" class="rc-hunk-header-cell">{header}</td>
        </tr>
      )}
      {block.lines.map((line, i) => {
        const lineNum = line.newNumber || line.oldNumber;
        const key = commentKey(file, lineNum);
        return (
          <DiffLine
            key={i}
            line={line}
            file={file}
            taskId={taskId}
            comments={commentIndex[key]}
            oldComments={oldCommentIndex[key]}
            showOld={showOld}
            onCommentSaved={onCommentSaved}
          />
        );
      })}
    </>
  );
}

// ── DiffFile ──

function DiffFile({ diffFile, taskId, commentIndex, oldCommentIndex, showOld, onCommentSaved, defaultCollapsed }) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  const name = fileName(diffFile);
  const fileCommentKey = commentKey(name, null);
  const fileComments = commentIndex[fileCommentKey] || [];
  const oldFileComments = oldCommentIndex[fileCommentKey] || [];

  // Count comments on this file (current attempt only)
  let commentCount = fileComments.length;
  for (const block of diffFile.blocks) {
    for (const line of block.lines) {
      const ln = line.newNumber || line.oldNumber;
      const key = commentKey(name, ln);
      if (commentIndex[key]) commentCount += commentIndex[key].length;
    }
  }

  return (
    <div class="rc-file">
      <div class="rc-file-header" onClick={() => setCollapsed(!collapsed)}>
        <span class="rc-file-toggle">{collapsed ? "\u25B6" : "\u25BC"}</span>
        <span class="rc-file-name">{name}</span>
        <span class="rc-file-stats">
          <span class="rc-file-add">+{diffFile.addedLines}</span>
          <span class="rc-file-del">-{diffFile.deletedLines}</span>
        </span>
        {commentCount > 0 && (
          <span class="rc-file-comment-count">{commentCount} comment{commentCount !== 1 ? "s" : ""}</span>
        )}
      </div>
      {!collapsed && (
        <div class="rc-file-body">
          {/* File-level comments */}
          {fileComments.map((c) => (
            <div key={"fc-" + c.id} class="rc-file-level-comment">
              <InlineComment comment={c} />
            </div>
          ))}
          {showOld && oldFileComments.map((c) => (
            <div key={"ofc-" + c.id} class="rc-file-level-comment">
              <InlineComment comment={c} dimmed />
            </div>
          ))}
          <table class="rc-diff-table">
            <colgroup>
              <col style="width:40px" />
              <col style="width:40px" />
              <col style="width:24px" />
              <col />
            </colgroup>
            <tbody>
              {diffFile.blocks.map((block, i) => (
                <DiffBlock
                  key={i}
                  block={block}
                  file={name}
                  taskId={taskId}
                  commentIndex={commentIndex}
                  oldCommentIndex={oldCommentIndex}
                  showOld={showOld}
                  onCommentSaved={onCommentSaved}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ── Main: ReviewableDiff ──

export function ReviewableDiff({
  diffRaw,
  taskId,
  currentComments = [],
  oldComments = [],
  isReviewable = false,
}) {
  const [localComments, setLocalComments] = useState([]);
  const [showOld, setShowOld] = useState(false);

  // Parse the unified diff
  const files = useMemo(() => {
    if (!diffRaw) return [];
    return diff2HtmlParse(diffRaw);
  }, [diffRaw]);

  // Merge server comments + local (optimistic) ones
  const allCurrentComments = useMemo(
    () => [...currentComments, ...localComments],
    [currentComments, localComments],
  );

  const commentIndex = useMemo(() => indexComments(allCurrentComments), [allCurrentComments]);
  const oldCommentIndex = useMemo(() => indexComments(oldComments), [oldComments]);

  const handleCommentSaved = useCallback((comment) => {
    setLocalComments((prev) => [...prev, comment]);
  }, []);

  if (!files.length) {
    return <div class="diff-empty">No changes</div>;
  }

  return (
    <div class="rc-reviewable-diff">
      {/* Previous reviews toggle */}
      {oldComments.length > 0 && (
        <div class="rc-old-toggle">
          <label>
            <input
              type="checkbox"
              checked={showOld}
              onChange={(e) => setShowOld(e.target.checked)}
            />
            {" "}Show previous review comments ({oldComments.length})
          </label>
        </div>
      )}
      {/* Summary */}
      <div class="rc-diff-summary">
        {files.length} file{files.length !== 1 ? "s" : ""} changed
        {allCurrentComments.length > 0 && (
          <span class="rc-comment-badge">{allCurrentComments.length} comment{allCurrentComments.length !== 1 ? "s" : ""}</span>
        )}
      </div>
      {/* Files */}
      {files.map((f, i) => (
        <DiffFile
          key={i}
          diffFile={f}
          taskId={taskId}
          commentIndex={commentIndex}
          oldCommentIndex={oldCommentIndex}
          showOld={showOld}
          onCommentSaved={isReviewable ? handleCommentSaved : undefined}
          defaultCollapsed={false}
        />
      ))}
    </div>
  );
}
