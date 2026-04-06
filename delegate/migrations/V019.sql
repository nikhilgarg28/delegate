-- V019: Per-project task numbering with display_id
-- Adds prefix to project_ids, seq and display_id to tasks

ALTER TABLE project_ids ADD COLUMN prefix TEXT NOT NULL DEFAULT '';
UPDATE project_ids SET prefix = UPPER(SUBSTR(REPLACE(REPLACE(name, '-', ''), '_', ''), 1, 4)) WHERE prefix = '';

ALTER TABLE tasks ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tasks ADD COLUMN display_id TEXT NOT NULL DEFAULT '';

-- Backfill seq (count of tasks with same project_uuid and id <= this task's id)
UPDATE tasks SET seq = (
    SELECT COUNT(*) FROM tasks AS t2
    WHERE t2.project_uuid = tasks.project_uuid AND t2.id <= tasks.id
) WHERE seq = 0;

-- Backfill display_id from prefix + seq
UPDATE tasks SET display_id = (
    SELECT UPPER(SUBSTR(REPLACE(REPLACE(p.name, '-', ''), '_', ''), 1, 4)) || '-' || printf('%04d', tasks.seq)
    FROM project_ids p WHERE p.uuid = tasks.project_uuid
) WHERE display_id = '' AND seq > 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_project_uuid_seq ON tasks(project_uuid, seq)
