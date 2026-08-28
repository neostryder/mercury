-- Mercury's event log / dashboard data store (D1). Applied with:
--   wrangler d1 execute mercury-log --remote --file=schema.sql
-- Written by hand rather than via a migrations tool for now - this is a
-- small, append-mostly schema. If it grows enough to need real migrations,
-- switch to `wrangler d1 migrations`.

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  from_display TEXT,
  from_domain TEXT,
  subject TEXT,
  injection_label TEXT,
  injection_score REAL,
  verdict TEXT,
  disposition TEXT,
  enforced_disposition TEXT,
  category TEXT,
  alert_level TEXT,
  reasoning TEXT,
  shadow_mode INTEGER,
  full_content TEXT,
  analysis TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_received_at ON messages (received_at);
CREATE INDEX IF NOT EXISTS idx_messages_disposition ON messages (enforced_disposition);
CREATE INDEX IF NOT EXISTS idx_messages_from_domain ON messages (from_domain);

CREATE TABLE IF NOT EXISTS rule_changes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  changed_at TEXT NOT NULL,
  action TEXT,
  rule_text TEXT,
  source TEXT
);

CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  executed_at TEXT NOT NULL,
  kind TEXT,
  details TEXT,
  outcome_summary TEXT,
  result TEXT,
  domain TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,
  kind TEXT,
  summary TEXT,
  related_message_id INTEGER,
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS admin_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  event TEXT,
  detail TEXT
);

-- Migration, apply once against an existing remote database (CREATE TABLE
-- IF NOT EXISTS above is safe to re-run; ALTER TABLE ADD COLUMN is not -
-- SQLite has no IF NOT EXISTS form for it, so re-running this against a
-- database that already has the column errors with "duplicate column name").
--
-- Records which standing rule, if any, determined a message's disposition -
-- captured verbatim by the judge at classification time (backend/app.py) -
-- so the dashboard's hard-bounce detail view can show it and support
-- reversing that specific rule from the ledger.
ALTER TABLE messages ADD COLUMN triggered_rule TEXT;
