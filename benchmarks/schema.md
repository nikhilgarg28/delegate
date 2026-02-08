# Benchmark Task YAML Schema

Benchmark tasks define self-contained coding challenges used to evaluate agent
performance. Each task is a single `.yaml` file in `benchmarks/tasks/`.

## Top-level fields

| Field                  | Type              | Required | Description                                                                 |
|------------------------|-------------------|----------|-----------------------------------------------------------------------------|
| `title`                | string            | yes      | Human-readable name for the task.                                           |
| `description`          | string            | yes      | The brief given to the agent -- what it should build or fix.                |
| `repo_setup`           | list of objects   | no       | Files or directories that must exist before the task starts (starter code). |
| `acceptance_criteria`  | list of objects   | yes      | Programmatic checks that determine pass/fail (see below).                   |
| `timeout_seconds`      | integer           | yes      | Maximum wall-clock time allowed for the task.                               |
| `tags`                 | list of strings   | yes      | Categorization labels (e.g. `backend`, `refactor`, `bugfix`).               |

## `repo_setup` entries

Each entry seeds a file into the working directory before the agent starts.

| Field     | Type   | Required | Description                          |
|-----------|--------|----------|--------------------------------------|
| `path`    | string | yes      | Relative path for the file.          |
| `content` | string | yes      | Full text content of the file.       |

## `acceptance_criteria` entries

Each entry is a single check. Exactly one *type key* must be present.

### `file_exists`

Check that a file exists at the given path.

```yaml
- file_exists:
    path: "src/foo.py"
```

### `tests_pass`

Run a shell command that executes tests. Passes when exit code is 0.

```yaml
- tests_pass:
    command: "pytest tests/test_foo.py"
```

### `grep_match`

Check that a file contains text matching a regex pattern.

```yaml
- grep_match:
    path: "src/foo.py"
    pattern: "def endpoint"
```

### `command_succeeds`

Run an arbitrary shell command. Passes when exit code is 0.

```yaml
- command_succeeds:
    command: "curl -s localhost:8000/health | jq .status"
```

## Example skeleton

```yaml
title: Implement widget parser
description: |
  Create a parser that reads widget definitions from JSON and
  returns a list of Widget dataclass instances.
repo_setup:
  - path: src/models.py
    content: |
      from dataclasses import dataclass

      @dataclass
      class Widget:
          name: str
          weight: float
acceptance_criteria:
  - file_exists:
      path: src/parser.py
  - grep_match:
      path: src/parser.py
      pattern: "def parse_widgets"
  - tests_pass:
      command: "pytest tests/test_parser.py -v"
timeout_seconds: 300
tags:
  - backend
  - feature
```
