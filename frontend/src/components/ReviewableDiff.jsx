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
import { diff2HtmlParse, cap } from "../utils.js";
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

  // Auto-grow textarea
  const handleInput = useCallback((e) => {
    setBody(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 300) + 'px';
  }, []);

  // Focus on mount and set initial height
  const setRef = useCallback((el) => {
    ref.current = el;
    if (el) {
      setTimeout(() => {
        el.focus();
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 300) + 'px';
      }, 50);
    }
  }, []);

  return (
    <div class="rc-comment-form">
      <textarea
        ref={setRef}
        class="rc-comment-textarea"
        placeholder="Leave a comment..."
        value={body}
        onInput={handleInput}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleSave();
          if (e.key === "Escape") onCancel && onCancel();
        }}
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

// ── InlineEditForm — edit an existing comment ──

function InlineEditForm({ comment, taskId, onSaved, onCancel }) {
  const [body, setBody] = useState(comment.body);
  const [saving, setSaving] = useState(false);
  const ref = useRef();

  const handleSave = useCallback(async () => {
    if (!body.trim()) return;
    setSaving(true);
    try {
      const updated = await api.updateReviewComment(currentTeam.value, taskId, comment.id, body.trim());
      if (onSaved) onSaved(updated);
    } catch (e) {
      showToast("Failed to update comment", "error");
    } finally {
      setSaving(false);
    }
  }, [body, taskId, comment.id, onSaved]);

  // Auto-grow textarea
  const handleInput = useCallback((e) => {
    setBody(e.target.value);
    e.target.style.height = 'auto';
    e.target.style.height = Math.min(e.target.scrollHeight, 300) + 'px';
  }, []);

  // Focus on mount and set initial height
  const setRef = useCallback((el) => {
    ref.current = el;
    if (el) {
      setTimeout(() => {
        el.focus();
        el.style.height = 'auto';
        el.style.height = Math.min(el.scrollHeight, 300) + 'px';
      }, 50);
    }
  }, []);

  return (
    <div class="rc-comment-form">
      <textarea
        ref={setRef}
        class="rc-comment-textarea"
        value={body}
        onInput={handleInput}
        onKeyDown={(e) => {
          e.stopPropagation();
          if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) handleSave();
          if (e.key === "Escape") onCancel && onCancel();
        }}
      />
      <div class="rc-comment-form-actions">
        <button class="rc-btn rc-btn-cancel" onClick={onCancel} disabled={saving}>Cancel</button>
        <button class="rc-btn rc-btn-save" onClick={handleSave} disabled={saving || !body.trim()}>
          {saving ? "Saving..." : "Save"}
        </button>
      </div>
    </div>
  );
}

// ── InlineComment display ──

function InlineComment({ comment, dimmed, onEdit, onDelete }) {
  return (
    <div class={"rc-comment" + (dimmed ? " rc-comment-dimmed" : "")}>
      <div class="rc-comment-header">
        <span class="rc-comment-author">{cap(comment.author)}</span>
        {dimmed && comment.attempt && (
          <span class="rc-comment-attempt">Attempt {comment.attempt}</span>
        )}
        {(onEdit || onDelete) && (
          <span class="rc-comment-actions">
            {onEdit && (
              <button class="rc-comment-action" title="Edit" onClick={onEdit}>
                <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M11.5 1.5l3 3L5 14H2v-3z" />
                </svg>
              </button>
            )}
            {onDelete && (
              <button class="rc-comment-action rc-comment-delete" title="Delete" onClick={onDelete}>
                <svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
                  <line x1="4" y1="4" x2="12" y2="12" /><line x1="12" y1="4" x2="4" y2="12" />
                </svg>
              </button>
            )}
          </span>
        )}
      </div>
      <div class="rc-comment-body">{comment.body}</div>
    </div>
  );
}

// ── DiffLine ──

function DiffLine({ line, file, taskId, comments, oldComments, showOld, onCommentSaved, onCommentEdit, onCommentDelete }) {
  const [formOpen, setFormOpen] = useState(false);
  const [editingId, setEditingId] = useState(null);
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
      {comments && comments.map((c) => (
        <tr key={"c-" + c.id} class="rc-comment-row">
          <td colSpan="4" class="rc-comment-cell">
            {editingId === c.id ? (
              <InlineEditForm
                comment={c}
                taskId={taskId}
                onSaved={(updated) => { setEditingId(null); onCommentEdit && onCommentEdit(updated); }}
                onCancel={() => setEditingId(null)}
              />
            ) : (
              <InlineComment
                comment={c}
                onEdit={onCommentEdit ? () => setEditingId(c.id) : undefined}
                onDelete={onCommentDelete ? () => onCommentDelete(c) : undefined}
              />
            )}
          </td>
        </tr>
      ))}
      {/* Old attempt comments (dimmed) */}
      {showOld && oldComments && oldComments.map((c) => (
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

// ── EditableComment — wraps InlineComment with edit state ──

function EditableComment({ comment, taskId, onEdit, onDelete }) {
  const [editing, setEditing] = useState(false);

  if (editing) {
    return (
      <InlineEditForm
        comment={comment}
        taskId={taskId}
        onSaved={(updated) => { setEditing(false); onEdit && onEdit(updated); }}
        onCancel={() => setEditing(false)}
      />
    );
  }

  return (
    <InlineComment
      comment={comment}
      onEdit={onEdit ? () => setEditing(true) : undefined}
      onDelete={onDelete ? () => onDelete(comment) : undefined}
    />
  );
}

// ── DiffBlock (hunk) ──

function DiffBlock({ block, file, taskId, commentIndex, oldCommentIndex, showOld, onCommentSaved, onCommentEdit, onCommentDelete }) {
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
            onCommentEdit={onCommentEdit}
            onCommentDelete={onCommentDelete}
          />
        );
      })}
    </>
  );
}

// ── DiffFile ──

function DiffFile({ diffFile, taskId, commentIndex, oldCommentIndex, showOld, onCommentSaved, onCommentEdit, onCommentDelete, defaultCollapsed }) {
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
              <EditableComment comment={c} taskId={taskId} onEdit={onCommentEdit} onDelete={onCommentDelete} />
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
                  onCommentEdit={onCommentEdit}
                  onCommentDelete={onCommentDelete}
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

  const handleCommentEdit = useCallback((updated) => {
    // Update in local comments if it exists there
    setLocalComments((prev) =>
      prev.map(c => c.id === updated.id ? { ...c, body: updated.body } : c)
    );
  }, []);

  const handleCommentDelete = useCallback(async (comment) => {
    try {
      await api.deleteReviewComment(currentTeam.value, taskId, comment.id);
      // Remove from local comments
      setLocalComments((prev) => prev.filter(c => c.id !== comment.id));
    } catch (e) {
      showToast("Failed to delete comment", "error");
    }
  }, [taskId]);

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
          onCommentEdit={isReviewable ? handleCommentEdit : undefined}
          onCommentDelete={isReviewable ? handleCommentDelete : undefined}
          defaultCollapsed={false}
        />
      ))}
    </div>
  );
}
