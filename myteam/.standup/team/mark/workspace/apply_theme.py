"""Apply light/dark theme CSS variables to web.py.

This script:
1. Adds :root { } block with light-mode CSS variable definitions
2. Adds @media (prefers-color-scheme: dark) { :root { } } with dark overrides
3. Replaces all hardcoded color values in CSS with var() references
4. Replaces inline JS style colors with var() references
"""

import re

FILE = "/Users/nikhil/dev/standup/scripts/web.py"

with open(FILE, "r") as f:
    content = f.read()

# ============================================================
# Step 1: Define the :root and dark mode blocks
# ============================================================

ROOT_BLOCK = """\
  /* Theme variables — light mode defaults */
  :root {
    --bg-body: #ffffff;
    --bg-surface: #f8f8f8;
    --bg-sidebar: #f3f3f5;
    --bg-hover: rgba(0,0,0,0.03);
    --bg-active: rgba(0,0,0,0.06);
    --bg-input: #ffffff;
    --text-primary: #1a1a1a;
    --text-secondary: #6b6b6b;
    --text-muted: #999999;
    --text-faint: #bbbbbb;
    --text-heading: #111111;
    --border-default: rgba(0,0,0,0.08);
    --border-subtle: rgba(0,0,0,0.04);
    --border-input: rgba(0,0,0,0.15);
    --border-focus: rgba(0,0,0,0.3);
    --accent-blue: #60a5fa;
    --accent-green: #22c55e;
    --accent-green-glow: rgba(34,197,94,0.4);
    --btn-bg: #1a1a1a;
    --btn-text: #ffffff;
    --btn-hover: #333333;
    --scrollbar-thumb: rgba(0,0,0,0.12);
    --scrollbar-hover: rgba(0,0,0,0.2);
    --dot-offline: #cccccc;
    --diff-add-bg: rgba(34,197,94,0.08);
    --diff-add-text: #16a34a;
    --diff-del-bg: rgba(248,113,113,0.08);
    --diff-del-text: #dc2626;
    --diff-ctx-text: #888888;
    --diff-hunk-bg: rgba(96,165,250,0.06);
    --backdrop-bg: rgba(0,0,0,0.3);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg-body: #0a0a0b;
      --bg-surface: #111113;
      --bg-sidebar: #0d0d0f;
      --bg-hover: rgba(255,255,255,0.02);
      --bg-active: rgba(255,255,255,0.08);
      --bg-input: #111113;
      --text-primary: #ededed;
      --text-secondary: #a1a1a1;
      --text-muted: #555555;
      --text-faint: #444444;
      --text-heading: #fafafa;
      --border-default: rgba(255,255,255,0.08);
      --border-subtle: rgba(255,255,255,0.04);
      --border-input: rgba(255,255,255,0.1);
      --border-focus: rgba(255,255,255,0.25);
      --accent-blue: #60a5fa;
      --accent-green: #22c55e;
      --accent-green-glow: rgba(34,197,94,0.4);
      --btn-bg: #fafafa;
      --btn-text: #0a0a0b;
      --btn-hover: #d4d4d4;
      --scrollbar-thumb: rgba(255,255,255,0.1);
      --scrollbar-hover: rgba(255,255,255,0.18);
      --dot-offline: #333333;
      --diff-add-bg: rgba(34,197,94,0.08);
      --diff-add-text: #6ee7b7;
      --diff-del-bg: rgba(248,113,113,0.08);
      --diff-del-text: #fca5a5;
      --diff-ctx-text: #666666;
      --diff-hunk-bg: rgba(96,165,250,0.06);
      --backdrop-bg: rgba(0,0,0,0.5);
    }
  }"""

# Insert after the * { box-sizing } and html, body { height: 100%; } rules
# We'll insert right after the opening <style> + universal reset lines
content = content.replace(
    "  * { box-sizing: border-box; margin: 0; padding: 0; }\n  html, body { height: 100%; }",
    "  * { box-sizing: border-box; margin: 0; padding: 0; }\n  html, body { height: 100%; }\n" + ROOT_BLOCK
)

# ============================================================
# Step 2: Replace hardcoded colors with CSS variables
# ============================================================

# These are exact string replacements in CSS rules.
# Order matters — do longer/more specific matches first.

css_replacements = [
    # body
    ("body { font-family: 'Geist Sans', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0a0a0b; color: #ededed;",
     "body { font-family: 'Geist Sans', Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg-body); color: var(--text-primary);"),

    # header
    (".header { background: #111113; padding: 14px 24px; border-bottom: 1px solid rgba(255,255,255,0.08);",
     ".header { background: var(--bg-surface); padding: 14px 24px; border-bottom: 1px solid var(--border-default);"),

    # sidebar
    (".sidebar { width: 280px; min-width: 280px; height: 100vh; position: sticky; top: 0; background: #0d0d0f; border-right: 1px solid rgba(255,255,255,0.06);",
     ".sidebar { width: 280px; min-width: 280px; height: 100vh; position: sticky; top: 0; background: var(--bg-sidebar); border-right: 1px solid var(--border-subtle);"),

    (".sidebar-widget { padding: 16px; border-bottom: 1px solid rgba(255,255,255,0.04);",
     ".sidebar-widget { padding: 16px; border-bottom: 1px solid var(--border-subtle);"),

    (".sidebar-widget-header { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555;",
     ".sidebar-widget-header { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted);"),

    (".sidebar-stat-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: #a1a1a1;",
     ".sidebar-stat-row { display: flex; align-items: center; gap: 8px; font-size: 13px; color: var(--text-secondary);"),

    (".sidebar-stat-row .stat-value { color: #ededed;",
     ".sidebar-stat-row .stat-value { color: var(--text-primary);"),

    (".sidebar-agent-dot.dot-offline { background: #333;",
     ".sidebar-agent-dot.dot-offline { background: var(--dot-offline);"),

    (".sidebar-agent-name { color: #ededed;",
     ".sidebar-agent-name { color: var(--text-primary);"),

    (".sidebar-agent-activity { color: #555;",
     ".sidebar-agent-activity { color: var(--text-muted);"),

    (".sidebar-agent-cost { color: #444;",
     ".sidebar-agent-cost { color: var(--text-faint);"),

    (".sidebar-task-id { color: #555;",
     ".sidebar-task-id { color: var(--text-muted);"),

    (".sidebar-task-title { color: #a1a1a1;",
     ".sidebar-task-title { color: var(--text-secondary);"),

    (".sidebar-task-assignee { color: #444;",
     ".sidebar-task-assignee { color: var(--text-faint);"),

    (".sidebar-see-all { color: #60a5fa;",
     ".sidebar-see-all { color: var(--accent-blue);"),

    # header h1
    (".header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; color: #fafafa;",
     ".header h1 { font-size: 16px; font-weight: 600; letter-spacing: -0.02em; color: var(--text-heading);"),

    # tabs
    (".tab { padding: 7px 14px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: #666;",
     ".tab { padding: 7px 14px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: var(--text-muted);"),

    (".tab:hover { color: #999; background: rgba(255,255,255,0.04);",
     ".tab:hover { color: var(--text-secondary); background: var(--border-subtle);"),

    (".tab.active { background: rgba(255,255,255,0.08); color: #fafafa;",
     ".tab.active { background: var(--bg-active); color: var(--text-heading);"),

    # task rows
    (".task-row:hover { background: rgba(255,255,255,0.02);",
     ".task-row:hover { background: var(--bg-hover);"),

    (".task-row.expanded { border-color: rgba(255,255,255,0.08); background: rgba(255,255,255,0.02);",
     ".task-row.expanded { border-color: var(--border-default); background: var(--bg-hover);"),

    (".task-id { color: #555;",
     ".task-id { color: var(--text-muted);"),

    (".task-title { color: #ededed;",
     ".task-title { color: var(--text-primary);"),

    (".task-assignee { color: #a1a1a1;",
     ".task-assignee { color: var(--text-secondary);"),

    (".task-priority { color: #a1a1a1;",
     ".task-priority { color: var(--text-secondary);"),

    # task detail
    (".task-detail-item { background: rgba(255,255,255,0.03);",
     ".task-detail-item { background: var(--bg-hover);"),

    (".task-detail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555;",
     ".task-detail-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted);"),

    (".task-detail-value { font-size: 13px; color: #ededed;",
     ".task-detail-value { font-size: 13px; color: var(--text-primary);"),

    (".task-desc { color: #a1a1a1; font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; padding: 10px 14px; background: rgba(255,255,255,0.02);",
     ".task-desc { color: var(--text-secondary); font-size: 13px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; padding: 10px 14px; background: var(--bg-hover);"),

    (".task-dates { display: flex; gap: 24px; margin-top: 10px; font-size: 11px; color: #555;",
     ".task-dates { display: flex; gap: 24px; margin-top: 10px; font-size: 11px; color: var(--text-muted);"),

    # task VCS
    (".task-branch { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; background: rgba(255,255,255,0.06); color: #a1a1a1;",
     ".task-branch { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 12px; background: var(--bg-active); color: var(--text-secondary);"),

    # badge-done
    (".badge-done { background: rgba(255,255,255,0.06); color: #555;",
     ".badge-done { background: var(--bg-active); color: var(--text-muted);"),

    # chat
    (".chat-log { flex: 1; min-height: 0; overflow-y: auto; background: #111113; border: 1px solid rgba(255,255,255,0.06);",
     ".chat-log { flex: 1; min-height: 0; overflow-y: auto; background: var(--bg-surface); border: 1px solid var(--border-subtle);"),

    (".msg:hover { background: rgba(255,255,255,0.02);",
     ".msg:hover { background: var(--bg-hover);"),

    (".msg-sender { font-weight: 600; color: #ededed;",
     ".msg-sender { font-weight: 600; color: var(--text-primary);"),

    (".msg-recipient { color: #555;",
     ".msg-recipient { color: var(--text-muted);"),

    (".msg-time { color: #444;",
     ".msg-time { color: var(--text-faint);"),

    (".msg-content { color: #a1a1a1;",
     ".msg-content { color: var(--text-secondary);"),

    (".msg-event-line { flex: 1; height: 1px; background: rgba(255,255,255,0.06);",
     ".msg-event-line { flex: 1; height: 1px; background: var(--border-subtle);"),

    (".msg-event-text { color: #444;",
     ".msg-event-text { color: var(--text-faint);"),

    (".msg-event-time { color: #444;",
     ".msg-event-time { color: var(--text-faint);"),

    # chat input
    (".chat-input input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed;",
     ".chat-input input { flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary);"),

    (".chat-input input:focus { border-color: rgba(255,255,255,0.25);",
     ".chat-input input:focus { border-color: var(--border-focus);"),

    (".chat-input input::placeholder { color: #444;",
     ".chat-input input::placeholder { color: var(--text-faint);"),

    (".chat-input select { padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed;",
     ".chat-input select { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary);"),

    (".chat-input button { padding: 10px 20px; border-radius: 8px; border: none; background: #fafafa; color: #0a0a0b;",
     ".chat-input button { padding: 10px 20px; border-radius: 8px; border: none; background: var(--btn-bg); color: var(--btn-text);"),

    (".chat-input button:hover { background: #d4d4d4;",
     ".chat-input button:hover { background: var(--btn-hover);"),

    # agents
    (".agent-card { background: #111113; border: 1px solid rgba(255,255,255,0.06);",
     ".agent-card { background: var(--bg-surface); border: 1px solid var(--border-subtle);"),

    (".agent-card:hover { border-color: rgba(255,255,255,0.12);",
     ".agent-card:hover { border-color: var(--border-default);"),

    (".agent-name { font-weight: 600; min-width: 120px; color: #ededed;",
     ".agent-name { font-weight: 600; min-width: 120px; color: var(--text-primary);"),

    (".agent-status { font-size: 12px; color: #555;",
     ".agent-status { font-size: 12px; color: var(--text-muted);"),

    (".dot-idle { background: #333;",
     ".dot-idle { background: var(--dot-offline);"),

    (".agent-stat { background: rgba(255,255,255,0.03);",
     ".agent-stat { background: var(--bg-hover);"),

    (".agent-stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: #555;",
     ".agent-stat-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted);"),

    (".agent-stat-value { font-size: 14px; font-weight: 600; color: #ededed;",
     ".agent-stat-value { font-size: 14px; font-weight: 600; color: var(--text-primary);"),

    # filters
    (".chat-filters select, .task-filters select { padding: 6px 10px; border-radius: 6px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #ededed;",
     ".chat-filters select, .task-filters select { padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-primary);"),

    (".chat-filters select:focus, .task-filters select:focus { border-color: rgba(255,255,255,0.25);",
     ".chat-filters select:focus, .task-filters select:focus { border-color: var(--border-focus);"),

    (".chat-filters label, .task-filters label { display: flex; align-items: center; gap: 6px; color: #666;",
     ".chat-filters label, .task-filters label { display: flex; align-items: center; gap: 6px; color: var(--text-muted);"),

    (".chat-filters label:hover, .task-filters label:hover { color: #999;",
     ".chat-filters label:hover, .task-filters label:hover { color: var(--text-secondary);"),

    (".chat-filters input[type=\"checkbox\"], .task-filters input[type=\"checkbox\"] { appearance: none; width: 14px; height: 14px; border: 1px solid rgba(255,255,255,0.15);",
     ".chat-filters input[type=\"checkbox\"], .task-filters input[type=\"checkbox\"] { appearance: none; width: 14px; height: 14px; border: 1px solid var(--border-input);"),

    (".chat-filters input[type=\"checkbox\"]:checked, .task-filters input[type=\"checkbox\"]:checked { background: #fafafa; border-color: #fafafa;",
     ".chat-filters input[type=\"checkbox\"]:checked, .task-filters input[type=\"checkbox\"]:checked { background: var(--btn-bg); border-color: var(--btn-bg);"),

    (".chat-filters input[type=\"checkbox\"]:checked::after, .task-filters input[type=\"checkbox\"]:checked::after { content: ''; position: absolute; top: 1px; left: 4px; width: 4px; height: 8px; border: solid #0a0a0b;",
     ".chat-filters input[type=\"checkbox\"]:checked::after, .task-filters input[type=\"checkbox\"]:checked::after { content: ''; position: absolute; top: 1px; left: 4px; width: 4px; height: 8px; border: solid var(--btn-text);"),

    (".filter-label { color: #444;",
     ".filter-label { color: var(--text-faint);"),

    # diff panel
    (".diff-panel { position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: #0d0d0f; border-left: 1px solid rgba(255,255,255,0.08);",
     ".diff-panel { position: fixed; top: 0; right: 0; width: 55vw; height: 100vh; background: var(--bg-sidebar); border-left: 1px solid var(--border-default);"),

    (".diff-backdrop.open { opacity: 1;",
     ".diff-backdrop.open { opacity: 1;"),

    (".diff-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.5);",
     ".diff-backdrop { position: fixed; inset: 0; background: var(--backdrop-bg);"),

    (".diff-panel-header { padding: 16px 20px 12px; border-bottom: 1px solid rgba(255,255,255,0.06);",
     ".diff-panel-header { padding: 16px 20px 12px; border-bottom: 1px solid var(--border-subtle);"),

    (".diff-panel-title { font-size: 15px; font-weight: 600; color: #fafafa;",
     ".diff-panel-title { font-size: 15px; font-weight: 600; color: var(--text-heading);"),

    (".diff-panel-commit { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; background: rgba(255,255,255,0.06); color: #a1a1a1;",
     ".diff-panel-commit { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; background: var(--bg-active); color: var(--text-secondary);"),

    (".diff-panel-close { position: absolute; top: 14px; right: 16px; background: none; border: none; color: #666;",
     ".diff-panel-close { position: absolute; top: 14px; right: 16px; background: none; border: none; color: var(--text-muted);"),

    (".diff-panel-close:hover { color: #ededed;",
     ".diff-panel-close:hover { color: var(--text-primary);"),

    (".diff-panel-tabs { display: flex; gap: 2px; padding: 8px 20px; border-bottom: 1px solid rgba(255,255,255,0.04);",
     ".diff-panel-tabs { display: flex; gap: 2px; padding: 8px 20px; border-bottom: 1px solid var(--border-subtle);"),

    (".diff-tab { padding: 6px 12px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: #666;",
     ".diff-tab { padding: 6px 12px; cursor: pointer; border-radius: 6px; background: transparent; border: none; color: var(--text-muted);"),

    (".diff-tab:hover { color: #999; background: rgba(255,255,255,0.04);",
     ".diff-tab:hover { color: var(--text-secondary); background: var(--border-subtle);"),

    (".diff-tab.active { background: rgba(255,255,255,0.08); color: #fafafa;",
     ".diff-tab.active { background: var(--bg-active); color: var(--text-heading);"),

    # diff code
    (".diff-file-header { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: rgba(255,255,255,0.03);",
     ".diff-file-header { display: flex; align-items: center; gap: 10px; padding: 8px 12px; background: var(--bg-hover);"),

    (".diff-file-name { font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; color: #ededed;",
     ".diff-file-name { font-size: 12px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-primary);"),

    (".diff-hunk-header { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; color: #555; padding: 4px 12px; background: rgba(96,165,250,0.06);",
     ".diff-hunk-header { font-size: 11px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-muted); padding: 4px 12px; background: var(--diff-hunk-bg);"),

    (".diff-line-gutter { width: 48px; min-width: 48px; text-align: right; padding-right: 8px; color: #444;",
     ".diff-line-gutter { width: 48px; min-width: 48px; text-align: right; padding-right: 8px; color: var(--text-faint);"),

    (".diff-line.add { background: rgba(34,197,94,0.08);",
     ".diff-line.add { background: var(--diff-add-bg);"),

    (".diff-line.add .diff-line-content { color: #6ee7b7;",
     ".diff-line.add .diff-line-content { color: var(--diff-add-text);"),

    (".diff-line.del { background: rgba(248,113,113,0.08);",
     ".diff-line.del { background: var(--diff-del-bg);"),

    (".diff-line.del .diff-line-content { color: #fca5a5;",
     ".diff-line.del .diff-line-content { color: var(--diff-del-text);"),

    (".diff-line.ctx .diff-line-content { color: #666;",
     ".diff-line.ctx .diff-line-content { color: var(--diff-ctx-text);"),

    (".diff-file-list-item:hover { background: rgba(255,255,255,0.04);",
     ".diff-file-list-item:hover { background: var(--border-subtle);"),

    (".diff-file-list-name { font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace; color: #ededed;",
     ".diff-file-list-name { font-size: 13px; font-family: 'SF Mono', 'Fira Code', monospace; color: var(--text-primary);"),

    (".diff-empty { color: #555;",
     ".diff-empty { color: var(--text-muted);"),

    # agent panel
    (".agent-msg { padding: 10px 12px; border-bottom: 1px solid rgba(255,255,255,0.04);",
     ".agent-msg { padding: 10px 12px; border-bottom: 1px solid var(--border-subtle);"),

    (".agent-msg-sender { font-weight: 500; font-size: 12px; color: #ededed;",
     ".agent-msg-sender { font-weight: 500; font-size: 12px; color: var(--text-primary);"),

    (".agent-msg-time { font-size: 11px; color: #444;",
     ".agent-msg-time { font-size: 11px; color: var(--text-faint);"),

    (".agent-msg-body { font-size: 12px; color: #a1a1a1;",
     ".agent-msg-body { font-size: 12px; color: var(--text-secondary);"),

    (".agent-log-header { cursor: pointer; padding: 8px 12px; background: rgba(255,255,255,0.03);",
     ".agent-log-header { cursor: pointer; padding: 8px 12px; background: var(--bg-hover);"),

    (".agent-log-header:hover { background: rgba(255,255,255,0.05);",
     ".agent-log-header:hover { background: var(--bg-active);"),

    (" color: #a1a1a1; display: flex; align-items: center; gap: 8px; }",
     " color: var(--text-secondary); display: flex; align-items: center; gap: 8px; }"),

    (".agent-log-arrow { font-size: 10px; color: #555;",
     ".agent-log-arrow { font-size: 10px; color: var(--text-muted);"),

    (".agent-log-content { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px; line-height: 1.5; color: #888;",
     ".agent-log-content { font-family: 'SF Mono', 'Fira Code', monospace; font-size: 11px; line-height: 1.5; color: var(--text-secondary);"),

    # mute toggle
    (".mute-toggle { background: transparent; border: none; color: #555;",
     ".mute-toggle { background: transparent; border: none; color: var(--text-muted);"),

    (".mute-toggle:hover { color: #999;",
     ".mute-toggle:hover { color: var(--text-secondary);"),

    # mic button
    (".chat-input .mic-btn { padding: 10px 12px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.1); background: #111113; color: #888;",
     ".chat-input .mic-btn { padding: 10px 12px; border-radius: 8px; border: 1px solid var(--border-input); background: var(--bg-input); color: var(--text-secondary);"),

    (".chat-input .mic-btn:hover { border-color: rgba(255,255,255,0.25); color: #ccc;",
     ".chat-input .mic-btn:hover { border-color: var(--border-focus); color: var(--text-primary);"),

    # scrollbar
    ("::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1);",
     "::-webkit-scrollbar-thumb { background: var(--scrollbar-thumb);"),

    ("::-webkit-scrollbar-thumb:hover { background: rgba(255,255,255,0.18);",
     "::-webkit-scrollbar-thumb:hover { background: var(--scrollbar-hover);"),
]

for old, new in css_replacements:
    if old in content:
        content = content.replace(old, new)
    else:
        print(f"WARNING: Could not find CSS pattern: {old[:80]}...")

# ============================================================
# Step 3: Replace inline style colors in JS/HTML
# ============================================================

# Sidebar loading placeholder: style="color:#444;font-size:12px"
content = content.replace(
    'style="color:#444;font-size:12px">Loading...</span>',
    'style="color:var(--text-faint);font-size:12px">Loading...</span>'
)

# loadTasks() — style="color:#888" for empty state
content = content.replace(
    """'<p style="color:#888">No tasks yet.</p>'""",
    """'<p style="color:var(--text-secondary)">No tasks yet.</p>'"""
)

content = content.replace(
    """'<p style="color:#888">No tasks match filters.</p>'""",
    """'<p style="color:var(--text-secondary)">No tasks match filters.</p>'"""
)

with open(FILE, "w") as f:
    f.write(content)

print("Done! All theme variables applied.")
