-- Mercury's event log / dashboard data store (D1).
--
-- THIS FILE IS NO LONGER SAFE TO RUN WHOLE against the live database. The
-- ALTER TABLE ADD COLUMN statements at the bottom have already been applied,
-- and SQLite has no IF NOT EXISTS form for them, so a full run fails on
-- "duplicate column name" and D1 rolls the whole batch back. The CREATE TABLE
-- statements above never get committed, which makes the failure look like
-- nothing happened when in fact nothing did.
--
-- To add something, run only the new statements:
--   wrangler d1 execute mercury-log --remote --command "<the new statement>"
-- On a fresh database, comment the ALTER block out and run the rest.
--
-- Written by hand rather than via a migrations tool. It has now grown enough
-- to want `wrangler d1 migrations` instead; this header is the interim fix.

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

-- Rows where the semantic judge and the structured judge reached different
-- answers for the same message. Agreement is never written, so this is a
-- tuning record rather than a second copy of `messages`: the thresholds in
-- backend/verdict_policy.py are read off these disagreements.
--
-- Apply this file BEFORE deploying a worker that references the table. The
-- nightly retention sweep runs its DELETEs as one D1 batch, and a batch
-- naming a table that does not exist fails as a whole, taking the other
-- tables' purges down with it.
CREATE TABLE IF NOT EXISTS judge_comparisons (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  compared_at TEXT NOT NULL,
  authoritative TEXT,
  fields TEXT,
  detail TEXT,
  structured_confidence REAL,
  structured_severity REAL,
  structured_why TEXT,
  latency_ms INTEGER,
  model TEXT
);

CREATE INDEX IF NOT EXISTS idx_judge_comparisons_compared_at
  ON judge_comparisons (compared_at);

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

-- How Aaron's own address was actually named on the message - one of
-- R (rpgm.tools address visible in To/Cc), F (a personal address that
-- forwards into rpgm.tools visible in To/Cc), r (Bcc'd straight to an
-- rpgm.tools address) or f (Bcc'd on a message to a personal address,
-- forwarded in). Computed at ingest time (backend/app.py's
-- _classify_recipient()); recipient_detail is a short human-readable string
-- for the dashboard's tooltip. Both NULL for messages ingested before this
-- column existed.
ALTER TABLE messages ADD COLUMN recipient_class TEXT;
ALTER TABLE messages ADD COLUMN recipient_detail TEXT;
