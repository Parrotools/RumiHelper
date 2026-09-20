"""SQLite 表结构：会话、记忆、任务、GitHub 监控、通知与审计记录。"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_key TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_key TEXT NOT NULL,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_key, id);

CREATE TABLE IF NOT EXISTS memories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    type        TEXT NOT NULL CHECK(type IN ('preference', 'project', 'fact', 'github', 'schedule')),
    content     TEXT NOT NULL,
    importance  REAL NOT NULL CHECK(importance >= 0 AND importance <= 1),
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_user_context
    ON memories(user_id, importance DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        TEXT NOT NULL,
    task_type       TEXT NOT NULL,
    payload         TEXT NOT NULL,
    cron_expression TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1,
    last_run        TEXT,
    next_run        TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled_next
    ON scheduled_tasks(enabled, next_run);

CREATE TABLE IF NOT EXISTS notification_settings (
    user_id         TEXT PRIMARY KEY,
    github_notify   INTEGER NOT NULL DEFAULT 0,
    daily_report    INTEGER NOT NULL DEFAULT 0,
    reminder_notify INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS github_repositories (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id    TEXT NOT NULL,
    repo_owner  TEXT NOT NULL,
    repo_name   TEXT NOT NULL,
    repo_url    TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_id, repo_owner, repo_name)
);
CREATE INDEX IF NOT EXISTS idx_github_repositories_owner ON github_repositories(owner_id);

CREATE TABLE IF NOT EXISTS github_snapshots (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id                INTEGER NOT NULL,
    stars                  INTEGER NOT NULL,
    forks                  INTEGER NOT NULL,
    watchers               INTEGER NOT NULL,
    commits_count          INTEGER NOT NULL,
    latest_commit_sha      TEXT NOT NULL,
    latest_commit_message  TEXT NOT NULL,
    latest_commit_author   TEXT NOT NULL,
    latest_commit_time     TEXT NOT NULL,
    latest_release         TEXT,
    open_issues_count      INTEGER NOT NULL,
    captured_at            TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_github_snapshots_repo ON github_snapshots(repo_id, id DESC);

CREATE TABLE IF NOT EXISTS github_notifications (
    repo_id     INTEGER NOT NULL,
    target_type TEXT NOT NULL CHECK(target_type IN ('user', 'group')),
    target_id   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(repo_id, target_type, target_id)
);

CREATE TABLE IF NOT EXISTS reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id     TEXT NOT NULL,
    report_type  TEXT NOT NULL,
    period_start TEXT NOT NULL,
    period_end   TEXT NOT NULL,
    content      TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(owner_id, report_type, period_start)
);
CREATE INDEX IF NOT EXISTS idx_reports_owner_period ON reports(owner_id, period_start DESC);

CREATE TABLE IF NOT EXISTS send_logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL,
    target_id   TEXT NOT NULL,
    content     TEXT NOT NULL,
    success     INTEGER NOT NULL,
    message_id  TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_send_logs_target ON send_logs(target_type, target_id);

CREATE TABLE IF NOT EXISTS pending_broadcasts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    admin_id    TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    content     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL
);
"""

OUTBOUND_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type     TEXT NOT NULL CHECK(target_type IN ('user', 'group')),
    target_id       TEXT NOT NULL,
    content         TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'sending', 'retry', 'sent', 'failed')),
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL,
    next_attempt_at TEXT NOT NULL,
    locked_at       TEXT,
    last_error      TEXT,
    message_id      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    sent_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbound_messages_due
    ON outbound_messages(status, next_attempt_at);
"""

GITHUB_DIGEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS github_digest_targets (
    target_type TEXT NOT NULL CHECK(target_type IN ('user', 'group')),
    target_id   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY(target_type, target_id)
);
"""

GROUP_FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS group_feedback_settings (
    group_id        TEXT PRIMARY KEY,
    feedback_enabled INTEGER NOT NULL DEFAULT 1 CHECK(feedback_enabled IN (0, 1)),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SCHEDULED_CONTENT_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_content_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER NOT NULL,
    content     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(task_id, content)
);
CREATE INDEX IF NOT EXISTS idx_scheduled_content_history_task
    ON scheduled_content_history(task_id, id DESC);
"""

OUTBOUND_NOTIFICATION_SOURCE_SCHEMA = OUTBOUND_MESSAGES_SCHEMA + """
ALTER TABLE outbound_messages
ADD COLUMN notification_kind TEXT
CHECK(notification_kind IN ('reminder', 'github', 'report') OR notification_kind IS NULL);
ALTER TABLE outbound_messages
ADD COLUMN notification_owner_id TEXT;
CREATE INDEX IF NOT EXISTS idx_outbound_notification_source
    ON outbound_messages(notification_kind, notification_owner_id, status);
"""

OUTBOUND_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS outbound_control (
    id           INTEGER PRIMARY KEY CHECK(id = 1),
    paused_until TEXT NOT NULL DEFAULT '1970-01-01 00:00:00'
);
INSERT OR IGNORE INTO outbound_control (id) VALUES (1);
"""

SYSTEM_TASK_UNIQUENESS_SCHEMA = """
UPDATE scheduled_tasks
SET enabled = 0, next_run = NULL
WHERE owner_id = '__system__'
  AND id NOT IN (
      SELECT MAX(id)
      FROM scheduled_tasks
      WHERE owner_id = '__system__'
      GROUP BY task_type
  );
CREATE UNIQUE INDEX IF NOT EXISTS idx_scheduled_tasks_system_active_unique
    ON scheduled_tasks(task_type)
    WHERE owner_id = '__system__' AND enabled = 1;
"""

DELIVERY_CONTENT_REDACTION_SCHEMA = """
UPDATE send_logs SET content = '' WHERE content <> '';
UPDATE outbound_messages
SET content = ''
WHERE status IN ('sent', 'failed') AND content <> '';
"""

TERMINAL_CONFIRMATION_REDACTION_SCHEMA = """
UPDATE pending_agent_actions
SET arguments = '{}', summary = ''
WHERE status IN ('confirmed', 'cancelled', 'expired', 'failed');
UPDATE pending_broadcasts
SET targets_json = '[]', content = ''
WHERE status IN ('confirmed', 'cancelled', 'expired', 'failed');
"""

REPORT_CONTENT_REDACTION_SCHEMA = """
UPDATE reports SET content = '' WHERE content <> '';
"""

OUTBOUND_CLAIM_OWNERSHIP_SCHEMA = """
ALTER TABLE outbound_messages ADD COLUMN lease_token TEXT;
"""

OUTBOUND_SEND_SLOT_SCHEMA = """
ALTER TABLE outbound_control
ADD COLUMN next_send_at TEXT NOT NULL DEFAULT '1970-01-01 00:00:00.000000';
"""

MESSAGE_ROLE_SANITIZATION_SCHEMA = """
UPDATE messages
SET role = 'user'
WHERE role NOT IN ('user', 'assistant');
"""

GITHUB_REPOSITORY_NOCASE_SCHEMA = """
CREATE TEMP TABLE github_repo_merge_map (
    old_id INTEGER PRIMARY KEY,
    keep_id INTEGER NOT NULL
);
INSERT INTO github_repo_merge_map (old_id, keep_id)
SELECT repo.id,
       (
           SELECT MIN(candidate.id)
           FROM github_repositories AS candidate
           WHERE candidate.owner_id = repo.owner_id
             AND lower(candidate.repo_owner) = lower(repo.repo_owner)
             AND lower(candidate.repo_name) = lower(repo.repo_name)
       )
FROM github_repositories AS repo
WHERE repo.id <> (
    SELECT MIN(candidate.id)
    FROM github_repositories AS candidate
    WHERE candidate.owner_id = repo.owner_id
      AND lower(candidate.repo_owner) = lower(repo.repo_owner)
      AND lower(candidate.repo_name) = lower(repo.repo_name)
);

INSERT INTO github_notifications (repo_id, target_type, target_id, enabled)
SELECT mapping.keep_id,
       notification.target_type,
       notification.target_id,
       notification.enabled
FROM github_notifications AS notification
JOIN github_repo_merge_map AS mapping
  ON mapping.old_id = notification.repo_id
WHERE 1
ON CONFLICT(repo_id, target_type, target_id)
DO UPDATE SET enabled = MAX(
    github_notifications.enabled,
    excluded.enabled
);

DELETE FROM github_notifications
WHERE repo_id IN (
    SELECT old_id FROM github_repo_merge_map
);

UPDATE github_snapshots
SET repo_id = (
    SELECT keep_id
    FROM github_repo_merge_map
    WHERE old_id = github_snapshots.repo_id
)
WHERE repo_id IN (
    SELECT old_id FROM github_repo_merge_map
);

DELETE FROM github_repositories
WHERE id IN (
    SELECT old_id FROM github_repo_merge_map
);

DROP TABLE github_repo_merge_map;

CREATE UNIQUE INDEX IF NOT EXISTS idx_github_repositories_owner_repo_nocase
ON github_repositories(
    owner_id,
    repo_owner COLLATE NOCASE,
    repo_name COLLATE NOCASE
);
"""

MESSAGE_EVENT_DEDUP_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_message_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key  TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SESSION_TURN_LEASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS session_turn_leases (
    session_key TEXT PRIMARY KEY,
    lease_token TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_session_turn_leases_expiry
    ON session_turn_leases(expires_at);
"""

AGENT_ACTION_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_agent_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    session_key  TEXT NOT NULL,
    tool_name    TEXT NOT NULL,
    arguments    TEXT NOT NULL,
    summary      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending', 'executing', 'confirmed', 'cancelled', 'expired', 'failed')),
    created_at   TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_agent_actions_lookup
    ON pending_agent_actions(user_id, session_key, status, id DESC);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, SCHEMA),
    (2, OUTBOUND_MESSAGES_SCHEMA),
    (3, GITHUB_DIGEST_SCHEMA),
    (4, SCHEDULED_CONTENT_HISTORY_SCHEMA),
    (5, AGENT_ACTION_SCHEMA),
    (6, GROUP_FEEDBACK_SCHEMA),
    (7, OUTBOUND_CONTROL_SCHEMA),
    (8, SYSTEM_TASK_UNIQUENESS_SCHEMA),
    (9, DELIVERY_CONTENT_REDACTION_SCHEMA),
    (10, TERMINAL_CONFIRMATION_REDACTION_SCHEMA),
    (11, REPORT_CONTENT_REDACTION_SCHEMA),
    (12, OUTBOUND_CLAIM_OWNERSHIP_SCHEMA),
    (13, OUTBOUND_SEND_SLOT_SCHEMA),
    (14, MESSAGE_ROLE_SANITIZATION_SCHEMA),
    (15, GITHUB_REPOSITORY_NOCASE_SCHEMA),
    (16, OUTBOUND_NOTIFICATION_SOURCE_SCHEMA),
    (17, MESSAGE_EVENT_DEDUP_SCHEMA),
    (18, SESSION_TURN_LEASE_SCHEMA),
)
